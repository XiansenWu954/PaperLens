"""Celery tasks for project background work."""
from __future__ import annotations

import logging
import time
from pathlib import Path

from asgiref.sync import async_to_sync
from celery import shared_task
from django.db import transaction

from rag.ingest import download_pdf, ingest_pdf_bytes

from .models import PaperIngestionJob, ProjectRun


def _publisher_for(run: ProjectRun, **ids):
    """Unified event publisher for a run (§30.1 — never direct create)."""
    from agent.event_publisher import EventPublisher

    return EventPublisher(run=run, **ids)


def _publish(run: ProjectRun, event_type: str, payload: dict) -> None:
    _publisher_for(run).publish(event_type, payload)


def _publish_with_key(run: ProjectRun, event_type: str, payload: dict,
                      dedupe_key: str) -> None:
    """P2-C-CX-03: idempotent publish with a stable attempt-free dedupe key
    (used for task-layer lifecycle events so replay never duplicates)."""
    _publisher_for(run).publish_with_key(event_type, payload,
                                         dedupe_key=dedupe_key)

logger = logging.getLogger(__name__)


class TransientIngestError(Exception):
    """Transient ingestion failure that MAY retry (ING-D-CX-02).

    Carries only a stable error code + error hash — the str() is the code
    itself, so Celery's retry logs never relay raw exception bodies.
    """

    def __init__(self, error_code: str = "transient_ingest_error",
                 error_hash: str = "") -> None:
        super().__init__(error_code)
        self.error_code = error_code
        self.error_hash = error_hash


def _mark_transient(job, run, exc, exec_token: str | None = None):
    """P2-GLM-01-CX-02/CX-03: the retry handoff is ONE fenced transaction —
    token + non-terminal + UNEXPIRED lease verified under the job row lock
    in the same transaction as the job/run state writes and the retry
    event — followed by an atomic ``handoff_execution`` that releases the
    token immediately but STAMPS the handoff heartbeat as the persisted
    recovery fact for Beat."""
    from agent.events import error_hash

    def _body():
        job.status = "pending"
        job.error_code = "transient"
        job.error_message = f"{exc.__class__.__name__}: retryable"
        job.save(update_fields=["status", "error_code", "error_message",
                                "updated_at"])
        run.status = "running"
        run.save(update_fields=["status", "updated_at"])
        _publish(run, "ingestion_retry", {
            "job_id": job.id,
            "attempt_count": job.attempt_count,
            "error_code": exc.__class__.__name__,
            "error_hash": error_hash(exc),
            "retryable": True,
        })
        logger.warning(
            "paper ingestion job transient failure",
            extra={
                "event": "ingestion_retry",
                "project_id": job.project_id,
                "paper_id": job.paper_id,
                "ingestion_job_id": job.id,
                "attempt": job.attempt_count,
                "error": exc.__class__.__name__,
                "status": "retry",
            },
        )

    if exec_token is not None:
        from api.ingestion_execution import (assert_execution_owner,
                                             handoff_execution)
        with transaction.atomic():
            assert_execution_owner(job.id, exec_token)
            _body()
        # atomic handoff (token + unexpired lease in its WHERE): releases
        # the token for the immediate Celery retry while stamping the
        # recovery heartbeat; an expired worker hands off nothing
        handoff_execution(job.id, exec_token)
    else:
        _body()


def _fail_permanent(job, run, exc, error_code="",
                    exec_token: str | None = None, started=None):
    # §30.3: stable public error on every external/persisted surface —
    # the raw exception message is never stored.
    from agent.events import error_hash, safe_stack_frames

    started = started or time.perf_counter()
    try:
        frames = safe_stack_frames(exc)
    except (AttributeError, TypeError):
        # synthesized exceptions (no real traceback) have no frames
        frames = []
    code = error_code or getattr(exc, "error_code", "") or exc.__class__.__name__
    # P2-D-R3-02: unified atomic terminal entry — the AUTHORITATIVE
    # result decides every surface. A failed loser (embedded already
    # won) publishes ingestion_completed, NOT ingestion_failed.
    # P2-GLM-01: the terminal write is fenced by the execution token —
    # a stale worker returns the lease-lost marker with ZERO side
    # effects (no error run state, no events, no logs).
    from .workflow_callbacks import finalize_job_terminal
    with transaction.atomic():
        fin = finalize_job_terminal(
            job.id, "failed",
            error_code=code,
            error_message=f"{code}: pdf ingestion failed",
            expected_execution_token=exec_token)
        job.refresh_from_db()
    if exec_token is not None and not fin["won"]:
        return "lease_lost"
    if fin["authoritative_status"] == "embedded":
        # embedded already won — this failed attempt is a no-op loser:
        # no error run state, no ingestion_failed event/log
        run.status = "done"
        run.output = (f"Indexed {fin['chunk_count']} chunks for "
                      f"paper {job.paper_id}.")
        run.save(update_fields=["status", "output", "updated_at"])
        return fin
    run.status = "error"
    run.error_message = f"{code}: pdf ingestion failed"
    run.save(update_fields=["status", "error_message", "updated_at"])
    _publish(run, "ingestion_failed", {
        "job_id": job.id,
        "message": code,
        "error_code": code,
        "error_hash": error_hash(exc),
        "retryable": False,
        "duration_ms": round((time.perf_counter() - started) * 1000, 2),
    })
    logger.error(
        "paper ingestion job failed",
        extra={
            "event": "ingestion_failed",
            "project_id": job.project_id,
            "paper_id": job.paper_id,
            "ingestion_job_id": job.id,
            "error": code,
            "error_hash": error_hash(exc),
            "stack_frames": frames,
            "duration_ms": round((time.perf_counter() - started) * 1000, 2),
            "status": "error",
        },
    )
    return fin


