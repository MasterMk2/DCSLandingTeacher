"""Request-body cap for ACMI uploads (Issue #29).

FastAPI parses the whole multipart body -- spooling the file part to disk --
before the endpoint or any of its dependencies run, authentication included.
A limit checked in the endpoint is checked after the bytes are already on
disk. This middleware enforces the limit while the body is received, so
neither an over-limit ``Content-Length`` nor a chunked body that declares no
length gets more than ``import_max_upload_mb`` into the process.

It is a plain ASGI middleware rather than ``@app.middleware("http")``: the cap
has to sit inside ``receive``, which the decorator form does not expose.
"""

from __future__ import annotations

from starlette.datastructures import Headers
from starlette.responses import Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.api.errors import error_envelope
from app.config import Settings


class _BodyTooLarge(Exception):
    """Raised from the app's ``receive`` once the body passes the cap."""


def _is_import_upload(scope: Scope) -> bool:
    return (
        scope["type"] == "http"
        and scope["method"] == "POST"
        and scope["path"].rstrip("/").endswith("/import")
    )


def _too_large(limit_mb: int) -> Response:
    return error_envelope(
        413,
        "UPLOAD_TOO_LARGE",
        f"upload exceeds the {limit_mb} MB limit",
        {"max_upload_mb": limit_mb},
    )


class UploadSizeLimitMiddleware:
    """Answer 413 as soon as an import upload's body exceeds the limit."""

    app: ASGIApp
    settings: Settings

    def __init__(self, app: ASGIApp, settings: Settings) -> None:
        self.app = app
        self.settings = settings

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if not _is_import_upload(scope):
            await self.app(scope, receive, send)
            return

        limit_mb = self.settings.import_max_upload_mb
        max_bytes = limit_mb * 1024 * 1024

        # A declared length over the limit is refused without reading a byte.
        declared = Headers(scope=scope).get("content-length")
        if declared is not None and declared.isdigit() and int(declared) > max_bytes:
            await _too_large(limit_mb)(scope, receive, send)
            return

        received = 0
        exceeded = False
        response_started = False

        async def capped_receive() -> Message:
            nonlocal received, exceeded
            if exceeded:
                raise _BodyTooLarge
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > max_bytes:
                    exceeded = True
                    raise _BodyTooLarge
            return message

        async def guarded_send(message: Message) -> None:
            nonlocal response_started
            if exceeded and not response_started:
                # FastAPI reports the aborted body as a 400 parse error; the
                # client gets the 413 below instead.
                return
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self.app(scope, capped_receive, guarded_send)
        except Exception:
            if not exceeded or response_started:
                raise
        if exceeded and not response_started:
            await _too_large(limit_mb)(scope, receive, send)
