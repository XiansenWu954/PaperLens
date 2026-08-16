"""P2-GLM-01: private ingestion execution lease — claim, heartbeat,
fencing, release and expired-lease detection.

An ingestion ATTEMPT has a separate execution identity from the workflow
owner lease: a worker must atomically claim this lease before doing any
durable work, renew it from a background thread while parsing/embedding
and verify the token at every durable side-effect boundary. A stale worker
whose token was superseded must produce ZERO side effects; Beat may
redispatch the same idempotent job only after the lease expires.

The token and lease fields are strictly private: they never enter
serializers, API responses, events, logs, checkpoints or Celery results.
Status age alone is never evidence of a dead worker — only an expired
execution lease is.
"""
from __future__ import annotations

import logging
import secrets
import threading
from datetime import timedelta

from django.core.exceptions import ImproperlyConfigured
from django.db import transaction
from django.db.models.functions import Now
from django.utils import timezone

logger = logging.getLogger(__name__)

# Non-terminal statuses that mean "a worker is inside the attempt".
EXECUTION_IN_PROGRESS_STATUSES = (
    "downloading", "parsing", "embedding", "committing",
)

# Scheduler tolerance added on top of lease + Beat interval when deriving
# the maximum compensating redispatch latency (design §7).
SCHEDULER_TOLERANCE_SECONDS = 5.0


class ExecutionLeaseLost(RuntimeError):
    """Raised when a side-effect boundary detects the caller no longer
    owns the execution lease (token superseded / job terminal). The
    str() is a stable code — it never carries the token."""


def execution_settings() -> tuple[float, float]:
    """(lease_seconds, heartbeat_seconds) read from settings on EVERY
    call so runtime overrides (including tests) are honoured. Positive
    values with heartbeat strictly below the lease, else fail closed."""
    from django.conf import settings

    lease = float(getattr(
        settings, "PAPERLENS_INGESTION_EXECUTION_LEASE_SECONDS", 60))
    heartbeat = float(getattr(
        settings, "PAPERLENS_INGESTION_HEARTBEAT_SECONDS", 10))
    if lease <= 0 or heartbeat <= 0:
        raise ImproperlyConfigured(
            "PAPERLENS_INGESTION_EXECUTION_LEASE_SECONDS and "
            "PAPERLENS_INGESTION_HEARTBEAT_SECONDS must be positive")
    if heartbeat >= lease:
        raise ImproperlyConfigured(
            "PAPERLENS_INGESTION_HEARTBEAT_SECONDS must be strictly below "
            "PAPERLENS_INGESTION_EXECUTION_LEASE_SECONDS")
    return lease, heartbeat


def beat_interval_seconds() -> float:
    """Current reconciliation schedule interval (design §7: 15s)."""
    from django.conf import settings

    schedule = getattr(settings, "CELERY_BEAT_SCHEDULE", {}) or {}
    entry = schedule.get("reconcile-workflow-runs-every-15s") or {}
    try:
        return float(entry.get("schedule", 15.0))
    except (TypeError, ValueError):
        return 15.0


def compensation_gate() -> float:
    """Maximum compensating redispatch latency derived from RUNTIME
    configuration: execution lease + Beat interval + scheduler tolerance.
    Never a hardcoded passing duration."""
    lease, _ = execution_settings()
    return lease + beat_interval_seconds() + SCHEDULER_TOLERANCE_SECONDS


def _clear_fields(now):
    """Full identity clear — reserved for TERMINALIZATION only
    (finalize_job_terminal embeds these values in its atomic UPDATE);
    release/handoff must keep the timestamped recovery fact."""
    return {
        "execution_token": "",
        "execution_heartbeat_at": None,
        "execution_lease_expires_at": None,
        "updated_at": now,
    }