@shared_task(
    bind=True,
    name="api.ingest_paper_pdf",
    # Tasks 4.4: late acknowledgement + worker-loss rejection so a crashed
    # worker redelivers the task instead of losing it.
    acks_late=True,
    reject_on_worker_lost=True,
    # Tasks 4.4: transient failures auto-retry at most 3 attempts total with
    # exponential backoff + jitter; permanent failures (PdfAcquisitionError /
    # validation) never auto-retry.
    autoretry_for=(TransientIngestError,),
    retry_backoff=True,
    retry_backoff_max=30,
    retry_jitter=True,
    retry_kwargs={"max_retries": 2},
)
def ingest_paper_pdf_task(self, job_id: int) -> dict:
    """Parse and index a PDF for one project paper (Tasks 4.1-4.4).

    P2-GLM-01: every attempt first CLAIMS the private execution lease
    (which is also the only place attempt_count increments). A duplicate
    delivery while a live lease exists exits with zero side effects; a
    task-scoped heartbeat thread renews the lease during the blocking
    parse/embed; every durable boundary (chunk persistence, activation,
    terminalization, event publishing) is fenced by the token at the
    database. Terminal transitions clear the execution identity.
    """
    from api.ingestion_execution import (IngestionHeartbeat,
                                         claim_execution,
                                         execution_settings,
                                         release_execution)

    started = time.perf_counter()
    job = PaperIngestionJob.objects.select_related("project", "paper").get(id=job_id)

    # P2-D-R4-02: a redelivered task on an ALREADY-TERMINAL job must not
    # create a phantom ingestion run or overwrite the authoritative state.
    if job.terminal_at is not None:
        job.refresh_from_db()
        return {"job_id": job.id,
                "status": job.status,
                "chunk_count": job.chunk_count}

    # P2-GLM-01: claim the execution lease BEFORE any side effect
    # (including the ingestion run row) so duplicates are harmless.
    exec_token = claim_execution(job.id)
    if exec_token is None:
        logger.info(
            "paper ingestion job skipped: execution lease held",
            extra={"event": "ingestion_execution_lease_held",
                   "project_id": job.project_id,
                   "paper_id": job.paper_id,
                   "ingestion_job_id": job.id,
                   "status": "skipped"})
        return {"job_id": job.id, "status": "skipped",
                "reason": "execution_lease_held"}
    job.refresh_from_db()  # pick up the claim's attempt_count/stamps

    lease_s, hb_interval = execution_settings()
    hb = IngestionHeartbeat(job.id, exec_token,
                            interval=hb_interval, lease=lease_s)
    hb.start()
    try:
        return _ingest_attempt_body(self, job, exec_token, hb, started)
    finally:
        hb.stop()
        # Release whatever we still own: a transient handoff already
        # released, a terminal transition cleared the identity, and a
        # superseded token makes this a no-op.
        release_execution(job.id, exec_token)


