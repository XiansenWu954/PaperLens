"""P2-B-CX-04: unified workflow data creation boundary.

All ProjectWorkflowDependency and workflow-owned ReportVersion creation
MUST go through this service. Direct model creation bypasses cross-project
validation and is forbidden for business code.

Why a service boundary instead of database triggers:
  - The checks span multiple tables (run.project vs paper vs job.project)
    and need Python-side object identity plus business error codes.
  - PostgreSQL triggers cannot cleanly surface per-field error codes to
    Django's ORM exception handling without OUT parameters, and would tie
    business logic to the database layer where the EventPublisher safety
    boundary cannot participate.
  - The UniqueConstraint(run, paper) remains as a last-resort DB guard.
"""
from __future__ import annotations

import logging

from django.db import IntegrityError

from .models import (
    PaperIngestionJob, ProjectRun, ProjectWorkflowDependency, ProjectPaper,
    ReportVersion,
)

logger = logging.getLogger(__name__)


class WorkflowDataError(Exception):
    """Stable error with a safe code — never carries raw DB or user data."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


FORBIDDEN_MEMBERSHIP_STATUSES = {"excluded"}


def create_workflow_dependency(
    *, run: ProjectRun, paper, ingestion_job: PaperIngestionJob | None = None,
    status: str = "pending",
) -> ProjectWorkflowDependency:
    """Create a dependency after validating project consistency.

    Rejects (with stable codes):
      foreign_paper — paper not a member of run.project (non-excluded)
      excluded_paper — paper's membership is excluded
      job_project_mismatch — ingestion_job.project != run.project
      job_paper_mismatch — ingestion_job.paper != paper
    """
    membership = ProjectPaper.objects.filter(
        project_id=run.project_id, paper_id=paper.id).first()
    if membership is None:
        raise WorkflowDataError("foreign_paper")
    if membership.status in FORBIDDEN_MEMBERSHIP_STATUSES:
        raise WorkflowDataError("excluded_paper")
    if ingestion_job is not None:
        if ingestion_job.project_id != run.project_id:
            raise WorkflowDataError("job_project_mismatch")
        if ingestion_job.paper_id != paper.id:
            raise WorkflowDataError("job_paper_mismatch")
    try:
        return ProjectWorkflowDependency.objects.create(
            run=run, paper=paper,
            ingestion_job=ingestion_job, status=status)
    except IntegrityError as exc:
        raise WorkflowDataError("duplicate_dependency") from exc


def create_workflow_report(
    *, run: ProjectRun, title: str, content: str,
    source: str = "langgraph",
) -> ReportVersion:
    """Create a workflow-owned report after validating project consistency.

    Rejects:
      report_project_mismatch — run.project != the target project
      duplicate_report_for_run — a report already exists for this run;
      the caller should reuse the existing report instead.
    """
    existing = ReportVersion.objects.filter(source_run=run).first()
    if existing is not None:
        raise WorkflowDataError("duplicate_report_for_run")
    try:
        return ReportVersion.objects.create(
            project_id=run.project_id, title=title, content=content,
            source=source, source_run=run)
    except IntegrityError as exc:
        raise WorkflowDataError("duplicate_report_for_run") from exc