@transaction.atomic
def claim_execution(job_id: int) -> str | None:
    """Atomically claim the private execution lease for one attempt.

    Returns the fresh token, or None when the job is terminal or another
    live lease exists (the duplicate delivery must exit with zero side
    effects). Taking over an EXPIRED lease is allowed — a dead worker is
    proven only by lease expiry, never by status age. ``attempt_count``
    counts EXECUTED attempts, so it increments only on a successful claim.
    """
    from api.models import PaperIngestionJob

    rows = PaperIngestionJob.objects.select_for_update().filter(id=job_id)
    job = rows.first()
    if job is None or job.terminal_at is not None:
        return None
    now = timezone.now()
    if job.execution_token and job.execution_lease_expires_at \
            and job.execution_lease_expires_at > now:
        return None  # live worker — never disturb
    token = secrets.token_hex(16)
    updated = rows.update(
        execution_token=token,
        execution_heartbeat_at=now,
        execution_lease_expires_at=now + timedelta(
            seconds=execution_settings()[0]),
        attempt_count=job.attempt_count + 1,
        updated_at=now,
    )
    return token if updated else None


def heartbeat(job_id: int, token: str, lease_seconds: float | None = None) -> bool:
    """Renew the lease for a still-owning worker with an UNEXPIRED lease
    (P2-GLM-01-CX-01): the conditional UPDATE's WHERE requires both token
    equality and ``execution_lease_expires_at > Now()`` in the same
    statement, so an expired worker can never revive itself. Returns False
    when the token no longer matches, the job is terminal or the lease
    already expired — the caller must stop producing side effects."""
    from api.models import PaperIngestionJob

    if lease_seconds is None:
        lease_seconds = execution_settings()[0]
    now = timezone.now()
    updated = PaperIngestionJob.objects.filter(
        id=job_id, execution_token=token, terminal_at__isnull=True,
        execution_lease_expires_at__gt=Now(),
    ).update(
        execution_heartbeat_at=now,
        execution_lease_expires_at=now + timedelta(seconds=lease_seconds),
        updated_at=now,
    )
    return bool(updated)


def assert_execution_owner(job_id: int, token: str) -> None:
    """Fence for use INSIDE an open transaction: locks the job row and
    raises ExecutionLeaseLost unless the caller is the CURRENT valid
    executor — token equality alone is NOT sufficient (P2-GLM-01-CX-01):

      - ``execution_token`` matches the expected token;
      - ``terminal_at`` is NULL (non-terminal);
      - ``execution_lease_expires_at > Now()`` — the lease must still be
        unexpired relative to the DATABASE TRANSACTION time, so an
        expired worker can never fence a write even with the right token.

    The row lock this check takes is what closes the race between the
    check and the commit: competing claim/terminalize/handoff writers
    need the same lock inside the caller's transaction.
    """
    from api.models import PaperIngestionJob

    owned = PaperIngestionJob.objects.select_for_update().filter(
        id=job_id, execution_token=token, terminal_at__isnull=True,
        execution_lease_expires_at__gt=Now(),
    ).exists()
    if not owned:
        raise ExecutionLeaseLost("ingestion_execution_lease_lost")


def execution_lost(job, now=None) -> bool:
    """Expired-lease detection for reconciliation. True only when the job
    is dep-linked non-terminal AND the evidence is a lease/handoff fact —
    never status age alone:

      - ``pending`` with no executed attempt beyond the enqueue grace;
      - in-progress with an EXPIRED execution lease (worker died);
      - in-progress with a RELEASED identity whose last heartbeat is
        older than the compensation gate;
      - ``pending`` after a begun attempt whose voluntary transient
        handoff stamped a heartbeat older than the compensation gate
        (lost retry publication — P2-GLM-01-CX-03);
      - ``pending`` after a begun attempt whose handoff crashed before
        the release: the expired lease is the recovery fact.
    """
    now = now or timezone.now()
    gate = timedelta(seconds=compensation_gate())
    if job.terminal_at is not None:
        return False
    if (job.attempt_count or 0) == 0:
        return job.status == "pending" and bool(
            job.created_at and job.created_at < now - gate)
    # an attempt began
    if job.status in EXECUTION_IN_PROGRESS_STATUSES:
        if job.execution_lease_expires_at is not None:
            return job.execution_lease_expires_at < now
        if not job.execution_token and job.execution_heartbeat_at:
            return job.execution_heartbeat_at < now - gate
        return False
    if job.status == "pending":
        # voluntary transient handoff: token released, handoff heartbeat
        # stamped — recoverable only after the DYNAMIC gate (a live
        # handoff whose retry delivery is merely in flight is never
        # mis-dispatched)
        if not job.execution_token and job.execution_heartbeat_at:
            return job.execution_heartbeat_at < now - gate
        # crash between the retry-mark and the handoff release: the
        # stale lease expiry is the persisted death fact
        if job.execution_lease_expires_at is not None:
            return job.execution_lease_expires_at < now
    return False


