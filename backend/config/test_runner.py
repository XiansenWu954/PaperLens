"""Custom Django test runner with a global outbound network guard.

In test mode (offline suite), this runner patches socket.create_connection
and socket.connect to block all outbound connections EXCEPT to explicitly
allowed hosts (PostgreSQL, Redis, localhost, Docker internal services).

A canary test verifies that the guard actually blocks https://example.com.

Usage: settings.py sets TEST_RUNNER = "config.test_runner.GuardedTestRunner"
The guard is active by default. To run live integration tests that need
network, set PAPERLENS_TEST_NETWORK=1 to disable the guard.
"""
from __future__ import annotations

import os
import socket
import logging

from django.test.runner import DiscoverRunner

logger = logging.getLogger(__name__)

# Hosts that are always allowed (local services).
_ALLOWED_HOSTS = {
    "localhost", "127.0.0.1", "::1",
    "postgres", "redis", "backend", "celery-worker", "frontend",
    "0.0.0.0",
}

# Networks that are always allowed (private/local).
_ALLOWED_PREFIXES = ("10.", "172.16.", "172.17.", "172.18.", "172.19.",
                     "172.20.", "172.21.", "172.22.", "172.23.", "172.24.",
                     "172.25.", "172.26.", "172.27.", "172.28.", "172.29.",
                     "172.30.", "172.31.", "192.168.")


class NetworkAccessBlocked(OSError):
    """Raised when an outbound connection is blocked by the test network guard."""
    pass


_original_create_connection = socket.create_connection
_original_socket_connect = socket.socket.connect
_guard_active = False


def _is_allowed(address) -> bool:
    """Check if a target address is in the allowed list (local services)."""
    # address can be a tuple (host, port) or a string
    host = ""
    if isinstance(address, tuple) and len(address) >= 1:
        host = str(address[0])
    elif isinstance(address, str):
        host = address
    host = host.strip().lower()
    if host in _ALLOWED_HOSTS:
        return True
    for prefix in _ALLOWED_PREFIXES:
        if host.startswith(prefix):
            return True
    # Unix domain sockets (used by some local services)
    if host.startswith("/") or host.startswith("\\"):
        return True
    return False


def _guarded_create_connection(address, *args, **kwargs):
    if _guard_active and not _is_allowed(address):
        raise NetworkAccessBlocked(
            f"Test network guard blocked outbound connection to {address}. "
            f"Tests must be fully offline. Set PAPERLENS_TEST_NETWORK=1 for live tests."
        )
    return _original_create_connection(address, *args, **kwargs)


def _guarded_connect(self, address, *args, **kwargs):
    if _guard_active and not _is_allowed(address):
        raise NetworkAccessBlocked(
            f"Test network guard blocked socket.connect to {address}. "
            f"Tests must be fully offline."
        )
    return _original_socket_connect(self, address, *args, **kwargs)


def install_network_guard():
    """Install the socket-level outbound connection guard."""
    global _guard_active
    if _guard_active:
        return
    socket.create_connection = _guarded_create_connection
    socket.socket.connect = _guarded_connect
    _guard_active = True
    logger.info("Test network guard installed: outbound connections to non-local hosts blocked.")


def uninstall_network_guard():
    """Remove the socket-level guard (for live integration tests)."""
    global _guard_active
    socket.create_connection = _original_create_connection
    socket.socket.connect = _original_socket_connect
    _guard_active = False


def drain_asgiref_db_sessions() -> None:
    """Close DB sessions held by asgiref's persistent worker threads.

    ``sync_to_async(thread_sensitive=True)`` executes callables in
    process-lifetime worker threads (asgiref's shared single-thread executor
    and per-context executors). Their Django connections live in thread-locals
    the main thread cannot reach, so without closing them Postgres refuses
    ``DROP DATABASE`` at suite teardown (leaked session = test failure).
    A close-all callable is therefore scheduled to run inside every such
    worker thread, followed by a gc pass for connections stranded in dead
    threads. Test infrastructure only — never invoked in production.
    """
    import asyncio
    import gc

    from asgiref.sync import SyncToAsync, sync_to_async

    def _close_all_here():
        from django.db import connections

        for conn in connections.all():
            conn.close()

    try:
        asyncio.run(sync_to_async(_close_all_here, thread_sensitive=True)())
    except Exception:  # noqa: BLE001
        logger.warning("drain: asgiref single-thread close failed", exc_info=True)
    for executor in list(getattr(SyncToAsync, "context_to_thread_executor", {}).values()):
        try:
            future = executor.submit(_close_all_here)
            future.result(timeout=10)
        except Exception:  # noqa: BLE001
            logger.warning("drain: per-context executor close failed", exc_info=True)
    gc.collect()
    gc.collect()


class GuardedTestRunner(DiscoverRunner):
    """Django test runner that blocks outbound network in offline test mode."""

    def setup_test_environment(self, **kwargs):
        super().setup_test_environment(**kwargs)
        if os.environ.get("PAPERLENS_TEST_NETWORK") != "1":
            install_network_guard()
        else:
            logger.info("PAPERLENS_TEST_NETWORK=1: network guard DISABLED (live/integration suite).")

    def setup_databases(self, **kwargs):
        # Mirror production startup ordering (migrate -> checkpoint setup):
        # create LangGraph checkpoint tables in the freshly built test
        # database so checkpointed-workflow tests run against real tables.
        result = super().setup_databases(**kwargs)
        try:
            from django.db import connection
            from django.conf import settings

            if connection.vendor == "postgresql" and getattr(
                    settings, "PAPERLENS_DURABLE_WORKFLOW_ENABLED", False):
                import asyncio
                import psycopg
                from langgraph.checkpoint.postgres.aio import (
                    AsyncPostgresSaver)

                db = connection.settings_dict

                async def _setup():
                    conn = await psycopg.AsyncConnection.connect(
                        host=db.get("HOST") or "localhost",
                        port=int(db.get("PORT") or 5432),
                        dbname=db.get("NAME") or "",
                        user=db.get("USER") or "",
                        password=db.get("PASSWORD") or "",
                        autocommit=True,
                    )
                    try:
                        await AsyncPostgresSaver(conn).setup()
                    finally:
                        await conn.close()

                asyncio.run(_setup())
                logger.info("Test database checkpoint tables created.")
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "test checkpoint setup failed",
                extra={"event": "test_checkpoint_setup_failed",
                       "error": exc.__class__.__name__})
        return result

    def teardown_databases(self, old_config, **kwargs):
        # §10 gate: release asgiref worker-thread DB sessions BEFORE the runner
        # drops the test databases.
        drain_asgiref_db_sessions()
        from agent.project_workflow import close_checkpointer
        close_checkpointer()
        return super().teardown_databases(old_config, **kwargs)
