"""Project package init.

Celery is an optional dependency for background ingestion jobs. Import it
lazily so Django can still boot in lightweight environments (local venv, CI)
that have not installed Celery. When Celery is unavailable, ``celery_app``
stays ``None`` and background tasks simply skip dispatching.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

try:
    from .celery import app as celery_app
except ImportError:  # pragma: no cover - depends on celery being installed
    celery_app = None
    logger.warning(
        "celery not installed; background ingestion jobs are disabled. "
        "Install with the project requirements to enable them."
    )

__all__ = ("celery_app",)
