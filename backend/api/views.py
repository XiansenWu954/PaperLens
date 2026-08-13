"""API REST 视图。

POST /api/research        创建研究任务（返回 task_id，由前端连 SSE）
GET  /api/research/<id>   查询任务结果
"""
from __future__ import annotations

import logging
import hashlib
import os
import time
from pathlib import Path

from asgiref.sync import async_to_sync
from django.conf import settings
from django.db.models import Count, OuterRef, Subquery
from django.http import HttpResponse, StreamingHttpResponse
from rest_framework import status
from rest_framework.decorators import api_view
from rest_framework.response import Response

from agent.harness import ProjectAgentHarness
from agent.project_tools import add_papers_to_project, execute_project_tool
from papers.models import Paper, upsert_paper
from rag.acquisition import PdfAcquisitionError
from realtime.sse import _sse

from .demo import seed_demo_project
from .models import ChatSession, PaperIngestionJob, PaperRelation, ProjectPaper, ProjectRun, ProjectRunEvent, ReportVersion, ResearchProject, ResearchTask
from .serializers import (
    ChatSessionSerializer,
    CreateResearchTaskSerializer,
    PaperIngestionJobSerializer,
    ProjectPaperSerializer,
    ProjectRunSerializer,
    ReportVersionSerializer,
    ResearchProjectSerializer,
    ResearchTaskSerializer,
)

logger = logging.getLogger(__name__)

# Tasks 3.2: hard ceiling for streamed PDF uploads (50 MiB, boundary inclusive)
MAX_UPLOAD_BYTES = 50 * 1024 * 1024


def _cleanup_upload_artifacts(
    part_path: Path, target_path: Path | None, *, remove_target: bool = False
) -> None:
    """Remove partial artifacts of a failed upload (Tasks 3.2 / ING-C-CX-04).

    Only the .part temp file is removed by default. ``target_path`` is
    content-addressed and may pre-exist this request, so it is removed ONLY
    when ``remove_target=True`` — i.e. the caller PROVED the target did not
    exist before this request (any bytes there can only be this request's
    partial output). Pre-existing artifacts are never deleted.
    """
    for candidate in (part_path, target_path if remove_target else None):
        if candidate is None:
            continue
        try:
            candidate.unlink(missing_ok=True)
        except OSError:
            pass


@api_view(["POST"])
def create_research(request):
    """创建研究任务。前端拿到 task_id 后连 /api/research/<id>/stream。"""
    started = time.perf_counter()
    serializer = CreateResearchTaskSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    question = serializer.validated_data["question"].strip()
    if not question:
        logger.info(
            "research create rejected",
            extra={"event": "research_create_rejected", "status": 400},
        )
        return Response({"error": "question 不能为空"}, status=status.HTTP_400_BAD_REQUEST)
    project = None
    project_id = serializer.validated_data.get("project_id")
    if project_id:
        project = ResearchProject.objects.filter(id=project_id).first()
    task = ResearchTask.objects.create(question=question, project=project, status="pending")
    logger.info(
        "research task created",
        extra={
            "event": "research_created",
            "task_id": task.id,
            "question_preview": question[:120],
            "duration_ms": round((time.perf_counter() - started) * 1000, 2),
            "status": 201,
        },
    )
    return Response({"task_id": task.id, "question": task.question}, status=status.HTTP_201_CREATED)


@api_view(["GET"])
def get_research(request, task_id: int):
    """查询任务结果（完成后含综述/图谱/来源）。"""
    started = time.perf_counter()
    try:
        task = ResearchTask.objects.get(id=task_id)
    except ResearchTask.DoesNotExist:
        logger.info(
            "research task not found",
            extra={"event": "research_not_found", "task_id": task_id, "status": 404},
        )
        return Response({"error": "任务不存在"}, status=status.HTTP_404_NOT_FOUND)
    logger.info(
        "research task fetched",
        extra={
            "event": "research_fetched",
            "task_id": task.id,
            "task_status": task.status,
            "duration_ms": round((time.perf_counter() - started) * 1000, 2),
            "status": 200,
        },
    )
    return Response(ResearchTaskSerializer(task).data)


def _project_queryset(include_archived: bool = False):
    latest_report = (
        ReportVersion.objects.filter(project=OuterRef("pk"))
        .order_by("-created_at", "-id")
        .values("id")[:1]
    )
    queryset = ResearchProject.objects.annotate(
        paper_count=Count("project_papers", distinct=True),
        run_count=Count("runs", distinct=True),
        latest_report_id=Subquery(latest_report),
    )
    if not include_archived:
        queryset = queryset.filter(status="active")
    return queryset.order_by("-updated_at", "-id")


