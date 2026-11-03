"""Request-body caps, enforced while the body is received.

FastAPI reads the whole body before the endpoint or any of its dependencies
run, authentication included: a multipart upload is spooled to disk, a JSON
body is held in memory. A limit checked in the endpoint is checked after the
bytes have arrived. This middleware enforces the limit while the body is
received, so neither an over-limit ``Content-Length`` nor a chunked body that
declares no length gets more than the limit into the process.

- ACMI uploads (``POST .../import``, Issue #29): ``import_max_upload_mb``,
  answered with 413 ``UPLOAD_TOO_LARGE``.
- Every other HTTP request: ``api_max_body_kb``, answered with 413
  ``REQUEST_TOO_LARGE``. The largest of those bodies is a regrade's
  ``overrides``, which is at most the whole grading config.

It is a plain ASGI middleware rather than ``@app.middleware("http")``: the cap
has to sit inside ``receive``, which the decorator form does not expose.
"""

from __future__ import annotations

from collections.abc import Callable
from functools import partial

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


def _upload_too_large(limit_mb: int) -> Response:
    return error_envelope(
        413,
        "UPLOAD_TOO_LARGE",
        f"upload exceeds the {limit_mb} MB limit",
        {"max_upload_mb": limit_mb},
    )


def _body_too_large(limit_kb: int) -> Response:
    return error_envelope(
        413,
        "REQUEST_TOO_LARGE",
        f"request body exceeds the {limit_kb} KB limit",
        {"max_body_kb": limit_kb},
    )


class BodySizeLimitMiddleware:
    """Answer 413 as soon as a request body exceeds its limit."""

    app: ASGIApp
    settings: Settings

    def __init__(self, app: ASGIApp, settings: Settings) -> None:
        self.app = app
        self.settings = settings

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        too_large: Callable[[], Response]
        if _is_import_upload(scope):
            limit_mb = self.settings.import_max_upload_mb
            max_bytes = limit_mb * 1024 * 1024
            too_large = partial(_upload_too_large, limit_mb)
        else:
            limit_kb = self.settings.api_max_body_kb
            max_bytes = limit_kb * 1024
            too_large = partial(_body_too_large, limit_kb)

        # A declared length over the limit is refused without reading a byte.
        declared = Headers(scope=scope).get("content-length")
        if declared is not None and declared.isdigit() and int(declared) > max_bytes:
            await too_large()(scope, receive, send)
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
            await too_large()(scope, receive, send)
