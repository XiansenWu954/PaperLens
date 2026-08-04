"""Celery tasks for project background work."""
from __future__ import annotations

import logging
import time
from pathlib import Path

from asgiref.sync import async_to_sync
from celery import shared_task
from django.db import transaction

from rag.ingest import download_pdf, ingest_pdf_bytes

from .models import PaperIngestionJob, ProjectRun, ProjectRunEvent

logger = logging.getLogger(__name__)


@shared_task(bind=True, name="api.ingest_paper_pdf")
def ingest_paper_pdf_task(self, job_id: int) -> dict:
    """Parse and index a PDF for one project paper."""

    started = time.perf_counter()
    job = PaperIngestionJob.objects.select_related("project", "paper").get(id=job_id)
    run = ProjectRun.objects.create(
        project=job.project,
        kind="ingestion",
        status="running",
        question=f"Ingest PDF for {job.paper.title[:120]}",
    )
    _event(run, "ingestion_started", {"job_id": job.id, "paper_id": job.paper_id})
    logger.info(
        "paper ingestion job started",
        extra={
            "event": "ingestion_started",
            "project_id": job.project_id,
            "paper_id": job.paper_id,
            "ingestion_job_id": job.id,
            "celery_task_id": self.request.id,
            "status": "running",
        },
    )
    try:
        job.status = "parsing"
        job.celery_task_id = self.request.id or job.celery_task_id
        job.error_message = ""
        job.save(update_fields=["status", "celery_task_id", "error_message", "updated_at"])
        _event(run, "ingestion_progress", {"job_id": job.id, "status": "parsing"})

        pdf_bytes = _load_pdf_bytes(job)
        chunk_count = async_to_sync(ingest_pdf_bytes)(
            job.paper,
            pdf_bytes,
            skip_existing=False,
            replace_existing=True,
        )

        with transaction.atomic():
            job.status = "embedded"
            job.chunk_count = chunk_count
            job.error_message = ""
            job.save(update_fields=["status", "chunk_count", "error_message", "updated_at"])
            run.status = "done"
            run.output = f"Indexed {chunk_count} chunks for paper {job.paper_id}."
            run.save(update_fields=["status", "output", "updated_at"])
            _event(
                run,
                "ingestion_completed",
                {"job_id": job.id, "paper_id": job.paper_id, "chunk_count": chunk_count},
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
                "duration_ms": duration_ms,
                "status": "done",
            },
        )
        return {"job_id": job.id, "status": "embedded", "chunk_count": chunk_count}
    except Exception as exc:
        job.status = "failed"
        job.error_message = str(exc)[:1000]
        job.save(update_fields=["status", "error_message", "updated_at"])
        run.status = "error"
        run.error_message = str(exc)[:1000]
        run.save(update_fields=["status", "error_message", "updated_at"])
        _event(run, "ingestion_failed", {"job_id": job.id, "message": job.error_message})
        logger.exception(
            "paper ingestion job failed",
            extra={
                "event": "ingestion_failed",
                "project_id": job.project_id,
                "paper_id": job.paper_id,
                "ingestion_job_id": job.id,
                "error": exc.__class__.__name__,
                "duration_ms": round((time.perf_counter() - started) * 1000, 2),
                "status": "error",
            },
        )
        raise


@shared_task(bind=True, name="api.run_research_expand_workflow")
def run_research_expand_workflow_task(self, run_id: int) -> dict:
    """Run the explicit LangGraph research expansion workflow."""

    from agent.project_workflow import run_project_research_expand

    started = time.perf_counter()
    run = ProjectRun.objects.select_related("project").get(id=run_id)
    run.status = "running"
    run.save(update_fields=["status", "updated_at"])
    _event(
        run,
        "workflow_started",
        {"run_id": run.id, "project_id": run.project_id, "question": run.question[:160]},
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
        _event(
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
        run.status = "error"
        run.error_message = str(exc)[:1000]
        run.save(update_fields=["status", "error_message", "updated_at"])
        _event(run, "workflow_failed", {"message": run.error_message})
        logger.exception(
            "research expansion workflow failed",
            extra={
                "event": "workflow_failed",
                "project_id": run.project_id,
                "run_id": run.id,
                "error": exc.__class__.__name__,
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


def _event(run: ProjectRun, event_type: str, payload: dict) -> None:
    ProjectRunEvent.objects.create(run=run, event_type=event_type, payload=payload)