@api_view(["GET", "POST"])
def projects(request):
    if request.method == "GET":
        include_archived = request.query_params.get("include_archived") in {"1", "true", "yes"}
        return Response(ResearchProjectSerializer(_project_queryset(include_archived), many=True).data)

    serializer = ResearchProjectSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    project = serializer.save()
    logger.info(
        "project created",
        extra={"event": "project_created", "project_id": project.id, "status": 201},
    )
    return Response(
        ResearchProjectSerializer(_project_queryset().get(id=project.id)).data,
        status=status.HTTP_201_CREATED,
    )


@api_view(["GET", "PATCH", "DELETE"])
def project_detail(request, project_id: int):
    try:
        project = ResearchProject.objects.get(id=project_id)
    except ResearchProject.DoesNotExist:
        return Response({"error": "项目不存在"}, status=status.HTTP_404_NOT_FOUND)

    if request.method == "GET":
        return Response(ResearchProjectSerializer(_project_queryset().get(id=project.id)).data)
    if request.method == "PATCH":
        serializer = ResearchProjectSerializer(project, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(ResearchProjectSerializer(_project_queryset().get(id=project.id)).data)
    project.status = "archived"
    project.save(update_fields=["status", "updated_at"])
    return Response(status=status.HTTP_204_NO_CONTENT)


def _project_papers_queryset(project_id: int):
    return (
        ProjectPaper.objects.select_related("paper", "paper__venue")
        .filter(project_id=project_id)
        .annotate(chunk_count=Count("paper__chunks", distinct=True))
        .order_by("-paper__citation_count", "paper__title")
    )


def _get_project_paper_link(project_id: int, paper_id: int) -> ProjectPaper | None:
    return ProjectPaper.objects.select_related("project", "paper").filter(
        project_id=project_id,
        paper_id=paper_id,
    ).first()


def _enqueue_ingestion_job(job: PaperIngestionJob):
    from .tasks import ingest_paper_pdf_task

    result = ingest_paper_pdf_task.delay(job.id)
    if result.id and job.celery_task_id != result.id:
        PaperIngestionJob.objects.filter(id=job.id).update(celery_task_id=result.id)
        job.celery_task_id = result.id
    return result


def _publish_ingestion_event(
    project_id: int, event_type: str, payload: dict
) -> None:
    """Tasks 5.4: view-layer ingestion events flow through the SAME
    EventPublisher sanitize boundary (schema allowlist + correlation ids);
    they are not persisted (no run) but are schema-validated."""
    from agent.event_publisher import EventPublisher

    EventPublisher(project_id=project_id, persist=False).publish(
        event_type, payload)


@api_view(["GET", "POST"])
def project_papers(request, project_id: int):
    project = ResearchProject.objects.filter(id=project_id).first()
    if not project:
        return Response({"error": "项目不存在"}, status=status.HTTP_404_NOT_FOUND)
    if request.method == "GET":
        return Response(ProjectPaperSerializer(_project_papers_queryset(project_id), many=True).data)

    paper_id = request.data.get("paper_id")
    payload = request.data.get("paper")
    if paper_id:
        paper = Paper.objects.filter(id=paper_id).first()
        if not paper:
            return Response({"error": "论文不存在"}, status=status.HTTP_404_NOT_FOUND)
    elif isinstance(payload, dict):
        paper = upsert_paper(payload)
    else:
        return Response({"error": "paper_id 或 paper payload 必填"}, status=status.HTTP_400_BAD_REQUEST)

    link, _created = ProjectPaper.objects.get_or_create(
        project=project,
        paper=paper,
        defaults={
            "status": request.data.get("status", "candidate"),
            "source_reason": request.data.get("source_reason", "User added paper"),
            "added_by": "user",
        },
    )
    return Response(ProjectPaperSerializer(link).data, status=status.HTTP_201_CREATED)


@api_view(["POST"])
def project_papers_import(request, project_id: int):
    """从 BibTeX / RIS 文本或上传文件批量导入论文到项目库。

    接受：
    - multipart 上传 .bib/.ris 文件（字段 file）
    - JSON body {format: "bibtex"|"ris", text: "..."}
    """
    from papers.bibtex import parse_bibtex, parse_ris
    from papers.models import upsert_paper

    project = ResearchProject.objects.filter(id=project_id).first()
    if not project:
        return Response({"error": "项目不存在"}, status=status.HTTP_404_NOT_FOUND)

    fmt = (request.data.get("format") or "").strip().lower()
    text = request.data.get("text") or ""
    upload = request.FILES.get("file")
    if upload:
        text = upload.read().decode("utf-8", errors="replace")
        if not fmt:
            fmt = "bibtex" if upload.name.lower().endswith(".bib") else "ris"
    if not text.strip():
        return Response({"error": "text 或 file 必填"}, status=status.HTTP_400_BAD_REQUEST)
    if not fmt:
        fmt = "bibtex" if " @" in text or "@article" in text.lower() or "@inproceedings" in text.lower() else "ris"

    payloads = parse_bibtex(text) if fmt == "bibtex" else parse_ris(text)
    if not payloads:
        return Response({"error": f"未解析出任何条目（{fmt}）"}, status=status.HTTP_400_BAD_REQUEST)

    added: list[dict] = []
    for payload in payloads:
        paper = upsert_paper(payload)
        link, created = ProjectPaper.objects.get_or_create(
            project=project,
            paper=paper,
            defaults={
                "status": "candidate",
                "source_reason": f"Imported from {fmt}",
                "added_by": "user",
            },
        )
        added.append({"paper_id": paper.id, "title": paper.title, "created": created})
    logger.info(
        "project papers imported",
        extra={"event": "project_papers_imported", "project_id": project_id, "count": len(added), "format": fmt},
    )
    return Response({"format": fmt, "count": len(added), "added": added}, status=status.HTTP_201_CREATED)


def _project_papers_for_export(project_id: int) -> list:
    return [row.paper for row in ProjectPaper.objects.select_related("paper", "paper__venue")
            .filter(project_id=project_id).exclude(status="excluded")]


@api_view(["GET"])
def project_papers_export_bib(request, project_id: int):
    """导出项目论文库为 BibTeX。"""
    from papers.bibtex import papers_to_bibtex

    if not ResearchProject.objects.filter(id=project_id).exists():
        return Response({"error": "项目不存在"}, status=status.HTTP_404_NOT_FOUND)
    content = papers_to_bibtex(_project_papers_for_export(project_id))
    resp = HttpResponse(content, content_type="application/x-bibtex")
    resp["Content-Disposition"] = f'attachment; filename="project-{project_id}.bib"'
    return resp


@api_view(["GET"])
def project_papers_export_ris(request, project_id: int):
    """导出项目论文库为 RIS。"""
    from papers.bibtex import papers_to_ris

    if not ResearchProject.objects.filter(id=project_id).exists():
        return Response({"error": "项目不存在"}, status=status.HTTP_404_NOT_FOUND)
    content = papers_to_ris(_project_papers_for_export(project_id))
    resp = HttpResponse(content, content_type="application/x-research-info-systems")
    resp["Content-Disposition"] = f'attachment; filename="project-{project_id}.ris"'
    return resp


@api_view(["PATCH", "DELETE"])
def project_paper_detail(request, project_id: int, paper_id: int):
    link = ProjectPaper.objects.filter(project_id=project_id, paper_id=paper_id).first()
    if not link:
        return Response({"error": "项目论文不存在"}, status=status.HTTP_404_NOT_FOUND)
    if request.method == "PATCH":
        serializer = ProjectPaperSerializer(link, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(ProjectPaperSerializer(link).data)
    link.delete()
    return Response(status=status.HTTP_204_NO_CONTENT)


@api_view(["POST"])
def project_paper_pdf_upload(request, project_id: int, paper_id: int):
    link = _get_project_paper_link(project_id, paper_id)
    if not link:
        return Response({"error": "项目论文不存在"}, status=status.HTTP_404_NOT_FOUND)
    uploaded = request.FILES.get("file")
    if not uploaded:
        return Response({"error": "multipart 字段 file 不能为空"}, status=status.HTTP_400_BAD_REQUEST)
    if uploaded.size <= 0:
        return Response({"error": "PDF 文件为空"}, status=status.HTTP_400_BAD_REQUEST)

    original_name = Path(uploaded.name).name or f"paper-{paper_id}.pdf"
    target_dir = Path(settings.MEDIA_ROOT) / "papers" / str(paper_id)
    target_dir.mkdir(parents=True, exist_ok=True)

    # Tasks 3.2/ING-C-CX-02/04: stream the upload via chunks() into a .part
    # temp file with a hard size cap; the committed file is content-addressed
    # and promoted by an ATOMIC same-directory replace — the payload is never
    # read back into memory (no Path.read_bytes). Identical hash already
    # present stays idempotent (replace overwrites the identical content).
    # Cleanup removes the .part always, and the target ONLY when the target
    # did not exist before this request (so pre-existing content-addressed
    # artifacts are never deleted).
    part_path = target_dir / f".{time.time_ns()}-{paper_id}.part"
    target_path: Path | None = None
    target_existed = False
    written = 0
    hasher = hashlib.sha256()
    try:
        with open(part_path, "wb") as fh:
            for chunk in uploaded.chunks():
                written += len(chunk)
                if written > MAX_UPLOAD_BYTES:
                    raise PdfAcquisitionError("size_limit_exceeded")
                hasher.update(chunk)
                fh.write(chunk)
        if written == 0:
            raise PdfAcquisitionError("invalid_pdf_magic")
        with open(part_path, "rb") as fh:
            head = fh.read(len(b"%PDF-"))
        if not head.startswith(b"%PDF-"):
            raise PdfAcquisitionError("invalid_pdf_magic")

        file_hash = hasher.hexdigest()
        target_path = target_dir / f"{file_hash}.pdf"
        target_existed = target_path.exists()
        os.replace(part_path, target_path)
    except PdfAcquisitionError as exc:
        _cleanup_upload_artifacts(part_path, target_path)
        logger.info(
            "project paper PDF upload rejected",
            extra={
                "event": "ingestion_upload_rejected",
                "project_id": project_id,
                "paper_id": paper_id,
                "error_code": exc.error_code,
                "status": "rejected",
            },
        )
        return Response({"error": exc.error_code}, status=status.HTTP_400_BAD_REQUEST)
    except OSError:
        # ING-C-CX-04: os.replace is atomic — a pre-existing content-addressed
        # target is untouched and MUST survive; a target that did not exist
        # before this request can only be this request's partial output and is
        # removed together with the .part.
        _cleanup_upload_artifacts(
            part_path, target_path, remove_target=not target_existed)
        logger.error(
            "project paper PDF upload storage failed",
            extra={
                "event": "ingestion_upload_storage_failed",
                "project_id": project_id,
                "paper_id": paper_id,
                "status": "error",
            },
        )
        return Response({"error": "storage_failed"}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    # Tasks 4.1: scoped job get-or-create on the request key (concurrent
    # identical uploads converge on ONE job) + global build claim (cross-
    # project identical PDFs share ONE non-null index version).
    from .ingestion_service import IngestionService

    service = IngestionService()
    idempotency_key = service.request_key(project_id, paper_id, file_hash)
    job, created = service.get_or_create_job(
        link.project,
        link.paper,
        idempotency_key=idempotency_key,
        file_name=original_name,
        file_hash=file_hash,
        file_path=str(target_path),
        source_kind="upload",
        file_size=written,
    )
    service.claim_build(job, file_hash)
    if job.index_version_id and job.index_version.status == "active":
        # ING-D-CX-01: the global build for this paper+source is ALREADY
        # active — reuse it: the job goes straight to the embedded/reused
        # terminal state with the active chunk_count; nothing is parsed,
        # embedded, written, deleted or enqueued again.
        job.status = "embedded"
        job.chunk_count = job.index_version.chunk_count
        job.save(update_fields=["status", "chunk_count", "updated_at"])
        created = False
        logger.info(
            "project paper PDF upload reused active build",
            extra={
                "event": "ingestion_upload_reused_active_build",
                "project_id": project_id,
                "paper_id": paper_id,
                "ingestion_job_id": job.id,
                "index_version_id": job.index_version_id,
                "status": 201,
            },
        )
    if created:
        async_result = _enqueue_ingestion_job(job)
        job.refresh_from_db()
        celery_task_id = async_result.id or job.celery_task_id
        logger.info(
            "project paper PDF upload queued",
            extra={
                "event": "ingestion_upload_queued",
                "project_id": project_id,
                "paper_id": paper_id,
                "ingestion_job_id": job.id,
                "file_size": written,
                "file_hash": file_hash,
                "status": 201,
            },
        )
    else:
        celery_task_id = job.celery_task_id
        logger.info(
            "project paper PDF upload reused existing job",
            extra={
                "event": "ingestion_upload_reused",
                "project_id": project_id,
                "paper_id": paper_id,
                "ingestion_job_id": job.id,
                "status": 200,
            },
        )
    payload = PaperIngestionJobSerializer(job).data
    payload["celery_task_id"] = celery_task_id
    _publish_ingestion_event(project_id, "ingestion_upload_queued", {
        "job_id": job.id, "paper_id": paper_id,
        "deduplicated": not created,
        "reused": job.status == "embedded",
        "fulltext_ready": payload.get("fulltext_ready"),
    })
    # Tasks 5.1: 201 on creation, 200 + deduplicated=true when the request
    # converged on an existing job — the caller can tell a NEW build from a
    # reused one without treating reuse as a fresh success.
    if created:
        return Response(payload, status=status.HTTP_201_CREATED)
    payload["deduplicated"] = True
    return Response(payload, status=status.HTTP_200_OK)


@api_view(["POST"])
def project_paper_ingest(request, project_id: int, paper_id: int):
    link = _get_project_paper_link(project_id, paper_id)
    if not link:
        return Response({"error": "项目论文不存在"}, status=status.HTTP_404_NOT_FOUND)
    source_url = (request.data.get("pdf_url") or link.paper.pdf_url or "").strip()
    if not source_url:
        return Response({"error": "论文没有可入库的 pdf_url"}, status=status.HTTP_400_BAD_REQUEST)

    # Tasks5-CX-01: URL ingest goes through IngestionService like upload —
    # scoped request key, job get-or-create, global build claim. Source
    # identity in keys/logs is a DIGEST, never the raw URL. The actual safe
    # download stays in the worker (SafePdfFetcher).
    import hashlib

    from .ingestion_service import IngestionService

    digest = hashlib.sha256(source_url.encode("utf-8")).hexdigest()[:64]
    service = IngestionService()
    job, created = service.get_or_create_job(
        link.project,
        link.paper,
        idempotency_key=service.request_key(project_id, paper_id, digest),
        source_kind="url",
        source_url=source_url,
        file_name=f"paper-{paper_id}-{digest[:8]}.pdf",
    )
    # source identity for the GLOBAL build is the raw URL — claim_build digests
    # it internally, exactly like the worker does (Tasks5-CX-01)
    version = service.claim_build(job, source_url)
    if version.status == "active":
        # Tasks5-CX-01: reuse the active global build — no enqueue/parse/write
        job.status = "embedded"
        job.chunk_count = version.chunk_count
        job.save(update_fields=["status", "chunk_count", "updated_at"])
        created = False
    if created:
        try:
            _enqueue_ingestion_job(job)
        except Exception:
            # eager environments may run the task synchronously; the job state
            # reflects the outcome — the response stays a queue acceptance
            pass
        job.refresh_from_db()
    logger.info(
        "project paper URL ingestion queued",
        extra={
            "event": "ingestion_url_queued",
            "project_id": project_id,
            "paper_id": paper_id,
            "ingestion_job_id": job.id,
            "source_hash": digest,
            "status": 201 if created else 200,
        },
    )
    payload = PaperIngestionJobSerializer(job).data
    _publish_ingestion_event(project_id, "ingestion_url_queued", {
        "job_id": job.id, "paper_id": paper_id,
        "source_hash": digest,
        "deduplicated": not created,
        "reused": job.status == "embedded",
        "fulltext_ready": payload.get("fulltext_ready"),
    })
    if created:
        return Response(payload, status=status.HTTP_201_CREATED)
    payload["deduplicated"] = True
    return Response(payload, status=status.HTTP_200_OK)


@api_view(["GET"])
def project_ingestion_jobs(request, project_id: int):
    if not ResearchProject.objects.filter(id=project_id).exists():
        return Response({"error": "项目不存在"}, status=status.HTTP_404_NOT_FOUND)
    jobs = PaperIngestionJob.objects.select_related("paper").filter(project_id=project_id)
    return Response(PaperIngestionJobSerializer(jobs, many=True).data)


@api_view(["POST"])
def project_ingestion_job_retry(request, project_id: int, job_id: int):
    """Tasks 5.1 / Tasks5-CX-04: scoped retry of a FAILED + RETRYABLE job.

    Only own failed jobs with retryable=true are re-queued (202). Own failed
    but non-retryable jobs, foreign jobs, non-failed jobs and nonexistent ids
    share ONE uniform safe rejection shape (404) — indistinguishable
    not-found semantics. Retry never flips a non-retryable job. If the queue
    is unavailable the job stays in an explicit pending state and a stable
    enqueue_failed error is returned instead of pretending success."""
    job = PaperIngestionJob.objects.filter(
        id=job_id, project_id=project_id).first()
    if job is None or job.status != "failed" or not job.retryable:
        return Response({"error": "job_not_found"}, status=status.HTTP_404_NOT_FOUND)
    job.status = "pending"
    job.error_message = ""
    job.error_code = ""
    job.save(update_fields=["status", "error_message", "error_code",
                            "updated_at"])
    try:
        _enqueue_ingestion_job(job)
    except Exception:
        job.refresh_from_db()
        if job.status == "failed":
            # an eager run executed the task and failed the job itself — the
            # ACCEPT reflects the queue transition; the job carries the state
            return Response(PaperIngestionJobSerializer(job).data,
                            status=status.HTTP_202_ACCEPTED)
        # real enqueue failure (broker unavailable): stable safe rejection,
        # job stays pending for a later retry
        return Response({"error": "enqueue_failed"},
                        status=status.HTTP_503_SERVICE_UNAVAILABLE)
    job.refresh_from_db()
    _publish_ingestion_event(project_id, "ingestion_job_retried", {
        "job_id": job.id, "paper_id": job.paper_id,
        "retryable": job.retryable,
        "fulltext_ready": PaperIngestionJobSerializer(job).data.get("fulltext_ready"),
    })
    logger.info(
        "project paper ingestion job retried",
        extra={
            "event": "ingestion_job_retried",
            "project_id": project_id,
            "paper_id": job.paper_id,
            "ingestion_job_id": job.id,
            "status": 202,
        },
    )
    return Response(PaperIngestionJobSerializer(job).data,
                    status=status.HTTP_202_ACCEPTED)


@api_view(["POST"])
def project_search_add(request, project_id: int):
    if not ResearchProject.objects.filter(id=project_id).exists():
        return Response({"error": "项目不存在"}, status=status.HTTP_404_NOT_FOUND)
    query = (request.data.get("query") or "").strip()
    max_results = int(request.data.get("max_results") or 5)
    if not query:
        return Response({"error": "query 不能为空"}, status=status.HTTP_400_BAD_REQUEST)
    from datasources.registry import search

    papers = async_to_sync(search)(query, max_results=max_results)
    add_result = async_to_sync(add_papers_to_project)(
        project_id, papers, f"User search-add: {query[:120]}"
    )
    return Response({"query": query, "results": papers, **add_result}, status=status.HTTP_201_CREATED)


@api_view(["GET", "POST"])
def project_reports(request, project_id: int):
    started = time.perf_counter()
    project = ResearchProject.objects.filter(id=project_id).first()
    if not project:
        logger.info(
            "project reports project not found",
            extra={"event": "project_reports_not_found", "project_id": project_id, "status": 404},
        )
        return Response({"error": "项目不存在"}, status=status.HTTP_404_NOT_FOUND)
    if request.method == "GET":
        reports = ReportVersion.objects.filter(project=project)
        logger.info(
            "project reports listed",
            extra={
                "event": "project_reports_listed",
                "project_id": project.id,
                "report_count": reports.count(),
                "duration_ms": round((time.perf_counter() - started) * 1000, 2),
                "status": 200,
            },
        )
        return Response(ReportVersionSerializer(reports, many=True).data)
    serializer = ReportVersionSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    report = serializer.save(project=project)
    logger.info(
        "project report saved",
        extra={
            "event": "project_report_saved",
            "project_id": project.id,
            "report_id": report.id,
            "report_source": report.source,
            "title_preview": report.title[:120],
            "content_chars": len(report.content),
            "duration_ms": round((time.perf_counter() - started) * 1000, 2),
            "status": 201,
        },
    )
    return Response(ReportVersionSerializer(report).data, status=status.HTTP_201_CREATED)


@api_view(["GET"])
def project_citation_graph(request, project_id: int):
    if not ResearchProject.objects.filter(id=project_id).exists():
        return Response({"error": "项目不存在"}, status=status.HTTP_404_NOT_FOUND)
    from agent.context import create_context
    from agent.project_tools import execute_project_tool

    result = async_to_sync(execute_project_tool)(
        create_context(project_id), "get_project_citation_graph", {}
    )
    return Response(result.get("graph", {"nodes": [], "edges": []}))


@api_view(["GET"])
def project_paper_connection_path(request, project_id: int, paper_a_id: int, paper_b_id: int):
    """两篇论文之间的引用连接路径（缝合 Inciteful Literature Connector）。"""
    if not ResearchProject.objects.filter(id=project_id).exists():
        return Response({"error": "项目不存在"}, status=status.HTTP_404_NOT_FOUND)
    from citation.graph_build import build_similarity_graph
    from citation.paths import find_connection_path
    from api.models import ProjectPaper

    papers = [
        row.paper
        for row in ProjectPaper.objects.select_related("paper")
        .filter(project_id=project_id).exclude(status="excluded")
    ]
    if paper_a_id not in {p.id for p in papers} or paper_b_id not in {p.id for p in papers}:
        return Response({"error": "论文不在项目范围内"}, status=status.HTTP_404_NOT_FOUND)
    G = build_similarity_graph(papers)
    result = find_connection_path(G, paper_a_id, paper_b_id)
    return Response(result)


@api_view(["GET", "POST"])
def project_paper_relations(request, project_id: int):
    """论文间引用语境标注（支持/反对/提及，对齐 Scite smart citations）。

    GET：返回项目内已分析的引用关系。
    POST：扫描项目论文的 referenced_works，对能匹配到项目库内的引用关系
          用 DeepSeek 做轻量分类（基于双方摘要），结果缓存到 PaperRelation。
    """
    if not ResearchProject.objects.filter(id=project_id).exists():
        return Response({"error": "项目不存在"}, status=status.HTTP_404_NOT_FOUND)
    project = ResearchProject.objects.get(id=project_id)

    if request.method == "GET":
        relations = PaperRelation.objects.filter(project=project).select_related(
            "citing_paper", "cited_paper"
        )
        return Response([
            {
                "citing_paper_id": r.citing_paper_id,
                "citing_title": r.citing_paper.title[:80],
                "cited_paper_id": r.cited_paper_id,
                "cited_title": r.cited_paper.title[:80],
                "label": r.label,
                "context": r.context,
                "confidence": r.confidence,
            }
            for r in relations
        ])

    # POST：分析项目内的引用语境
    from django.utils import timezone

    paper_rows = list(
        ProjectPaper.objects.select_related("paper")
        .filter(project_id=project_id)
        .exclude(status="excluded")
    )
    # 建立 openalex_id -> paper 的索引（用于匹配 referenced_works）
    id_to_paper = {row.paper.openalex_id: row.paper for row in paper_rows if row.paper.openalex_id}
    # 归一化 openalex id（去 URL 前缀）
    def _norm_oid(oid):
        return oid.rsplit("/", 1)[-1] if oid and "/" in oid else (oid or "")

    id_to_paper_norm = {_norm_oid(k): v for k, v in id_to_paper.items()}

    analyzed = 0
    for row in paper_rows:
        citing = row.paper
        for ref_oid in (citing.referenced_works or []):
            cited = id_to_paper_norm.get(_norm_oid(ref_oid))
            if not cited or cited.id == citing.id:
                continue
            rel, _created = PaperRelation.objects.get_or_create(
                project=project, citing_paper=citing, cited_paper=cited,
                defaults={"label": "unanalyzed"},
            )
            if rel.label != "unanalyzed":
                continue  # 已分析则跳过（缓存）
            label, context, confidence = _classify_citation_context(citing, cited)
            rel.label = label
            rel.context = context
            rel.confidence = confidence
            rel.analyzed_at = timezone.now()
            rel.save()
            analyzed += 1
    logger.info(
        "paper relations analyzed",
        extra={"event": "paper_relations_analyzed", "project_id": project_id, "analyzed": analyzed},
    )
    return Response({"analyzed": analyzed}, status=status.HTTP_200_OK)


def _classify_citation_context(citing, cited) -> tuple[str, str, float]:
    """用 DeepSeek 对单条引用关系做轻量分类（基于双方摘要）。

    返回 (label, context, confidence)。缺 key 或 LLM 失败时返回 mentioning 兜底。
    """
    import json
    from llm.deepseek import DeepSeekClient

    prompt = (
        "判断以下引用关系的语境。论文 A 在参考文献中列出了论文 B。\n"
        f"论文 A《{citing.title}》摘要：{(citing.abstract or '无摘要')[:300]}\n"
        f"论文 B《{cited.title}》摘要：{(cited.abstract or '无摘要')[:300]}\n\n"
        "输出严格 JSON：{\"label\": \"supporting|contradicting|mentioning\", "
        "\"reason\": \"一句话说明判断依据（约30字）\"}\n"
        "- supporting：A 的方法/结论基于或支持 B\n"
        "- contradicting：A 反驳或对比 B\n"
        "- mentioning：仅提及，无明确支持/反对\n只输出 JSON。"
    )
    try:
        client = DeepSeekClient()
        r = client.complete(
            [{"role": "user", "content": prompt}],
            thinking=False,
            max_tokens=150,
            response_format={"type": "json_object"},
        )
        data = json.loads(r["content"])
        label = data.get("label", "mentioning")
        if label not in {"supporting", "contradicting", "mentioning"}:
            label = "mentioning"
        return label, str(data.get("reason", ""))[:200], 0.7
    except Exception:
        return "mentioning", "", 0.0


@api_view(["GET"])
def project_runs(request, project_id: int):
    if not ResearchProject.objects.filter(id=project_id).exists():
        return Response({"error": "项目不存在"}, status=status.HTTP_404_NOT_FOUND)
    limit = max(1, min(50, int(request.query_params.get("limit") or 12)))
    runs = (
        ProjectRun.objects.prefetch_related("events")
        .filter(project_id=project_id)
        .order_by("-created_at", "-id")[:limit]
    )
    return Response(ProjectRunSerializer(runs, many=True).data)


@api_view(["POST"])
def project_research_expand_workflow(request, project_id: int):
    project = ResearchProject.objects.filter(id=project_id).first()
    if not project:
        return Response({"error": "项目不存在"}, status=status.HTTP_404_NOT_FOUND)
    question = (request.data.get("question") or "").strip()
    if not question:
        return Response({"error": "question 不能为空"}, status=status.HTTP_400_BAD_REQUEST)
    run = ProjectRun.objects.create(
        project=project,
        kind="workflow",
        status="pending",
        question=question,
    )
    from .tasks import run_research_expand_workflow_task

    result = run_research_expand_workflow_task.delay(run.id)
    # §30.1: unified EventPublisher — the question is never persisted; only
    # the celery id and publisher-derived correlation ids are stored.
    from agent.event_publisher import EventPublisher

    EventPublisher(
        run=run,
        request_id=getattr(request, "paperlens_request_id", ""),
    ).publish("workflow_queued", {"celery_task_id": result.id or ""})
    logger.info(
        "research expansion workflow queued",
        extra={
            "event": "workflow_queued",
            "project_id": project.id,
            "run_id": run.id,
            "celery_task_id": result.id,
            "status": 201,
        },
    )
    run.refresh_from_db()
    return Response(ProjectRunSerializer(run).data, status=status.HTTP_201_CREATED)


@api_view(["GET", "POST"])
def project_chat(request, project_id: int):
    if not ResearchProject.objects.filter(id=project_id).exists():
        return Response({"error": "项目不存在"}, status=status.HTTP_404_NOT_FOUND)
    if request.method == "GET":
        sessions = ChatSession.objects.filter(project_id=project_id).order_by("-updated_at", "-id")
        return Response(ChatSessionSerializer(sessions, many=True).data)
    message = (request.data.get("message") or "").strip()
    session_id = request.data.get("session_id")
    if not message:
        return Response({"error": "message 不能为空"}, status=status.HTTP_400_BAD_REQUEST)
    harness = ProjectAgentHarness(
        project_id,
        int(session_id) if session_id else None,
        use_llm=settings.PROJECT_CHAT_LIVE_LLM,
    )
    result = async_to_sync(harness.run)(message)
    return Response(result, status=status.HTTP_201_CREATED)


async def project_chat_stream(request, project_id: int, session_id: int):
    if not await ResearchProject.objects.filter(id=project_id).aexists():
        return StreamingHttpResponse(
            iter([_sse("error", {"message": "项目不存在"})]),
            content_type="text/event-stream",
        )
    message = request.GET.get("message", "").strip()
    if not message:
        return StreamingHttpResponse(
            iter([_sse("error", {"message": "message 不能为空"})]),
            content_type="text/event-stream",
        )

    async def events():
        yield b": connected\n\n"
        harness = ProjectAgentHarness(
            project_id,
            session_id if session_id > 0 else None,
            use_llm=settings.PROJECT_CHAT_LIVE_LLM,
        )
        async for event in harness.stream(message):
            yield _sse(event["event"], event["data"])

    resp = StreamingHttpResponse(events(), content_type="text/event-stream")
    resp["Cache-Control"] = "no-cache"
    resp["X-Accel-Buffering"] = "no"
    return resp


@api_view(["POST"])
def demo_seed(request):
    result = seed_demo_project(request.data.get("title") or None)
    return Response(result, status=status.HTTP_201_CREATED)