def _ingest_attempt_body(self, job, exec_token: str, hb, started: float) -> dict:
    """One fenced ingestion attempt (inside the lease heartbeat session).

    Never increments attempt_count — the claim already did. Every durable
    write passes ``exec_token`` down to its database fence; any fence
    failure exits with zero side effects instead of publishing errors.
    """
    from api.ingestion_execution import ExecutionLeaseLost

    def _lease_lost_skip():
        logger.info(
            "paper ingestion attempt exited: execution lease lost",
            extra={"event": "ingestion_execution_lease_lost",
                   "project_id": job.project_id,
                   "paper_id": job.paper_id,
                   "ingestion_job_id": job.id,
                   "status": "skipped"})
        return {"job_id": job.id, "status": "skipped",
                "reason": "execution_lease_lost"}

    # P2-GLM-01-CX-02: the ingestion run creation is fenced — token +
    # non-terminal + UNEXPIRED lease verified under the job row lock in
    # the SAME transaction as the run write (an expired worker creates
    # no phantom run)
    from api.ingestion_execution import assert_execution_owner
    try:
        with transaction.atomic():
            assert_execution_owner(job.id, exec_token)
            run = ProjectRun.objects.create(
                project=job.project,
                kind="ingestion",
                status="running",
                question=f"Ingest PDF for {job.paper.title[:120]}",
            )
    except ExecutionLeaseLost:
        return _lease_lost_skip()

    # ING-D-CX-01: a redelivery whose claimed build is ALREADY active is a
    # no-op reuse — never re-parse/embed/write/delete the active chunks.
    from rag.models import PaperIndexVersion

    active_build = None
    if job.index_version_id:
        active_build = PaperIndexVersion.objects.filter(
            id=job.index_version_id, status="active").first()
    if active_build is not None:
        # P2-D-R3-02: reuse path through the SAME unified atomic entry —
        # obey the AUTHORITATIVE result. A successful loser (failed
        # already won) must NOT publish ingestion_completed.
        # P2-GLM-01: fenced by the execution token — a stale worker
        # skips without publishing.
        from .workflow_callbacks import finalize_job_terminal
        with transaction.atomic():
            fin = finalize_job_terminal(
                job.id, "embedded",
                chunk_count=active_build.chunk_count,
                index_version_id=active_build.id,
                expected_execution_token=exec_token)
            job.refresh_from_db()
        if exec_token is not None and not fin["won"]:
            return _lease_lost_skip()
        if fin["authoritative_status"] == "failed":
            # failed already won — successful loser publishes nothing
            return fin
        run.status = "done"
        run.output = f"Reused active build {active_build.id} for paper {job.paper_id}."
        run.save(update_fields=["status", "output", "updated_at"])
        _publish(run, "ingestion_completed", {
            "job_id": job.id, "paper_id": job.paper_id,
            "chunk_count": fin["chunk_count"],
            "index_version_id": fin["index_version_id"],
            "reused": True,
        })
        logger.info(
            "paper ingestion job reused active build",
            extra={
                "event": "ingestion_reused_active_build",
                "project_id": job.project_id,
                "paper_id": job.paper_id,
                "ingestion_job_id": job.id,
                "index_version_id": fin["index_version_id"],
                "status": fin["authoritative_status"],
            },
        )
        return {"job_id": job.id, "status": fin["authoritative_status"],
                "chunk_count": fin["chunk_count"]}

    # P2-GLM-01-CX-02: started/progress events and the parsing-status
    # write share ONE fenced transaction (job row lock + token +
    # non-terminal + unexpired lease) — no write happens after a separate
    # ownership check. The whole attempt then runs inside one try block
    # whose handlers all respect the execution fence.
    try:
        with transaction.atomic():
            assert_execution_owner(job.id, exec_token)
            _publish(run, "ingestion_started",
                     {"job_id": job.id, "paper_id": job.paper_id})
            logger.info(
                "paper ingestion job started",
                extra={
                    "event": "ingestion_started",
                    "project_id": job.project_id,
                    "paper_id": job.paper_id,
                    "ingestion_job_id": job.id,
                    "celery_task_id": self.request.id,
                    "attempt_count": job.attempt_count,
                    "status": "running",
                },
            )
            # P2-GLM-01: attempt_count was incremented by the claim; this
            # write only records the in-progress state of THIS attempt.
            job.status = "parsing"
            job.celery_task_id = self.request.id or job.celery_task_id
            job.error_message = ""
            job.error_code = ""
            job.save(update_fields=[
                "status", "celery_task_id", "error_message",
                "error_code", "updated_at"])
            _publish(run, "ingestion_progress",
                     {"job_id": job.id, "status": "parsing"})

        try:
            pdf_bytes = _load_pdf_bytes(job)
        except Exception as exc:
            # permanent acquisition/loading failure: never auto-retry (the
            # exception class is not in autoretry_for). P2-D-R4-02: obey
            # the authoritative result — if embedded already won, this
            # failed loser returns safely WITHOUT raising.
            fin = _fail_permanent(job, run, exc, exec_token=exec_token,
                                  started=started)
            if fin == "lease_lost":
                return _lease_lost_skip()
            if fin["authoritative_status"] == "embedded":
                return {"job_id": job.id,
                        "status": fin["authoritative_status"],
                        "chunk_count": fin["chunk_count"]}
            raise

        if hb.lost.is_set():
            return _lease_lost_skip()

        try:
            chunk_count = async_to_sync(ingest_pdf_bytes)(
                job.paper,
                pdf_bytes,
                skip_existing=False,
                replace_existing=True,
                index_version=job.index_version,
                execution=(job.id, exec_token),
            )
        except ExecutionLeaseLost:
            # P2-GLM-01: the chunk-persistence fence rejected this stale
            # worker — zero side effects, no retry, no error surfaces.
            return _lease_lost_skip()
        except Exception as exc:
            # transient parse/embed/storage failure: auto-retry (3 attempts);
            # relay ONLY the stable code + hash, never the raw exception body
            from agent.events import error_hash

            _mark_transient(job, run, exc, exec_token=exec_token)
            raise TransientIngestError("transient_ingest_error",
                                       error_hash(exc)) from exc

        if chunk_count <= 0:
            # Tasks 4.2: zero-chunk parse must NEVER claim success. P2-D-R4-02:
            # obey the authoritative result — embedded already won -> safe
            # return; failed authoritative -> return failed.
            fin = _fail_permanent(job, run, RuntimeError("zero_chunks"),
                                  error_code="zero_chunks",
                                  exec_token=exec_token, started=started)
            if fin == "lease_lost":
                return _lease_lost_skip()
            return {"job_id": job.id,
                    "status": fin["authoritative_status"],
                    "chunk_count": fin["chunk_count"]}

        # Tasks 4.3: short activation transaction — lock, verify, supersede,
        # activate exactly one version (old active stays on any failure).
        # P2-GLM-01: both the build claim and the activation are fenced.
        from .ingestion_service import IngestionService

        service = IngestionService()
        version = service.claim_build(
            job, job.file_hash or job.source_url,
            expected_execution=(job.id, exec_token))
        service.activate(job.paper_id, version.id, chunk_count,
                         expected_execution=(job.id, exec_token))

        with transaction.atomic():
            # P2-D-R3-02: unified atomic terminal entry — obey the
            # AUTHORITATIVE result on every surface.
            from .workflow_callbacks import finalize_job_terminal
            fin = finalize_job_terminal(
                job.id, "embedded",
                chunk_count=chunk_count,
                index_version_id=version.id,
                expected_execution_token=exec_token)
            job.refresh_from_db()
        if exec_token is not None and not fin["won"]:
            return _lease_lost_skip()
        if fin["authoritative_status"] == "failed":
            # failed already won — successful loser publishes nothing
            return {"job_id": job.id,
                    "status": fin["authoritative_status"],
                    "chunk_count": fin["chunk_count"]}
        run.status = "done"
        run.output = (f"Indexed {fin['chunk_count']} chunks for "
                      f"paper {job.paper_id}.")
        run.save(update_fields=["status", "output", "updated_at"])
        _publish(
            run,
            "ingestion_completed",
            {"job_id": job.id, "paper_id": job.paper_id,
             "chunk_count": fin["chunk_count"],
             "index_version_id": fin["index_version_id"]},
        )
        duration_ms = round((time.perf_counter() - started) * 1000, 2)
        logger.info(
            "paper ingestion job completed",
            extra={
                "event": "ingestion_completed",
                "project_id": job.project_id,
                "paper_id": job.paper_id,
                "ingestion_job_id": job.id,
                "chunk_count": fin["chunk_count"],
                "index_version_id": fin["index_version_id"],
                "duration_ms": duration_ms,
                "status": fin["authoritative_status"],
            },
        )
        return {"job_id": job.id,
                "status": fin["authoritative_status"],
                "chunk_count": fin["chunk_count"]}
    except TransientIngestError:
        # Celery autoretry handles the backoff/redelivery; the job was already
        # marked pending/transient by _mark_transient.
        raise
    except ExecutionLeaseLost:
        # A fence rejected this (stale) worker mid-attempt — safe exit.
        return _lease_lost_skip()
    except Exception as exc:
        # unexpected/permanent failure. P2-D-R4-02: obey the authoritative
        # result — a failed loser (embedded already won) returns safely
        # WITHOUT raising; only the authoritative failure re-raises.
        # P2-GLM-01: an inner handler that already terminalized this job
        # must not be re-processed here (double finalize) — propagate.
        if job.terminal_at is not None:
            raise
        fin = _fail_permanent(job, run, exc, exec_token=exec_token,
                              started=started)
        if fin == "lease_lost":
            return _lease_lost_skip()
        if fin["authoritative_status"] == "embedded":
            return {"job_id": job.id,
                    "status": fin["authoritative_status"],
                    "chunk_count": fin["chunk_count"]}
        raise

