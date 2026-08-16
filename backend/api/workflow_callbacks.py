"""Phase 2 Batch D — terminal wakeup + dependency synchronization.

Task 5.1: when a PaperIngestionJob first reaches a terminal state
(``embedded`` / ``failed``), the terminal timestamp is written ONCE and a
``transaction.on_commit`` callback:
  1. synchronizes the affected ProjectWorkflowDependency rows
     (pending -> succeeded/failed with terminal_at + stable error code);
  2. updates the run's last_ingestion_terminal_at (max over deps);
  3. enqueues an idempotent resume wakeup for the waiting run — but ONLY
     when EVERY dependency of that run is terminal.

The callback is a WAKEUP producer, never a second workflow owner: it never
executes graph nodes, ingestion, RAG or report persistence.
"""
from __future__ import annotations

import logging

from django.db import transaction
from django.utils import timezone

logger = logging.getLogger(__name__)

TERMINAL_JOB_STATUSES = ("embedded", "failed")


def record_job_terminal(job_id: int) -> None:
    """Called inside the ingestion task's terminal transaction.

    Returns the wakeup payload (job id + terminal status) or None when the
    job is not terminal. The caller passes the result to
    ``schedule_terminal_wakeup`` AFTER the transaction commits.
    """
    from api.models import PaperIngestionJob

    job = PaperIngestionJob.objects.filter(id=job_id).first()
    if job is None or job.status not in TERMINAL_JOB_STATUSES:
        return None
    return {"job_id": job.id, "status": job.status}


def mark_terminal_once(job) -> bool:
    """5.1 + P2-D-CX-02 + P2-D-R2-02: stamp ``terminal_at`` exactly once via
    an atomic conditional UPDATE — concurrent redelivery races converge on
    ONE stamp; the losing caller REFRESHES from the database to obtain the
    winning timestamp. Returns True when THIS call wrote the timestamp."""
    from api.models import PaperIngestionJob

    if job.terminal_at is not None:
        return False
    now = timezone.now()
    updated = PaperIngestionJob.objects.filter(
        id=job.id, terminal_at__isnull=True,
    ).update(terminal_at=now)
    if updated:
        job.terminal_at = now
        return True
    # lost the race — refresh to the WINNING timestamp from the database
    job.terminal_at = PaperIngestionJob.objects.filter(
        id=job.id).values_list("terminal_at", flat=True).first()
    return False


def finalize_job_terminal(job_id: int, status: str,
                          *, chunk_count: int = 0,
                          index_version_id: int | None = None,
                          error_code: str = "",
                          error_message: str = "",
                          expected_execution_token: str | None = None) -> dict:
    """P2-D-R3-02: THE single atomic terminal transition for all paths
    (embedded, failed, reuse). Returns the AUTHORITATIVE result:

      {"won": bool,            # did THIS call write the terminal state?
       "authoritative_status": str,   # the DB-winning status
       "terminal_at": datetime,       # the DB-winning timestamp
       "chunk_count": int,
       "index_version_id": int | None}

    Rules (P2-D-R3-02):
      - ONLY the winner registers the on_commit dependency sync/wakeup.
      - A loser registers NOTHING (recovery is the winner's or Beat's).
      - A loser refreshes and obeys the DB-authoritative values.
      - Callers must use ``authoritative_status`` for events/run/log/
        Celery returns — a failed loser never publishes ingestion_failed;
        a successful loser never publishes ingestion_completed when
        failed already won.

    P2-GLM-01 fencing: when ``expected_execution_token`` is supplied the
    conditional UPDATE also requires that exact token AND an UNEXPIRED
    execution lease (``execution_lease_expires_at > Now()``, the database
    transaction time) in the SAME atomic statement — so a stale or
    already-expired worker can never terminalize. The winning transition
    also CLEARS the private execution identity (token/heartbeat/lease)
    inside the same atomic statement.
    """
    from api.models import PaperIngestionJob

    update_fields = {"status": status}
    if chunk_count:
        update_fields["chunk_count"] = chunk_count
    if index_version_id:
        update_fields["index_version_id"] = index_version_id
    if error_code:
        update_fields["error_code"] = error_code
        update_fields["retryable"] = False
    if error_message:
        update_fields["error_message"] = error_message
    # P2-GLM-01: terminalization clears the execution identity atomically
    now = timezone.now()
    update_fields.update({
        "execution_token": "",
        "execution_heartbeat_at": None,
        "execution_lease_expires_at": None,
    })

    conditions = dict(id=job_id, terminal_at__isnull=True)
    if expected_execution_token is not None:
        conditions["execution_token"] = expected_execution_token
        # P2-GLM-01-CX-01: token equality alone is not ownership — the
        # lease must still be unexpired at the DATABASE TRANSACTION time
        # inside this same UPDATE
        from django.db.models.functions import Now
        conditions["execution_lease_expires_at__gt"] = Now()
    won = bool(PaperIngestionJob.objects.filter(
        **conditions).update(terminal_at=now, **update_fields))
    # refresh to the AUTHORITATIVE DB state (winner or losing racer)
    row = PaperIngestionJob.objects.filter(id=job_id).values_list(
        "status", "terminal_at", "chunk_count", "index_version_id",
    ).first()
    result = {
        "won": won,
        "authoritative_status": row[0] if row else status,
        "terminal_at": row[1] if row else now,
        "chunk_count": row[2] if row else (chunk_count or 0),
        "index_version_id": row[3] if row else index_version_id,
    }
    if won:
        # ONLY the winner registers the post-commit sync + wakeup.
        transaction.on_commit(lambda: _sync_and_wake(job_id))
    return result


