"""Shared application error type and standard error envelope (Issue #42).

Kept in its own module so both ``main`` and ``routes`` can import it without a
circular dependency.
"""

from __future__ import annotations

from fastapi.responses import JSONResponse


class AppError(Exception):
    """Application error rendered as the standard error envelope (Issue #42).

    ``status_code`` maps to HTTP (404 -> NotFound, 409 -> Conflict, 500 ->
    Internal, ...); ``error_code`` is a stable machine-readable string clients
    can branch on instead of parsing message text.
    """

    def __init__(
        self, status_code: int, error_code: str, message: str, details: dict | None = None
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.error_code = error_code
        self.message = message
        self.details = details or {}


def error_envelope(
    status_code: int, error_code: str, message: str, details: dict
) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={"error": error_code, "message": message, "details": details},
    )
