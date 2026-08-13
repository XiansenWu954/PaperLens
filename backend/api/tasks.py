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
    """Parse and index a PDF for one project paper (Tasks 4.1-4.4)."""

    started = time.perf_counter()

    def _mark_transient(job, run, exc):
        from agent.events import error_hash

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

    def _fail_permanent(job, run, exc, error_code=""):
        # §30.3: stable public error on every external/persisted surface —
        # the raw exception message is never stored.
        from agent.events import error_hash, safe_stack_frames

        try:
            frames = safe_stack_frames(exc)
        except (AttributeError, TypeError):
            # synthesized exceptions (no real traceback) have no frames
            frames = []
        code = error_code or getattr(exc, "error_code", "") or exc.__class__.__name__
        job.status = "failed"
        job.error_code = code
        job.error_message = f"{code}: pdf ingestion failed"
        job.retryable = False
        job.save(update_fields=["status", "error_code", "error_message",
                                "retryable", "updated_at"])
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

    job = PaperIngestionJob.objects.select_related("project", "paper").get(id=job_id)
    run = ProjectRun.objects.create(
        project=job.project,
        kind="ingestion",
        status="running",
        question=f"Ingest PDF for {job.paper.title[:120]}",
    )

    # ING-D-CX-01: a redelivery whose claimed build is ALREADY active is a
    # no-op reuse — never re-parse/embed/write/delete the active chunks.
    from rag.models import PaperIndexVersion

    active_build = None
    if job.index_version_id:
        active_build = PaperIndexVersion.objects.filter(
            id=job.index_version_id, status="active").first()
    if active_build is not None:
        job.status = "embedded"
        job.chunk_count = active_build.chunk_count
        job.save(update_fields=["status", "chunk_count", "updated_at"])
        run.status = "done"
        run.output = f"Reused active build {active_build.id} for paper {job.paper_id}."
        run.save(update_fields=["status", "output", "updated_at"])
        _publish(run, "ingestion_completed", {
            "job_id": job.id, "paper_id": job.paper_id,
            "chunk_count": active_build.chunk_count,
            "index_version_id": active_build.id,
            "reused": True,
        })
        logger.info(
            "paper ingestion job reused active build",
            extra={
                "event": "ingestion_reused_active_build",
                "project_id": job.project_id,
                "paper_id": job.paper_id,
                "ingestion_job_id": job.id,
                "index_version_id": active_build.id,
                "status": "done",
            },
        )
        return {"job_id": job.id, "status": "embedded", "chunk_count": active_build.chunk_count}

    _publish(run, "ingestion_started", {"job_id": job.id, "paper_id": job.paper_id})
    logger.info(
        "paper ingestion job started",
        extra={
            "event": "ingestion_started",
            "project_id": job.project_id,
            "paper_id": job.paper_id,
            "ingestion_job_id": job.id,
            "celery_task_id": self.request.id,
            "attempt_count": job.attempt_count + 1,
            "status": "running",
        },
    )
    try:
        # Tasks 2.3/4.4: attempt_count counts EXECUTED attempts.
        job.attempt_count += 1
        job.status = "parsing"
        job.celery_task_id = self.request.id or job.celery_task_id
        job.error_message = ""
        job.error_code = ""
        job.save(update_fields=[
            "attempt_count", "status", "celery_task_id", "error_message",
            "error_code", "updated_at"])
        _publish(run, "ingestion_progress", {"job_id": job.id, "status": "parsing"})

        try:
            pdf_bytes = _load_pdf_bytes(job)
        except Exception as exc:
            # permanent acquisition/loading failure: never auto-retry (the
            # exception class is not in autoretry_for) — mark failed and
            # re-raise so caller-visible error semantics stay intact.
            _fail_permanent(job, run, exc)
            raise

        try:
            chunk_count = async_to_sync(ingest_pdf_bytes)(
                job.paper,
                pdf_bytes,
                skip_existing=False,
                replace_existing=True,
                index_version=job.index_version,
            )
        except Exception as exc:
            # transient parse/embed/storage failure: auto-retry (3 attempts);
            # relay ONLY the stable code + hash, never the raw exception body
            from agent.events import error_hash

            _mark_transient(job, run, exc)
            raise TransientIngestError("transient_ingest_error",
                                       error_hash(exc)) from exc

        if chunk_count <= 0:
            # Tasks 4.2: zero-chunk parse must NEVER claim success; the job is
            # failed permanently and the task returns (never raises).
            _fail_permanent(job, run, RuntimeError("zero_chunks"),
                            error_code="zero_chunks")
            return {"job_id": job.id, "status": "failed"}

        # Tasks 4.3: short activation transaction — lock, verify, supersede,
        # activate exactly one version (old active stays on any failure).
        from .ingestion_service import IngestionService

        service = IngestionService()
        version = service.claim_build(job, job.file_hash or job.source_url)
        service.activate(job.paper_id, version.id, chunk_count)

        with transaction.atomic():
            job.status = "embedded"
            job.chunk_count = chunk_count
            job.index_version = version
            job.error_message = ""
            job.error_code = ""
            job.save(update_fields=[
                "status", "chunk_count", "index_version", "error_message",
                "error_code", "updated_at"])
            run.status = "done"
            run.output = f"Indexed {chunk_count} chunks for paper {job.paper_id}."
            run.save(update_fields=["status", "output", "updated_at"])
            _publish(
                run,
                "ingestion_completed",
                {"job_id": job.id, "paper_id": job.paper_id,
                 "chunk_count": chunk_count,
                 "index_version_id": version.id},
            )
        duration_ms = round((time.perf_counter() - started) * 1000, 2)
        logger.info(
            "paper ingestion job completed",
            extra={
                "event": "ingestion_completed",
                "project_id": job.project_id,
                "paper_id": job.paper_id,
                "ingestion_job_id": job.id,
                "chunk_count": chunk_count,
                "index_version_id": version.id,
                "duration_ms": duration_ms,
                "status": "done",
            },
        )
        return {"job_id": job.id, "status": "embedded", "chunk_count": chunk_count}
    except TransientIngestError:
        # Celery autoretry handles the backoff/redelivery; the job was already
        # marked pending/transient by _mark_transient.
        raise
    except Exception as exc:
        # unexpected/permanent failure: mark failed (idempotent — already
        # marked failures are not double-recorded) and re-raise (safe error
        # surface)
        if job.status != "failed":
            _fail_permanent(job, run, exc)
        raise