def schedule_terminal_wakeup(payload: dict | None) -> None:
    """Register the on_commit dependency sync + resume wakeup.

    Must be called INSIDE the terminal transaction; the actual side
    effects run only after commit (Task 5.1).
    """
    if not payload:
        return
    transaction.on_commit(lambda: _sync_and_wake(payload["job_id"]))


def sync_dependencies_only(job_id: int) -> set:
    """P2-D-R2-03: dependency sync WITHOUT enqueueing the wakeup — the
    caller (reconciliation) owns the wakeup decision. Returns the set of
    run IDs whose dependencies became all-terminal (wake candidates)."""
    from api.models import PaperIngestionJob, ProjectRun

    job = PaperIngestionJob.objects.filter(id=job_id).select_related(
        "project").first()
    if job is None or job.status not in TERMINAL_JOB_STATUSES:
        return set()

    deps = list(job.workflow_dependencies.select_related("run"))
    for dep in deps:
        if dep.status != "pending":
            continue
        if job.status == "embedded":
            dep.status = "succeeded"
            dep.error_code = ""
        else:
            dep.status = "failed"
            dep.error_code = job.error_code or "ingestion_failed"
        dep.terminal_at = job.terminal_at or timezone.now()
        dep.save(update_fields=["status", "error_code", "terminal_at",
                                "updated_at"])

    wake_candidates = set()
    affected_runs = {dep.run_id for dep in deps}
    for run_id in affected_runs:
        run = ProjectRun.objects.filter(id=run_id).first()
        if run is None:
            continue
        latest = None
        for dep in run.workflow_dependencies.filter(
                terminal_at__isnull=False):
            if latest is None or dep.terminal_at > latest:
                latest = dep.terminal_at
        if latest is not None and (
                run.last_ingestion_terminal_at is None
                or latest > run.last_ingestion_terminal_at):
            ProjectRun.objects.filter(id=run.id).update(
                last_ingestion_terminal_at=latest)
        still_pending = run.workflow_dependencies.filter(
            status="pending").exists()
        if not still_pending and run.status == "waiting_ingestion":
            wake_candidates.add(run_id)
    return wake_candidates


def _sync_and_wake(job_id: int) -> None:
    """Post-commit: unified sync + one idempotent resume wakeup."""
    candidates = sync_dependencies_only(job_id)
    from api.models import ProjectRun
    for run_id in candidates:
        run = ProjectRun.objects.filter(id=run_id).first()
        if run is None:
            continue
        try:
            from api.tasks import resume_research_expand_workflow_task
            resume_research_expand_workflow_task.delay(run.id)
            logger.info(
                "workflow terminal wakeup enqueued",
                extra={"event": "workflow_terminal_wakeup",
                       "project_id": run.project_id, "run_id": run.id,
                       "reason": "deps_terminal", "status": "wakeup"})
        except Exception as exc:  # noqa: BLE001 - wakeup is best-effort;
            # Beat reconciliation (5.2) recovers lost wakeups
            logger.warning(
                "workflow terminal wakeup failed",
                extra={"event": "workflow_terminal_wakeup_failed",
                       "reason": exc.__class__.__name__,
                       "run_id": run.id, "status": "deferred"})
