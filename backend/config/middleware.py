"""Request observability middleware."""
from __future__ import annotations

import logging
import time
import uuid

from .logging_context import reset_request_id, set_request_id

logger = logging.getLogger("paperlens.request")


class RequestIDMiddleware:
    """Create or propagate X-Request-ID and log request completion."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        request_id = request.headers.get("X-Request-ID") or uuid.uuid4().hex[:16]
        request.paperlens_request_id = request_id
        token = set_request_id(request_id)
        started = time.perf_counter()
        try:
            response = self.get_response(request)
            duration_ms = round((time.perf_counter() - started) * 1000, 2)
            response["X-Request-ID"] = request_id
            logger.info(
                "request completed",
                extra={
                    "event": "request_completed",
                    "method": request.method,
                    "path": request.path,
                    "status": getattr(response, "status_code", "-"),
                    "duration_ms": duration_ms,
                },
            )
            return response
        except Exception as exc:
            # §30.3: no logger.exception (raw exception message) — type only.
            from agent.events import error_hash

            duration_ms = round((time.perf_counter() - started) * 1000, 2)
            logger.error(
                "request failed",
                extra={
                    "event": "request_failed",
                    "method": request.method,
                    "path": request.path,
                    "duration_ms": duration_ms,
                    "error": exc.__class__.__name__,
                    "error_hash": error_hash(exc),
                },
            )
            raise
        finally:
            reset_request_id(token)