def release_execution(job_id: int, token: str) -> bool:
    """Release a STILL-VALID lease while PRESERVING a timestamped
    recovery fact (P2-GLM-01-CX-04).

    Clears only the token and the lease expiry; ``execution_heartbeat_at``
    is re-stamped with the release time as the persisted recovery fact so
    a job that remains non-terminal (e.g. the task exited fail-closed
    after a heartbeat DB error while the job was still ``parsing``) is
    recognized by reconciliation after the DYNAMIC compensation gate —
    never immediately (a fresh handoff is never mis-dispatched), never
    lost (the fact cannot be erased by release).

    Terminalization (``finalize_job_terminal``) remains the ONLY path
    that clears all three execution-identity fields. An expired lease
    never matches this UPDATE's WHERE, so an expired worker still cannot
    erase the expired evidence (CX-03).
    """
    from api.models import PaperIngestionJob

    now = timezone.now()
    updated = PaperIngestionJob.objects.filter(
        id=job_id, execution_token=token, terminal_at__isnull=True,
        execution_lease_expires_at__gt=Now(),
    ).update(
        execution_token="",
        execution_lease_expires_at=None,
        execution_heartbeat_at=now,  # timestamped recovery fact kept
        updated_at=now,
    )
    return bool(updated)


def handoff_execution(job_id: int, token: str) -> bool:
    """Voluntary transient-retry handoff (P2-GLM-01-CX-03).

    Alias of the release semantics: clears token and lease so the Celery
    autoretry (or any replacement delivery) can reclaim IMMEDIATELY,
    while stamping ``execution_heartbeat_at`` with the handoff time as
    the persisted recovery fact: if the retry publication is lost,
    reconciliation recovers after the DYNAMIC compensation gate (pending
    + attempt begun + no token + aged heartbeat) — never earlier, so a
    live handoff is never mis-dispatched.

    Atomic conditional UPDATE: requires token match, non-terminal state
    and an UNEXPIRED lease in the same statement — an expired worker
    cannot hand off (or erase) anything.
    """
    return release_execution(job_id, token)


class IngestionHeartbeat:
    """Task-scoped heartbeat thread renewing the execution lease while the
    worker blocks inside parse/embed.

    Uses its OWN Django database connection (per-thread), which the thread
    closes in its finally block. The session context guarantees stop/join
    on exit; a lost lease (or heartbeat failure — fail closed) sets the
    ``lost`` event so the task can exit early. The authoritative fence is
    always the database boundary check, never this event alone.
    """

    def __init__(self, job_id: int, token: str, *, interval: float,
                 lease: float):
        self._job_id = job_id
        self._token = token
        self._interval = float(interval)
        self._lease = float(lease)
        self._stop = threading.Event()
        self.lost = threading.Event()
        self._thread = threading.Thread(
            target=self._run, name=f"ingest-hb-{job_id}", daemon=True)

    def _run(self) -> None:
        from django.db import connection

        try:
            while not self._stop.wait(self._interval):
                try:
                    alive = heartbeat(self._job_id, self._token, self._lease)
                except Exception:  # noqa: BLE001 — fail closed on any error
                    alive = False
                if not alive:
                    self.lost.set()
                    return
        finally:
            try:
                connection.close()
            except Exception:  # noqa: BLE001 — cleanup must never raise
                pass

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        """Signal, then join reliably (bounded)."""
        self._stop.set()
        self._thread.join(timeout=10)

    def __enter__(self) -> "IngestionHeartbeat":
        self.start()
        return self

    def __exit__(self, *exc_info) -> None:
        self.stop()
