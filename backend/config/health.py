"""P2-B-CX-02: durable workflow health/readiness check."""
from __future__ import annotations

import logging

from django.conf import settings
from django.db import connection

logger = logging.getLogger(__name__)


def workflow_checkpointer_ready() -> bool:
    """Verify LangGraph checkpoint tables exist and the DB is reachable.

    Returns True ONLY when running on PostgreSQL AND the checkpoint tables
    are present. SQLite, missing tables, or connection failure all return
    False (fail-closed). Never exposes DSN/host/user/password/table content.
    """
    if connection.vendor != "postgresql":
        return False
    if not getattr(settings, "PAPERLENS_DURABLE_WORKFLOW_ENABLED", False):
        return False
    try:
        with connection.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM pg_tables "
                "WHERE schemaname='public' AND tablename='checkpoints'")
            row = cur.fetchone()
            if row is None:
                return False
            cur.execute("SELECT 1 FROM checkpoints LIMIT 1")
            cur.fetchall()
            return True
    except Exception as exc:
        logger.warning(
            "workflow checkpointer readiness check failed",
            extra={
                "event": "workflow_checkpointer_not_ready",
                "error": exc.__class__.__name__,
                "status": "unavailable",
            })
        return False


def durable_workflow_health() -> dict:
    """Safe health payload — no DSN, host, user, password, or table data."""
    enabled = bool(
        getattr(settings, "PAPERLENS_DURABLE_WORKFLOW_ENABLED", False))
    ready = workflow_checkpointer_ready()
    return {
        "durable_workflow_enabled": enabled,
        "workflow_checkpointer_ready": ready,
        "durable_workflow_available": enabled and ready,
    }