@shared_task(bind=True, name="api.run_research_expand_workflow")
def run_research_expand_workflow_task(self, run_id: int) -> dict:
    """Run the explicit LangGraph research expansion workflow."""

    from agent.project_workflow import run_project_research_expand

    started = time.perf_counter()
    run = ProjectRun.objects.select_related("project").get(id=run_id)
    run.status = "running"
    run.save(update_fields=["status", "updated_at"])
    _publish(
        run,
        "workflow_started",
        {"run_id": run.id, "project_id": run.project_id},
    )
    logger.info(
        "research expansion workflow started",
        extra={
            "event": "workflow_started",
            "project_id": run.project_id,
            "run_id": run.id,
            "celery_task_id": self.request.id,
            "status": "running",
        },
    )
    try:
        result = async_to_sync(run_project_research_expand)(run.project_id, run.question, run.id)
        run.status = "done"
        run.output = f"Workflow completed. Report id: {result.get('report_id')}"
        run.sources = result.get("search_results") or []
        run.save(update_fields=["status", "output", "sources", "updated_at"])
        _publish(
            run,
            "workflow_completed",
            {"run_id": run.id, "report_id": result.get("report_id"), "status": "done"},
        )
        logger.info(
            "research expansion workflow completed",
            extra={
                "event": "workflow_completed",
                "project_id": run.project_id,
                "run_id": run.id,
                "report_id": result.get("report_id"),
                "duration_ms": round((time.perf_counter() - started) * 1000, 2),
                "status": "done",
            },
        )
        return {"run_id": run.id, "status": "done", "report_id": result.get("report_id")}
    except Exception as exc:
        # §30.3: stable public error surfaces; raw exception text never stored.
        from agent.events import error_hash, safe_stack_frames

        run.status = "error"
        run.error_message = f"{exc.__class__.__name__}: workflow execution failed"
        run.save(update_fields=["status", "error_message", "updated_at"])
        _publish(run, "workflow_failed", {
            "message": exc.__class__.__name__,
            "error_hash": error_hash(exc),
        })
        logger.error(
            "research expansion workflow failed",
            extra={
                "event": "workflow_failed",
                "project_id": run.project_id,
                "run_id": run.id,
                "error": exc.__class__.__name__,
                "error_hash": error_hash(exc),
                "stack_frames": safe_stack_frames(exc),
                "duration_ms": round((time.perf_counter() - started) * 1000, 2),
                "status": "error",
            },
        )
        raise


def _load_pdf_bytes(job: PaperIngestionJob) -> bytes:
    if job.file_path:
        return Path(job.file_path).read_bytes()
    if job.source_url:
        return download_pdf(job.source_url)
    if job.paper.pdf_url:
        return download_pdf(job.paper.pdf_url)
    raise ValueError("No PDF file or pdf_url available for ingestion.")