def _has_checkpoint(run_id: int) -> bool:
    """P2-C-R2-03: does this run's thread have a persisted checkpoint?
    Decides whether a redelivered ``running`` run resumes from its saved
    progress instead of restarting from scratch."""
    from django.db import connection

    if connection.vendor != "postgresql":
        return False
    try:
        with connection.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM checkpoints WHERE thread_id = %s LIMIT 1",
                [str(run_id)])
            return cur.fetchone() is not None
    except Exception:  # noqa: BLE001 - missing tables -> no checkpoint
        return False


def _owner_superseded(run, owner_token: str) -> bool:
    """P2-C-R3-01: True ONLY when a DIFFERENT owner now holds the run.
    A released lease (the waiting hand-off) is NOT supersession — the
    Celery wrapper may still write the waiting terminal state."""
    from api.models import ProjectRun

    try:
        row = ProjectRun.objects.only(
            "owner_token", "lease_expires_at").get(id=run.id)
    except ProjectRun.DoesNotExist:
        return True
    if not row.owner_token:
        return False  # released by ourselves — not superseded
    return row.owner_token != owner_token


def _still_owner(run, owner_token: str) -> bool:
    """P2-C-R3-01: non-locking ownership check used by the Celery wrapper
    before every terminal write (waiting/done/error). A superseded owner
    must not write run terminal state."""
    from agent.owner_service import is_owner

    try:
        return is_owner(run, owner_token)
    except Exception:  # noqa: BLE001 - treat check failure as lost
        return False


