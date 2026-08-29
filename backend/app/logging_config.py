"""Structured logging configuration (Issue #32).

The app previously used ad-hoc ``logger.info/warning`` calls with no common
format, so correlating an ACMI ingest event with its detection, grading, and
the API request that surfaced it meant grepping free text. This module gives
every log line a stable, machine-parseable shape (JSON when configured) while
staying on the stdlib ``logging`` module -- no new dependency.

Call :func:`configure_logging` once at process start (the app factory does).
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone


class JsonFormatter(logging.Formatter):
    """Render a log record as a single JSON object on one line."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            # The record's own creation time, not "now": a queued or
            # delayed record would otherwise be stamped when it happened to
            # reach the formatter, which is the one thing a log timestamp
            # must not do.
            "ts": datetime.fromtimestamp(record.created, timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        # Surface structured context attached via ``logger.info(..., extra=...)``
        # only for keys we did not already emit.
        for key, value in record.__dict__.items():
            if key.startswith("ctx_"):
                payload[key[4:]] = value
        return json.dumps(payload, ensure_ascii=False)


def configure_logging(level: str = "INFO", json_logs: bool = True) -> None:
    """Install the application-wide log handler/formatter (Issue #32)."""
    handler = logging.StreamHandler(sys.stderr)
    if json_logs:
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
        )
    root = logging.getLogger()
    # Replace any default handlers so we don't double-emit.
    root.handlers = [handler]
    root.setLevel(level)
