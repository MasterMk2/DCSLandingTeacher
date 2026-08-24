"""Simple shared-token authentication (Issue #8).

Authentication is enabled only when :attr:`app.config.Settings.auth_token`
(``DLT_AUTH_TOKEN``) is non-empty. When disabled every dependency below is a
no-op and the API behaves exactly as before (default deployment).

When enabled:

- REST endpoints under ``/api`` require ``Authorization: Bearer <token>``
  or an ``X-Auth-Token`` header (missing credentials -> 401, wrong token -> 403).
- The WebSocket endpoint cannot rely on headers (browsers cannot attach them
  to ``WebSocket`` connections), so it accepts ``?token=<token>`` instead and
  rejects the connection when the token does not match.
- ``/api/health`` stays public for liveness monitoring, and the SPA static
  hosting is outside ``/api`` and therefore never authenticated.

Token comparison uses :func:`secrets.compare_digest` (constant time).
"""

from __future__ import annotations

import secrets

from fastapi import HTTPException, Request, WebSocket


def _tokens_match(provided: str, expected: str) -> bool:
    return secrets.compare_digest(
        provided.encode("utf-8"), expected.encode("utf-8")
    )


def extract_rest_token(request: Request) -> str | None:
    """Extract the token from ``Authorization: Bearer`` or ``X-Auth-Token``."""
    authorization = request.headers.get("Authorization")
    if authorization:
        scheme, _, param = authorization.partition(" ")
        if scheme.lower() == "bearer" and param.strip():
            return param.strip()
    header_token = request.headers.get("X-Auth-Token")
    if header_token and header_token.strip():
        return header_token.strip()
    return None


async def require_auth(request: Request) -> None:
    """FastAPI dependency enforcing the shared token on REST endpoints."""
    expected = request.app.state.settings.auth_token
    if not expected:
        return
    provided = extract_rest_token(request)
    if provided is None:
        raise HTTPException(status_code=401, detail="authentication required")
    if not _tokens_match(provided, expected):
        raise HTTPException(status_code=403, detail="invalid token")


def ws_token_ok(websocket: WebSocket) -> bool:
    """Check the ``?token=`` query parameter for the WebSocket endpoint."""
    expected = websocket.app.state.settings.auth_token
    if not expected:
        return True
    provided = websocket.query_params.get("token")
    return provided is not None and _tokens_match(provided, expected)
