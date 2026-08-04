"""Logging context and JSON formatting for PaperLens."""
from __future__ import annotations

import contextvars
import json
import logging
from datetime import datetime, timezone
from typing import Any

request_id_var: contextvars.ContextVar[str] = contextvars.ContextVar(
    "request_id", default="-"
)
task_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("task_id", default="-")


def set_request_id(request_id: str):
    return request_id_var.set(request_id or "-")


def set_task_id(task_id: int | str | None):
    return task_id_var.set(str(task_id) if task_id is not None else "-")


def reset_request_id(token) -> None:
    request_id_var.reset(token)


def reset_task_id(token) -> None:
    task_id_var.reset(token)


class RequestContextFilter(logging.Filter):
    """Attach request/task context to every log record."""

    def filter(self, record: logging.LogRecord) -> bool:
        if not hasattr(record, "request_id"):
            record.request_id = request_id_var.get()
        if not hasattr(record, "task_id"):
            record.task_id = task_id_var.get()
        if not hasattr(record, "event"):
            record.event = "-"
        if not hasattr(record, "duration_ms"):
            record.duration_ms = "-"
        if not hasattr(record, "status"):
            record.status = "-"
        if not hasattr(record, "error"):
            record.error = "-"
        return True


class JsonLogFormatter(logging.Formatter):
    """Compact JSON formatter for grep-friendly structured logs."""

    RESERVED = set(logging.LogRecord("", 0, "", 0, "", (), None).__dict__.keys())

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "request_id": getattr(record, "request_id", "-"),
            "task_id": getattr(record, "task_id", "-"),
            "event": getattr(record, "event", "-"),
            "duration_ms": getattr(record, "duration_ms", "-"),
            "status": getattr(record, "status", "-"),
            "error": getattr(record, "error", "-"),
        }
        for key, value in record.__dict__.items():
            if key in self.RESERVED or key in payload or key.startswith("_"):
                continue
            try:
                json.dumps(value, ensure_ascii=False, default=str)
                payload[key] = value
            except TypeError:
                payload[key] = str(value)
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)
