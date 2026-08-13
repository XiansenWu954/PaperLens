"""Auditable project Agent tools.

These are the only safe autonomous tools exposed to project chat. Destructive
operations such as deleting papers are intentionally absent.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from typing import Any

from asgiref.sync import sync_to_async

from .context import ToolExecutionContext
from .evidence import EvidenceEnvelope, MetadataEvidence, make_evidence_id
from .scope import ProjectScopeResolver

logger = logging.getLogger(__name__)

PROJECT_RAG_RCS_TIMEOUT_SECONDS = float(os.environ.get("PAPERLENS_PROJECT_RAG_RCS_TIMEOUT_SECONDS", "10"))


PROJECT_AGENT_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "query_project_rag",
            "description": "Search the current project's paper chunks/evidence and return grounded snippets.",
            "parameters": {
                "type": "object",
                "properties": {
                    "question": {"type": "string"},
                    "k": {"type": "integer", "minimum": 1, "maximum": 20, "default": 6},
                },
                "required": ["question"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_papers",
            "description": "Search free CS paper sources. DBLP is included by default.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "max_results": {"type": "integer", "minimum": 1, "maximum": 20, "default": 5},
                },
                "required": ["query"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "add_papers_to_project",
            "description": "Add searched papers to a project library. This is non-destructive.",
            "parameters": {
                "type": "object",
                "properties": {
                    "papers": {"type": "array", "items": {"type": "object"}},
                    "reason": {"type": "string"},
                },
                "required": ["papers"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_project_papers",
            "description": "List papers in a project library.",
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_project_citation_graph",
            "description": "Build a project-scoped citation graph from project papers.",
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "draft_report_section",
            "description": "Draft a report section from project evidence without overwriting report versions.",
            "parameters": {
                "type": "object",
                "properties": {
                    "question": {"type": "string"},
                },
                "required": ["question"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_paper_section",
            "description": "Read a specific section or full text of a paper in the project library. Use when the user asks about details/methods/results of a specific paper, or wants to read a section.",
            "parameters": {
                "type": "object",
                "properties": {
                    "paper_id": {"type": "integer", "minimum": 1, "description": "The paper ID to read"},
                    "section": {"type": "string", "description": "Optional section name to filter (e.g. 'method', 'results'). Returns all chunks if omitted."},
                    "max_chunks": {"type": "integer", "minimum": 1, "maximum": 50, "default": 10},
                },
                "required": ["paper_id"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "compare_papers",
            "description": (
                "Compare 2-5 project papers by retrieving full-text evidence for EACH paper "
                "via per-paper Hybrid RAG, then summarizing method/dataset/result differences. "
                "Use this for any comparison or contrast question. Each paper gets its own "
                "evidence chunks; papers without fulltext are flagged as evidence_gap. "
                "Do NOT compare using only abstracts."
            ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "paper_ids": {"type": "array", "items": {"type": "integer", "minimum": 1},
                                      "minItems": 2, "maxItems": 5, "description": "Paper IDs to compare (2-5 papers)"},
                        "question": {"type": "string", "description": "What aspect to compare (e.g. 'methods', 'performance', 'approach')"},
                    },
                    "required": ["paper_ids"],
                    "additionalProperties": False,
                },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "export_bibtex",
            "description": "Export the project paper library as BibTeX text. Use when the user asks to export citations or get a .bib file.",
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
                "additionalProperties": False,
            },
        },
    },
]


def available_tool_names() -> list[str]:
    return [tool["function"]["name"] for tool in PROJECT_AGENT_TOOLS]


# Authorization fields are server-bound (Task 2.2/2.5): they never appear in the
# model-visible schema, and the executor strips any smuggled copies.
AUTH_ARGUMENT_FIELDS = {"project_id", "run_id", "session_id", "actor"}


def strip_auth_fields(arguments: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in (arguments or {}).items() if k not in AUTH_ARGUMENT_FIELDS}


async def execute_project_tool(
    context: ToolExecutionContext, name: str, arguments: dict[str, Any]
) -> dict[str, Any]:
    """Auditable project tool executor (Task 2.1/2.5).

    The project identity comes EXCLUSIVELY from the frozen context. Auth fields
    in arguments are stripped defensively (they must not reach the tools).
    """
    started = time.perf_counter()
    stripped = strip_auth_fields(arguments)
    safe_args = _safe_arguments(stripped)
    logger.info(
        "project tool started",
        extra={
            "event": "project_tool_started",
            "tool_name": name,
            **context.to_audit(),
            **safe_args,
        },
    )
    project_id = context.project_id
    try:
        if name == "query_project_rag":
            result = await query_project_rag(
                project_id,
                str(stripped["question"]),
                int(stripped.get("k", 6)),
            )
        elif name == "search_papers":
            result = await search_papers(
                str(stripped["query"]),
                int(stripped.get("max_results", 5)),
            )
        elif name == "add_papers_to_project":
            result = await add_papers_to_project(
                project_id,
                list(stripped.get("papers") or []),
                str(stripped.get("reason") or "Agent selected relevant papers."),
            )
        elif name == "list_project_papers":
            result = await list_project_papers(project_id)
        elif name == "get_project_citation_graph":
            result = await get_project_citation_graph(project_id)
        elif name == "draft_report_section":
            result = await draft_report_section(
                project_id,
                str(stripped["question"]),
            )
        elif name == "read_paper_section":
            result = await read_paper_section(
                project_id,
                int(stripped["paper_id"]),
                stripped.get("section"),
                int(stripped.get("max_chunks", 10)),
            )
        elif name == "compare_papers":
            result = await compare_papers(
                project_id,
                list(stripped.get("paper_ids") or []),
                stripped.get("question", ""),
            )
        elif name == "export_bibtex":
            result = await export_bibtex(project_id)
        else:
            result = {"error": f"unknown tool {name}"}
        duration_ms = int((time.perf_counter() - started) * 1000)
        logger.info(
            "project tool completed",
            extra={
                "event": "project_tool_completed",
                "tool_name": name,
                "duration_ms": duration_ms,
                **context.to_audit(),
                **safe_args,
                **_result_summary(name, result),
            },
        )
        return result
    except Exception as exc:
        # §30.3: never log the raw exception message — type + safe frames +
        # digest only.
        from .events import error_hash, safe_stack_frames

        duration_ms = int((time.perf_counter() - started) * 1000)
        logger.error(
            "project tool failed",
            extra={
                "event": "project_tool_failed",
                "tool_name": name,
                "duration_ms": duration_ms,
                "error": exc.__class__.__name__,
                "error_hash": error_hash(exc),
                "stack_frames": safe_stack_frames(exc),
                **context.to_audit(),
                **safe_args,
            },
        )
        raise


async def search_papers(query: str, max_results: int = 5) -> dict[str, Any]:
    from datasources.registry import search

    fetch_limit = min(20, max(max_results, max_results * 3))
    papers = await search(query, max_results=fetch_limit)
    papers = _rank_and_filter_search_results(query, papers, max_results)
    return {"papers": papers, "count": len(papers)}


async def add_papers_to_project(project_id: int, papers: list[dict], reason: str = "") -> dict[str, Any]:
    """Add papers to a project with Tasks 5.3 auto-queue semantics.

    Result collections separate the outcomes so the frontend never mistakes a
    metadata membership for full-text ingestion:
    - added:          memberships created/refreshed (all papers);
    - queued:         papers WITH a trusted HTTPS PDF URL auto-queued for
                      ingestion — at most THREE per call (Tasks 5.3);
    - reused:         papers whose paper ALREADY has an active index or a
                      completed job — never re-queued;
    - deferred:       papers with a PDF URL beyond the three-per-call cap —
                      no job is created in this call;
    - upload_required: papers WITHOUT a PDF URL — ingestion needs an upload.

    Ingestion NEVER runs in the Agent process: only a scoped job get-or-create
    + build claim + Celery enqueue happen here (enqueue failures in eager
    test environments never break the membership result).
    """
    from api.ingestion_service import IngestionService
    from api.models import PaperIngestionJob, ProjectPaper, ResearchProject
    from papers.models import upsert_paper
    from rag.models import PaperIndexVersion

    def _add() -> dict:
        project = ResearchProject.objects.get(id=project_id)
        added: list[dict] = []
        queued: list[dict] = []
        reused: list[dict] = []
        deferred: list[dict] = []
        upload_required: list[dict] = []
        service = IngestionService()
        for payload in papers:
            paper = upsert_paper(payload)
            link, created = ProjectPaper.objects.get_or_create(
                project=project,
                paper=paper,
                defaults={
                    "status": "candidate",
                    "source_reason": reason,
                    "added_by": "agent",
                },
            )
            if not created and reason:
                link.source_reason = reason
                link.save(update_fields=["source_reason", "updated_at"])
            entry = {"paper_id": paper.id, "title": paper.title,
                     "created": created}
            added.append(entry)

            pdf_url = str(payload.get("pdf_url") or paper.pdf_url or "").strip()
            if not pdf_url:
                upload_required.append({**entry, "reason": "missing_url"})
                continue
            if not _is_https_candidate_url(pdf_url):
                # Tasks5-CX-06: only HTTPS candidate URLs (no userinfo) may be
                # auto-queued; anything else needs a safe source and is never
                # enqueued or turned into a job.
                upload_required.append({**entry, "reason": "unsafe_url"})
                continue
            if PaperIndexVersion.objects.filter(
                    paper=paper, status="active").exists() or \
               PaperIngestionJob.objects.filter(
                    paper=paper, status="embedded").exists():
                reused.append({**entry, "reason": "already_indexed"})
                continue
            if len(queued) >= 3:
                deferred.append({**entry, "reason": "queue_limit"})
                continue
            # scoped job get-or-create + global build claim + Celery enqueue;
            # the file_name is a SAFE digest name — never derived from the
            # raw URL path (Tasks5-CX-07)
            try:
                job, _job_created = service.get_or_create_job(
                    project, paper,
                    idempotency_key=service.request_key(
                        project.id, paper.id, _digest_source(pdf_url)),
                    source_kind="agent",
                    source_url=pdf_url,
                    file_name=f"paper-{paper.id}-{_digest_source(pdf_url)[:8]}.pdf",
                )
                service.claim_build(job, _digest_source(pdf_url))
                from api.tasks import ingest_paper_pdf_task

                try:
                    ingest_paper_pdf_task.delay(job.id)
                except Exception:
                    # eager environments / unavailable worker must not break
                    # the membership result; the job stays pending for the
                    # worker or a later retry
                    pass
                queued.append({**entry, "ingestion_job_id": job.id})
                _publish_ingestion_event(project_id, "ingestion_agent_queued", {
                    "job_id": job.id, "paper_id": paper.id, "reason": "auto",
                })
            except Exception as exc:
                logger.warning(
                    "add_papers_to_project enqueue skipped",
                    extra={
                        "event": "agent_add_enqueue_skipped",
                        "project_id": project.id,
                        "paper_id": paper.id,
                        "error": exc.__class__.__name__,
                        "status": "skipped",
                    },
                )
                deferred.append(entry)
        return {
            "added": added,
            "count": len(added),
            "queued": queued,
            "reused": reused,
            "deferred": deferred,
            "upload_required": upload_required,
        }

    return await sync_to_async(_add)()


def _is_https_candidate_url(url: str) -> bool:
    """Tasks5-CX-06: HTTPS-only, no userinfo — the ONLY URLs eligible for
    auto-queueing. Never logs the URL itself."""
    from urllib.parse import urlparse

    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    return (
        parsed.scheme == "https"
        and parsed.username is None
        and parsed.password is None
        and bool(parsed.hostname)
    )


def _publish_ingestion_event(
    project_id: int, event_type: str, payload: dict
) -> None:
    """Tasks 5.4: agent auto-queue summaries flow through the SAME
    EventPublisher sanitize boundary (schema allowlist; never persisted —
    the agent process has no run)."""
    from agent.event_publisher import EventPublisher

    EventPublisher(project_id=project_id, persist=False).publish(
        event_type, payload)


def _digest_source(value: str) -> str:
    import hashlib

    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:64]


async def list_project_papers(project_id: int) -> dict[str, Any]:
    resolver = ProjectScopeResolver(project_id)

    def _list() -> list[dict]:
        rows = resolver.library_memberships()
        return [
            {
                "paper_id": row.paper_id,
                "title": row.paper.title,
                "year": row.paper.year,
                "venue": row.paper.venue.name if row.paper.venue_id else "",
                "citation_count": row.paper.citation_count,
                "status": row.status,
                "doi": row.paper.doi,
                "arxiv_id": row.paper.arxiv_id,
                "pdf_url": row.paper.pdf_url,
            }
            for row in rows
        ]

    papers = await sync_to_async(_list)()
    return {"papers": papers, "count": len(papers)}


async def project_paper_ids(project_id: int) -> list[int]:
    return await sync_to_async(ProjectScopeResolver(project_id).paper_ids)()


async def query_project_rag(project_id: int, question: str, k: int = 6) -> dict[str, Any]:
    from rag.models import Text

    resolver = ProjectScopeResolver(project_id)
    paper_ids = await sync_to_async(resolver.paper_ids)()
    if not paper_ids:
        return {"evidence": [], "fallback": "项目论文库为空。"}

    chunked_paper_ids = await sync_to_async(
        lambda: set(
            resolver.chunks(paper_ids=paper_ids)
            .values_list("paper_id", flat=True)
            .distinct()
        )
    )()
    if not chunked_paper_ids:
        metadata_evidence = await sync_to_async(_metadata_evidence)(project_id, paper_ids, question, k)
        return {"evidence": metadata_evidence, "fallback": "项目论文尚未完成全文向量入库，已使用元数据回答。"}

    from rag.retrieval import retrieve_evidence

    try:
        evidences = await asyncio.wait_for(
            retrieve_evidence(question, paper_ids=paper_ids, k=k),
            timeout=PROJECT_RAG_RCS_TIMEOUT_SECONDS,
        )
    except TimeoutError:
        metadata_evidence = await sync_to_async(_metadata_evidence)(project_id, paper_ids, question, k)
        logger.warning(
            "project RAG RCS timed out; using metadata evidence",
            extra={
                "event": "project_rag_rcs_timeout",
                "project_id": project_id,
                "question_preview": question[:120],
                "timeout_seconds": PROJECT_RAG_RCS_TIMEOUT_SECONDS,
            },
        )
        return {
            "evidence": metadata_evidence,
            "fallback": "全文 RAG 评分超时，已使用项目论文元数据回答。",
        }

    fulltext_evidence = []
    for ev in evidences:
        item = EvidenceEnvelope(
            evidence_id=make_evidence_id(project_id, ev.text.paper_id, ev.text.id,
                                    ev.text.content_hash, ev.text.embedding_version),
            project_id=project_id,
            paper_id=ev.text.paper_id,
            chunk_id=ev.text.id,
            content_hash=ev.text.content_hash,
            excerpt=ev.summary,
            page_start=ev.text.page_start,
            page_end=ev.text.page_end,
            section=ev.text.section,
            retrieval_sources=("hybrid_rag",),
            retrieval_scores={"rcs": float(ev.score)},
            embedding_version=ev.text.embedding_version,
            chunk_index=ev.text.chunk_index,
            title=ev.text.paper.title if ev.text.paper_id else "",
            summary=ev.summary,
            citation=ev.citation_key,
            source_marker=ev.citation_key or ev.text.docname,
            score=float(ev.score),
            docname=ev.text.docname,
        ).to_dict()
        fulltext_evidence.append(item)
    seen_papers = {item["paper_id"] for item in fulltext_evidence if item.get("paper_id")}
    combined = list(fulltext_evidence)
    fallback = ""
    missing_chunk_paper_ids = [paper_id for paper_id in paper_ids if paper_id not in chunked_paper_ids]
    if missing_chunk_paper_ids:
        metadata_evidence = await sync_to_async(_metadata_evidence)(
            project_id, missing_chunk_paper_ids, question, k)
        for item in metadata_evidence:
            if len(combined) >= k:
                break
            if item.get("paper_id") in seen_papers:
                continue
            combined.append(item)
        fallback = "部分项目论文尚未完成全文向量入库，已补充元数据证据。"
    return {
        "evidence": combined,
        "fallback": fallback,
    }


async def get_project_citation_graph(project_id: int) -> dict[str, Any]:
    resolver = ProjectScopeResolver(project_id)

    def _build() -> dict:
        papers = resolver.graph_papers()
        if len(papers) < 2:
            return {"nodes": [], "edges": []}
        from citation.analyze import label_nodes
        from citation.graph_build import build_similarity_graph
        from citation.visualize import to_vis_data

        graph = build_similarity_graph(papers)
        labels = label_nodes(graph)
        return to_vis_data(graph, labels)

    return {"graph": await sync_to_async(_build)()}


async def draft_report_section(project_id: int, question: str) -> dict[str, Any]:
    rag = await query_project_rag(project_id, question, k=8)
    evidence = rag.get("evidence", [])
    lines = [f"## {question}", ""]
    if not evidence:
        lines.append("当前项目库还没有足够证据生成章节。")
    for item in evidence:
        source = item.get("citation") or item.get("title") or item.get("docname") or f"paper {item.get('paper_id')}"
        lines.append(f"- {item.get('summary', '')} [cite:{source}]")
    return {"section": "\n".join(lines), "evidence": evidence}


async def read_paper_section(
    project_id: int, paper_id: int, section: str | None = None, max_chunks: int = 10
) -> dict[str, Any]:
    """读取项目库内某篇论文的指定章节/全文 chunks。

    Scoped not-found: foreign / excluded / unlinked / nonexistent papers all
    produce the SAME stable result shape (no existence signal, Task 2.4).
    """
    resolver = ProjectScopeResolver(project_id)

    def _read() -> list[dict] | None:
        paper = resolver.project_paper(paper_id)
        if paper is None:
            return None
        qs = resolver.chunks(paper_ids=[paper_id])
        if section:
            qs = qs.filter(section__icontains=section)
        return [
            {
                "chunk_index": t.chunk_index,
                "section": t.section,
                "page_start": t.page_start,
                "content": t.content[:1500],
                "citation_key": t.citation_key,
                "evidence_id": make_evidence_id(project_id, paper_id, t.id,
                                    t.content_hash, t.embedding_version),
                "project_id": project_id,
                "paper_id": paper_id,
                "chunk_id": t.id,
                "content_hash": t.content_hash,
                "embedding_version": t.embedding_version,
                "evidence_type": "fulltext",
                "excerpt": t.content[:1500],
                "page_end": t.page_end,
                "retrieval_sources": ["project_read"],
                "retrieval_scores": {},
                "title": paper.title,
                "source_marker": t.citation_key,
            }
            for t in qs.order_by("chunk_index")[:max_chunks]
        ]

    chunks = await sync_to_async(_read)()
    if chunks is None:
        return {
            "paper_id": paper_id,
            "chunks": [],
            "note": "该论文尚未完成全文入库，可用 RAG 元数据或检索补充。",
        }
    if not chunks:
        return {"paper_id": paper_id, "chunks": [], "note": "该论文尚未完成全文入库，可用 RAG 元数据或检索补充。"}
    return {"paper_id": paper_id, "chunk_count": len(chunks), "chunks": chunks}


async def compare_papers(
    project_id: int, paper_ids: list[int], question: str = ""
) -> dict[str, Any]:
    """对比多篇论文:对每篇单独执行项目范围 Hybrid RAG,取全文证据后均衡汇总。

    每篇默认取 2-3 个全文 chunk(不是摘要),确保比较两侧都有证据覆盖。
    任一目标论文没有全文证据时明确返回 evidence_gap;metadata fallback
    (仅摘要、未入库全文)被明确标记,不能描述为已阅读全文。
    """
    from api.models import PaperIngestionJob
    from rag.retrieval import hybrid_retrieve_texts
    from rag.models import Text

    CHUNKS_PER_PAPER = 3

    def _resolve_papers() -> list[dict]:
        resolver = ProjectScopeResolver(project_id)
        out = []
        for pid in paper_ids:
            paper = resolver.project_paper(pid)
            if paper is None:
                # foreign / excluded / unlinked / nonexistent — same drop-out
                continue
            # §21.3: has_fulltext reflects the resolver's CURRENT ACTIVE chunks
            # only — a stale-only paper must not be labeled full-text available.
            has_fulltext = resolver.chunks(paper_ids=[pid], active_only=True).exists()
            # 是否曾尝试入库?
            job = PaperIngestionJob.objects.filter(paper=paper).order_by("-updated_at").first()
            ingest_status = job.status if job else "never_ingested"
            out.append({
                "paper_id": paper.id,
                "title": paper.title,
                "abstract": paper.abstract or "",
                "year": paper.year,
                "arxiv_id": paper.arxiv_id,
                "citation_count": paper.citation_count,
                "has_fulltext": has_fulltext,
                "ingest_status": ingest_status,
            })
        return out

    papers_meta = await sync_to_async(_resolve_papers)()
    if len(papers_meta) < 2:
        return {"error": "需要至少 2 篇在项目库内的论文才能对比"}

    aspect = (question or "general").strip()
    results: list[dict] = []
    evidence_gaps: list[dict] = []
    for pm in papers_meta:
        pid = pm["paper_id"]
        if not pm["has_fulltext"]:
            # 无全文证据:只给 metadata,明确标记 fallback;typed metadata evidence
            # 随结果输出,供 typed collection 与 capability policy 消费。
            abstract = pm.get("abstract") or ""
            entry = {
                **pm,
                "evidence_source": "metadata_fallback",
                "evidence_note": "该论文未入库全文,仅提供元数据;不可描述为已阅读全文。",
                "chunks": [],
                "evidence": [MetadataEvidence(
                    project_id=project_id,
                    paper_id=pm["paper_id"],
                    title=pm["title"],
                    summary=(abstract[:420] or f"元数据证据：{pm['title']}"),
                    citation=pm["title"],
                    source_marker=pm["title"],
                ).to_dict()],
            }
            evidence_gaps.append({"paper_id": pid, "title": pm["title"], "reason": "no fulltext chunks"})
            results.append(entry)
            continue
        # 对该论文单独做项目范围 hybrid RAG(aspect + title 作为查询,提高该论文证据召回)
        per_paper_query = f"{pm['title']} {aspect}".strip()
        try:
            texts = await hybrid_retrieve_texts(per_paper_query, paper_ids=[pid], final_k=CHUNKS_PER_PAPER)
        except Exception:
            texts = []
        chunks = []
        for t in texts:
            chunks.append({
                "chunk_id": t.id, "page_start": t.page_start, "page_end": t.page_end,
                "section": t.section or "", "content": t.content,
                "citation": t.citation_key,
                "evidence_id": make_evidence_id(project_id, pid, t.id,
                                    t.content_hash, t.embedding_version),
                "project_id": project_id,
                "paper_id": pid,
                "content_hash": t.content_hash,
                "embedding_version": t.embedding_version,
                "evidence_type": "fulltext",
                "excerpt": t.content,
                "retrieval_sources": ["per_paper_hybrid_rag"],
                "retrieval_scores": {},
                "chunk_index": t.chunk_index,
                "title": pm["title"],
                "summary": t.content,
                "source_marker": t.citation_key,
            })
        entry = {
            **pm,
            "evidence_source": "fulltext_hybrid_rag",
            "chunks": chunks,
        }
        if not chunks:
            entry["evidence_note"] = "该论文虽有入库记录但检索未返回 chunk(可能 embedding 不匹配)。"
            evidence_gaps.append({"paper_id": pid, "title": pm["title"], "reason": "retrieval returned no chunks"})
        results.append(entry)

    # paper coverage:有多少目标论文拿到了全文证据
    covered = sum(1 for r in results if r["chunks"])
    coverage = covered / len(results) if results else 0.0

    return {
        "papers": results,
        "compare_aspect": aspect,
        "paper_coverage": round(coverage, 3),
        "evidence_gaps": evidence_gaps,
        "note": (
            "已对每篇论文单独执行全文 Hybrid RAG,提取关键 chunk 作为比较证据。"
            + (" 所有论文均有全文证据覆盖。" if coverage == 1.0
               else f" 注意:{len(evidence_gaps)} 篇论文缺少全文证据,比较可能不完整。")
        ),
    }


async def export_bibtex(project_id: int) -> dict[str, Any]:
    """导出项目论文库为 BibTeX 文本。"""
    from papers.bibtex import papers_to_bibtex

    resolver = ProjectScopeResolver(project_id)

    def _export() -> str:
        return papers_to_bibtex(resolver.papers())

    bib_text = await sync_to_async(_export)()
    return {"format": "bibtex", "count": bib_text.count("@"), "content": bib_text[:5000]}


def dumps_tool_result(result: dict[str, Any]) -> str:
    return json.dumps(result, ensure_ascii=False, default=str)


def _safe_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    """Log-safe argument summary (§30.3): numeric knobs and counts only —
    never query/question text, papers payloads or free-text arguments."""
    safe: dict[str, Any] = {}
    if "max_results" in arguments:
        safe["max_results"] = arguments["max_results"]
    if "k" in arguments:
        safe["k"] = arguments["k"]
    if "papers" in arguments:
        safe["paper_payload_count"] = len(arguments.get("papers") or [])
    return safe


def _result_summary(name: str, result: dict[str, Any]) -> dict[str, Any]:
    if "error" in result:
        return {"status": "error", "error": result["error"]}
    if name == "query_project_rag":
        return {"status": "ok", "evidence_count": len(result.get("evidence") or []), "fallback": result.get("fallback", "")}
    if name == "search_papers":
        return {"status": "ok", "paper_count": result.get("count", 0)}
    if name == "add_papers_to_project":
        return {"status": "ok", "paper_count": result.get("count", 0)}
    if name == "list_project_papers":
        return {"status": "ok", "paper_count": result.get("count", 0)}
    if name == "get_project_citation_graph":
        graph = result.get("graph") or {}
        return {"status": "ok", "nodes": len(graph.get("nodes") or []), "edges": len(graph.get("edges") or [])}
    if name == "draft_report_section":
        return {"status": "ok", "section_length": len(result.get("section") or "")}
    return {"status": "ok"}


def _metadata_evidence(project_id: int, paper_ids: list[int], question: str, k: int) -> list[dict[str, Any]]:
    from papers.models import Paper

    papers = list(Paper.objects.select_related("venue").filter(id__in=paper_ids))
    ranked = sorted(
        papers,
        key=lambda paper: (_metadata_overlap_score(question, paper), paper.citation_count or 0, paper.year or 0),
        reverse=True,
    )
    return [_paper_metadata_evidence(project_id, paper, question) for paper in ranked[:k]]


def _metadata_overlap_score(question: str, paper) -> int:
    query_tokens = _query_tokens(question)
    haystack = " ".join(
        [
            paper.title or "",
            paper.abstract or "",
            paper.venue.name if paper.venue_id else "",
        ]
    ).lower()
    return sum(1 for token in query_tokens if token in haystack)


def _query_tokens(question: str) -> set[str]:
    text = (question or "").lower()
    tokens = set(re.findall(r"[a-z0-9][a-z0-9_\-]{2,}", text))
    if "mamba" in text:
        tokens.update({"mamba", "selective", "state", "space", "sequence"})
    if "transformer" in text:
        tokens.update({"transformer", "attention"})
    if "rag" in text or "retrieval augmented generation" in text or "检索增强" in text:
        tokens.update({"retrieval", "augmented", "generation", "rag"})
    if "评测" in text or "评价" in text or "benchmark" in text or "evaluation" in text:
        tokens.update({"evaluation", "benchmark", "assess", "faithfulness"})
    if "长序列" in text:
        tokens.update({"long", "sequence", "context"})
    return tokens


def _paper_metadata_evidence(project_id: int, paper, question: str) -> dict[str, Any]:
    source_marker = paper.title
    abstract = (paper.abstract or "").strip()
    if abstract:
        summary = abstract[:420]
    else:
        year = paper.year or "n.d."
        venue = paper.venue.name if paper.venue_id else "unknown venue"
        summary = f"元数据证据：{paper.title}，年份 {year}，来源/venue {venue}。"
    return {
        "evidence_id": f"md-{project_id}-{paper.id}",
        "project_id": project_id,
        "paper_id": paper.id,
        "title": paper.title,
        "summary": summary,
        "citation": source_marker,
        "source_marker": source_marker,
        "score": max(1, _metadata_overlap_score(question, paper)),
        "evidence_type": "metadata",
    }


def _rank_and_filter_search_results(query: str, papers: list[dict[str, Any]], max_results: int) -> list[dict[str, Any]]:
    if not papers:
        return []
    scored = sorted(
        [(_search_relevance_score(query, paper), paper) for paper in papers],
        key=lambda item: (item[0], int(item[1].get("citation_count") or 0), int(item[1].get("year") or 0)),
        reverse=True,
    )
    anchors = _title_anchors(query)
    if anchors:
        anchored = [
            (score, paper)
            for score, paper in scored
            if score > 0 and _title_has_anchor(str(paper.get("title") or ""), anchors)
        ]
        if anchored:
            return [paper for _score, paper in anchored[:max_results]]
    positive = [paper for score, paper in scored if score > 0]
    return (positive or [paper for _score, paper in scored])[:max_results]


def _search_relevance_score(query: str, paper: dict[str, Any]) -> int:
    tokens = _query_tokens(query)
    title = str(paper.get("title") or "").lower()
    abstract = str(paper.get("abstract") or "").lower()
    venue = str(paper.get("venue") or "").lower()
    return (
        sum(3 for token in tokens if token in title)
        + sum(1 for token in tokens if token in abstract)
        + sum(1 for token in tokens if token in venue)
    )


def _title_anchors(query: str) -> set[str]:
    text = (query or "").lower()
    anchors: set[str] = set()
    if "mamba" in text:
        anchors.update({"mamba", "state space"})
    if "transformer" in text or "attention" in text:
        anchors.update({"transformer", "attention", "long sequence"})
    if "retrieval augmented generation" in text or re.search(r"\brag\b", text):
        anchors.update({"retrieval", "rag", "generation", "evaluation", "benchmark"})
    return anchors


def _title_has_anchor(title: str, anchors: set[str]) -> bool:
    lowered = title.lower()
    return any(anchor in lowered for anchor in anchors)