def _run_workflow_task_body(self, run_id: int, wakeup_reason: str) -> dict:
    from agent.owner_service import (acquire_owner, new_owner_token,
                                     release_owner)
    from agent.project_workflow import (OwnerLeaseLost,
                                        clear_owner_context,
                                        current_owner_token,
                                        resume_project_research_expand,
                                        run_project_research_expand,
                                        set_owner_context)

    started = time.perf_counter()
    run = ProjectRun.objects.select_related("project").get(id=run_id)
    owner_token = new_owner_token()
    if not acquire_owner(run, owner_token):
        # Valid owner exists — duplicate start/resume exits safely.
        logger.info(
            "research expansion workflow skipped: owner held",
            extra={"event": "workflow_owner_conflict",
                   "project_id": run.project_id, "run_id": run.id,
                   "wakeup_reason": wakeup_reason, "status": "skipped"})
        return {"run_id": run.id, "status": "skipped",
                "reason": "valid_owner_exists"}

    # P2-C-R2-01: EVERY path after acquire releases the lease it still owns
    # and clears the process-local owner context via this finally block.
    set_owner_context(owner_token)
    try:
        if run.status in ("done", "partial", "error"):
            logger.info(
                "research expansion workflow skipped: terminal run",
                extra={"event": "workflow_terminal_skip",
                       "project_id": run.project_id, "run_id": run.id,
                       "status": run.status})
            return {"run_id": run.id, "status": "skipped",
                    "reason": "terminal_run"}

        # P2-C-CX-04 + P2-C-R2-03 + P2-E-CX-01: decide skip vs resume from
        # DB state + checkpoint. A resume request WITHOUT a checkpoint
        # falls back to a fresh start (the checkpoint invariant
        # waiting_ingestion→checkpoint was violated by a crash or manual
        # state; starting fresh is the only safe recovery).
        if wakeup_reason == "resume" and not _has_checkpoint(run.id):
            logger.info(
                "research expansion workflow resume without checkpoint -> start",
                extra={"event": "workflow_resume_without_checkpoint",
                       "project_id": run.project_id, "run_id": run.id,
                       "status": "restart"})
            wakeup_reason = "start"
        elif wakeup_reason == "start" and (
                run.status == "waiting_ingestion"
                or (run.status == "running" and _has_checkpoint(run.id))):
            logger.info(
                "research expansion workflow duplicate start -> resume",
                extra={"event": "workflow_duplicate_start_resumed",
                       "project_id": run.project_id, "run_id": run.id,
                       "status": "resume"})
            wakeup_reason = "resume"

        from langgraph.errors import GraphInterrupt

        run.status = "running"
        if not run.started_at:
            from django.utils import timezone
            run.started_at = timezone.now()
        run.save(update_fields=["status", "started_at", "updated_at"])
        _publish_with_key(
            run,
            "workflow_started",
            {"run_id": run.id, "project_id": run.project_id},
            dedupe_key=f"run:{run.id}:started",
        )
        logger.info(
            "research expansion workflow started",
            extra={
                "event": "workflow_started",
                "project_id": run.project_id,
                "run_id": run.id,
                "celery_task_id": self.request.id,
                "wakeup_reason": wakeup_reason,
                "status": "running",
            },
        )
        try:
            if wakeup_reason == "resume":
                result = async_to_sync(resume_project_research_expand)(
                    run.project_id, run.id)
            else:
                result = async_to_sync(run_project_research_expand)(
                    run.project_id, run.question, run.id)
        except GraphInterrupt:
            # Interrupt surfaced outside the graph runtime — release and wait.
            # P2-C-R3-01: only a DIFFERENT owner blocks the waiting write
            # (the graph's waiting hand-off releases our own lease).
            if _owner_superseded(run, owner_token):
                return {"run_id": run.id, "status": "skipped",
                        "reason": "owner_lease_lost"}
            return {"run_id": run.id, "status": "waiting_ingestion",
                    "interrupted": True}
        except OwnerLeaseLost:
            # P2-C-CX-02: lease lost mid-flight — the new owner owns the run.
            # Exit safely: NO error status, NO further events/reports.
            logger.info(
                "research expansion workflow owner lease lost — safe exit",
                extra={"event": "workflow_owner_lease_lost",
                       "project_id": run.project_id, "run_id": run.id,
                       "status": "skipped"})
            return {"run_id": run.id, "status": "skipped",
                    "reason": "owner_lease_lost"}
        except Exception as exc:
            # §30.3: stable public error surfaces; raw exception text never
            # stored.
            # P2-C-R3-01: if the owner was already lost, a generic failure
            # MUST NOT write error state to the shared run — exit skipped.
            if not _still_owner(run, owner_token):
                logger.info(
                    "research expansion workflow failed after owner loss — "
                    "safe exit",
                    extra={"event": "workflow_failed_owner_lost",
                           "project_id": run.project_id, "run_id": run.id,
                           "error": exc.__class__.__name__,
                           "status": "skipped"})
                return {"run_id": run.id, "status": "skipped",
                        "reason": "owner_lease_lost"}
            from agent.events import error_hash, safe_stack_frames

            run.status = "error"
            run.error_message = f"{exc.__class__.__name__}: workflow execution failed"
            run.save(update_fields=["status", "error_message", "updated_at"])
            _publish_with_key(run, "workflow_failed", {
                "message": exc.__class__.__name__,
                "error_hash": error_hash(exc),
            }, dedupe_key=f"run:{run.id}:failed")
            logger.error(
                "research expansion workflow failed",
                extra={
                    "event": "workflow_failed",
                    "project_id": run.project_id,
                    "run_id": run.id,
                    "error": exc.__class__.__name__,
                    "error_hash": error_hash(exc),
                    "stack_frames": safe_stack_frames(exc),
                    "duration_ms": round(
                        (time.perf_counter() - started) * 1000, 2),
                    "status": "error",
                },
            )
            raise

        # Waiting interrupt (__interrupt__ in result) or normal completion.
        interrupts = result.get("__interrupt__")
        if interrupts:
            # P2-C-R3-01: only a DIFFERENT owner blocks the waiting write
            # (the graph's waiting hand-off releases our own lease).
            if _owner_superseded(run, owner_token):
                return {"run_id": run.id, "status": "skipped",
                        "reason": "owner_lease_lost"}
            run.refresh_from_db()
            if run.status != "waiting_ingestion":
                from django.utils import timezone
                run.status = "waiting_ingestion"
                run.waiting_at = timezone.now()
                run.save(update_fields=["status", "waiting_at",
                                        "updated_at"])
            logger.info(
                "research expansion workflow waiting for ingestion",
                extra={"event": "workflow_waiting",
                       "project_id": run.project_id, "run_id": run.id,
                       "duration_ms": round(
                           (time.perf_counter() - started) * 1000, 2),
                       "status": "waiting_ingestion"})
            return {"run_id": run.id, "status": "waiting_ingestion",
                    "interrupted": True}

        # P2-C-R3-01: re-confirm ownership before the done/completed write.
        if not _still_owner(run, owner_token):
            return {"run_id": run.id, "status": "skipped",
                    "reason": "owner_lease_lost"}
        # P2-D-R2-03: final status comes from the graph's deterministic
        # outcome (done | partial | error) — read from DB to guarantee
        # surface consistency; never a hardcoded "done".
        run.refresh_from_db()
        final_status = run.status
        if final_status not in ("done", "partial"):
            # error outcome: the graph already wrote status/error — no
            # completion bookkeeping for error runs (5.4: zero reports).
            return {"run_id": run.id, "status": final_status,
                    "report_id": None}
        run.output = f"Workflow {final_status}. Report id: {result.get('report_id')}"
        from django.utils import timezone
        run.completed_at = timezone.now()
        run.save(update_fields=["output", "completed_at", "updated_at"])
        # P2-C-CX-03: the completion EVENT is produced exactly once by the
        # graph's persist_report node (stable dedupe key) — the task only
        # updates run rows, never a second workflow_completed event.
        logger.info(
            "research expansion workflow completed",
            extra={
                "event": "workflow_completed",
                "project_id": run.project_id,
                "run_id": run.id,
                "report_id": result.get("report_id"),
                "duration_ms": round(
                    (time.perf_counter() - started) * 1000, 2),
                # P2-D-R2-03: dynamic status — identical to DB/serializer/
                # API/Celery result surfaces
                "status": final_status,
            },
        )
        return {"run_id": run.id, "status": final_status,
                "report_id": result.get("report_id")}
    finally:
        # P2-C-R2-01: deterministic cleanup on EVERY path — release the
        # lease we still own (a superseded owner's release is a no-op) and
        # clear the process-local owner context.
        try:
            if current_owner_token():
                release_owner(run, owner_token)
        except Exception:  # noqa: BLE001 - cleanup must never mask results
            pass
        clear_owner_context()


@shared_task(
    bind=True,
    name="api.run_research_expand_workflow",
    # Task 4.4: late acknowledgement + worker-loss rejection so a crashed
    # worker redelivers the (idempotent) task instead of losing it.
    acks_late=True,
    reject_on_worker_lost=True,
)
def run_research_expand_workflow_task(self, run_id: int,
                                      wakeup_reason: str = "start") -> dict:
    """Run (or resume) the checkpointed LangGraph research expansion workflow.

    Task 4.4 + P2-C-CX-02/04 + P2-C-R2-01/03: acquires the 300s DB owner
    lease (token bound to a process-local context variable); the lease is
    re-validated before every durable side effect; every path after acquire
    releases its lease and clears the context via finally. Redelivered
    waiting/running runs resume from the checkpoint. Broker payload carries
    ONLY run_id + stable wakeup reason — never question/payload/token.
    """
    return _run_workflow_task_body(self, run_id, wakeup_reason)


@shared_task(
    bind=True,
    name="api.resume_research_expand_workflow",
    acks_late=True,
    reject_on_worker_lost=True,
)
def resume_research_expand_workflow_task(self, run_id: int) -> dict:
    """Explicit resume entry (Task 4.3): re-enters the SAME checkpoint
    thread. Batch D will add on_commit wakeups and Beat reconciliation."""
    return run_research_expand_workflow_task.run(run_id,
                                                 wakeup_reason="resume")


@shared_task(name="api.reconcile_workflow_runs")
def reconcile_workflow_runs_task() -> dict:
    """Batch D Task 5.2: stateless 15-second reconciliation.

    Scans (enqueue-only — NEVER executes ingestion/RAG/report/graph):
      1. waiting_ingestion runs whose dependencies are ALL terminal
      2. running runs whose owner lease has expired
      3. pending runs whose start task was never delivered/started
      4. lost wakeups: terminal jobs whose dependency rows were never
         synced by the on-commit callback (synced here, then woken)
      5. P2-GLM-01: dep-linked non-terminal ingestion jobs whose worker is
         gone — proven by an EXPIRED execution lease (or a never-started
         attempt past the enqueue grace), never by status age alone

    Each run receives AT MOST ONE wakeup request per cycle; each job at
    most ONE compensating redispatch per cycle.
    """
    from datetime import timedelta

    from django.db import connection, transaction
    from django.db.models import Q
    from django.utils import timezone

    from .ingestion_execution import execution_lost
    from .models import PaperIngestionJob, ProjectRun, ProjectWorkflowDependency
    from .workflow_callbacks import TERMINAL_JOB_STATUSES

    stats = {"waiting_terminal": 0, "running_expired": 0,
             "pending_not_started": 0, "lost_wakeup": 0, "woken": 0,
             "execution_lost": 0}
    woken_runs: set[int] = set()

    def _wake(run, reason: str) -> None:
        """One idempotent wakeup per run per cycle."""
        if run.id in woken_runs:
            return
        woken_runs.add(run.id)
        stats["woken"] += 1
        try:
            resume_research_expand_workflow_task.delay(run.id)
            logger.info(
                "reconciliation wakeup enqueued",
                extra={"event": "workflow_reconcile_wakeup",
                       "project_id": run.project_id, "run_id": run.id,
                       "reason": reason, "status": "wakeup"})
        except Exception as exc:  # noqa: BLE001 - enqueue is best-effort
            logger.warning(
                "reconciliation wakeup failed",
                extra={"event": "workflow_reconcile_wakeup_failed",
                       "run_id": run.id, "reason": exc.__class__.__name__,
                       "status": "deferred"})

    now = timezone.now()

    # 4. lost wakeups FIRST: reuse the UNIFIED dependency synchronization
    #    service (P2-D-R2-03) — per terminal job, same code path as the
    #    on-commit callback (dependency rows + last_ingestion_terminal_at
    #    max refresh). The sync itself may wake terminal runs; runs it woke
    #    are recorded so the scan below never double-wakes them.
    from .workflow_callbacks import sync_dependencies_only
    stale_jobs = (PaperIngestionJob.objects
                  .filter(status__in=TERMINAL_JOB_STATUSES,
                          workflow_dependencies__status="pending")
                  .distinct())
    stale_run_ids = set()
    synced_terminal_runs = set()
    for job in stale_jobs:
        deps = job.workflow_dependencies.filter(status="pending")
        stale_run_ids.update(deps.values_list("run_id", flat=True))
        synced_terminal_runs |= sync_dependencies_only(job.id)
    # synced runs whose deps are now all-terminal are picked up by scan 1
    # below (single bounded wakeup); do NOT pre-mark woken_runs.
    stats["lost_wakeup"] = len(stale_run_ids)

    # 1. waiting runs whose dependencies are ALL terminal
    for run in ProjectRun.objects.filter(status="waiting_ingestion"):
        has_dep = run.workflow_dependencies.exists()
        all_terminal = has_dep and not run.workflow_dependencies.filter(
            ~Q(status__in=("succeeded", "failed", "unavailable"))).exists()
        if all_terminal:
            stats["waiting_terminal"] += 1
            _wake(run, "deps_terminal")

    # 2. running runs with an expired owner lease (crashed worker) OR
    #    orphaned running runs (owner released but status never
    #    terminalized — discovered by Batch E fault injection at the
    #    enqueue boundary).
    expired = ProjectRun.objects.filter(
        Q(status="running", lease_expires_at__lt=now)
        | Q(status="running", owner_token="",
            lease_expires_at__isnull=True),
    )
    for run in expired:
        stats["running_expired"] += 1
        _wake(run, "owner_expired")

    # 3. pending runs never started (queued before `started_at` existed or
    #    lost delivery): older than one lease window with no checkpoint
    #    progress and no owner
    grace = now - timedelta(seconds=330)
    stale_pending = ProjectRun.objects.filter(
        status="pending", started_at__isnull=True,
        owner_token="", created_at__lt=grace,
    )
    for run in stale_pending:
        stats["pending_not_started"] += 1
        try:
            run_research_expand_workflow_task.delay(run.id)
            woken_runs.add(run.id)
            stats["woken"] += 1
            logger.info(
                "reconciliation start wakeup enqueued",
                extra={"event": "workflow_reconcile_start",
                       "project_id": run.project_id, "run_id": run.id,
                       "reason": "pending_not_started", "status": "wakeup"})
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "reconciliation start wakeup failed",
                extra={"event": "workflow_reconcile_start_failed",
                       "run_id": run.id,
                       "reason": exc.__class__.__name__,
                       "status": "deferred"})

    # 5. P2-GLM-01: dep-linked NON-terminal jobs whose executing worker is
    #    gone. Evidence is the private execution lease (expired) or a
    #    never-started attempt past the DYNAMIC enqueue grace — never
    #    status/updated_at age, because legitimate parses can be long.
    #    Only the EXISTING idempotent ingestion task is redispatched; the
    #    scanner never parses/embeds/activates/creates jobs or touches
    #    dependency rows, never disturbs a live lease, and redispatches
    #    each job at most once per cycle via an atomic row claim.
    skip_locked = connection.vendor == "postgresql"
    candidates = (PaperIngestionJob.objects
                  .filter(terminal_at__isnull=True,
                          workflow_dependencies__isnull=False)
                  .distinct()
                  .values_list("id", flat=True))
    for job_id in candidates:
        with transaction.atomic():
            locked = (PaperIngestionJob.objects
                      .select_for_update(skip_locked=skip_locked)
                      .filter(id=job_id).first())
            if locked is None or not execution_lost(locked, now):
                continue
            try:
                ingest_paper_pdf_task.delay(locked.id)
            except Exception as exc:  # noqa: BLE001 - enqueue best-effort
                logger.warning(
                    "execution-lost ingestion redispatch failed",
                    extra={"event": "ingestion_redispatch_failed",
                           "ingestion_job_id": locked.id,
                           "reason": exc.__class__.__name__,
                           "status": "deferred"})
                continue
            stats["execution_lost"] += 1
            logger.info(
                "execution-lost ingestion redispatched",
                extra={"event": "ingestion_redispatched",
                       "project_id": locked.project_id,
                       "paper_id": locked.paper_id,
                       "ingestion_job_id": locked.id,
                       "reason": "execution_lease_expired",
                       "status": "wakeup"})

    logger.info(
        "workflow reconciliation cycle complete",
        extra={"event": "workflow_reconcile_cycle", **stats,
               "status": "done"})
    return stats


def _load_pdf_bytes(job: PaperIngestionJob) -> bytes:
    if job.file_path:
        return Path(job.file_path).read_bytes()
    if job.source_url:
        return download_pdf(job.source_url)
    if job.paper.pdf_url:
        return download_pdf(job.paper.pdf_url)
    raise ValueError("No PDF file or pdf_url available for ingestion.")
