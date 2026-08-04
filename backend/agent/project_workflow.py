"""LangGraph workflow for longer project research expansion tasks."""
from __future__ import annotations

import logging
import time
from typing import Any, TypedDict

from asgiref.sync import sync_to_async
from langgraph.graph import END, START, StateGraph

from .project_tools import add_papers_to_project, draft_report_section, query_project_rag, search_papers

logger = logging.getLogger(__name__)


class ProjectWorkflowState(TypedDict, total=False):
    project_id: int
    run_id: int
    question: str
    queries: list[str]
    search_results: list[dict[str, Any]]
    added: list[dict[str, Any]]
    ingestion_jobs: list[int]
    evidence: list[dict[str, Any]]
    critic: dict[str, Any]
    report_section: str
    report_id: int


async def run_project_research_expand(project_id: int, question: str, run_id: int) -> dict[str, Any]:
    """Run the bounded LangGraph research expansion workflow."""

    graph = build_project_workflow()
    state: ProjectWorkflowState = {
        "project_id": project_id,
        "run_id": run_id,
        "question": question,
    }
    result = await graph.ainvoke(state)
    return dict(result)


def build_project_workflow():
    graph = StateGraph(ProjectWorkflowState)
    graph.add_node("plan_expansion", plan_expansion)
    graph.add_node("search_sources", search_sources)
    graph.add_node("add_candidates", add_candidates)
    graph.add_node("enqueue_ingestion", enqueue_ingestion)
    graph.add_node("query_hybrid_rag", query_hybrid_rag)
    graph.add_node("critic", critic)
    graph.add_node("draft_report", draft_report)
    graph.add_node("persist_report", persist_report)
    graph.add_edge(START, "plan_expansion")
    graph.add_edge("plan_expansion", "search_sources")
    graph.add_edge("search_sources", "add_candidates")
    graph.add_edge("add_candidates", "enqueue_ingestion")
    graph.add_edge("enqueue_ingestion", "query_hybrid_rag")
    graph.add_edge("query_hybrid_rag", "critic")
    graph.add_edge("critic", "draft_report")
    graph.add_edge("draft_report", "persist_report")
    graph.add_edge("persist_report", END)
    return graph.compile()


async def plan_expansion(state: ProjectWorkflowState) -> dict[str, Any]:
    await _event(state, "workflow_node", {"node": "plan_expansion", "status": "running"})
    question = state["question"].strip()
    queries = [_rewrite_query(question)]
    await _event(state, "workflow_node", {"node": "plan_expansion", "status": "done", "queries": queries})
    return {"queries": queries}


async def search_sources(state: ProjectWorkflowState) -> dict[str, Any]:
    await _event(state, "workflow_node", {"node": "search_sources", "status": "running"})
    results: list[dict[str, Any]] = []
    for query in state.get("queries", []):
        payload = await search_papers(query, max_results=8)
        results.extend(payload.get("papers") or [])
    deduped = _dedupe_papers(results)
    await _event(
        state,
        "workflow_node",
        {"node": "search_sources", "status": "done", "paper_count": len(deduped)},
    )
    return {"search_results": deduped}


async def add_candidates(state: ProjectWorkflowState) -> dict[str, Any]:
    await _event(state, "workflow_node", {"node": "add_candidates", "status": "running"})
    result = await add_papers_to_project(
        state["project_id"],
        state.get("search_results", []),
        f"LangGraph expansion: {state['question'][:120]}",
    )
    added = result.get("added") or []
    await _event(
        state,
        "workflow_node",
        {"node": "add_candidates", "status": "done", "added_count": len(added)},
    )
    return {"added": added}


async def enqueue_ingestion(state: ProjectWorkflowState) -> dict[str, Any]:
    await _event(state, "workflow_node", {"node": "enqueue_ingestion", "status": "running"})
    job_ids = await sync_to_async(_enqueue_missing_pdf_ingestion)(state["project_id"])
    await _event(
        state,
        "workflow_node",
        {"node": "enqueue_ingestion", "status": "done", "job_count": len(job_ids)},
    )
    return {"ingestion_jobs": job_ids}


async def query_hybrid_rag(state: ProjectWorkflowState) -> dict[str, Any]:
    await _event(state, "workflow_node", {"node": "query_hybrid_rag", "status": "running"})
    result = await query_project_rag(state["project_id"], state["question"], k=8)
    evidence = result.get("evidence") or []
    await _event(
        state,
        "hybrid_retrieval",
        {
            "node": "query_hybrid_rag",
            "status": "done",
            "evidence_count": len(evidence),
            "fallback": result.get("fallback", ""),
        },
    )
    return {"evidence": evidence}


async def critic(state: ProjectWorkflowState) -> dict[str, Any]:
    evidence = state.get("evidence", [])
    verdict = {
        "passed": len(evidence) >= 2,
        "evidence_count": len(evidence),
        "risk": "low" if len(evidence) >= 4 else "medium" if evidence else "high",
        "recommendation": "Proceed with a cited draft." if evidence else "Search and ingest more full-text PDFs first.",
    }
    await _event(state, "workflow_node", {"node": "critic", "status": "done", **verdict})
    return {"critic": verdict}


async def draft_report(state: ProjectWorkflowState) -> dict[str, Any]:
    await _event(state, "workflow_node", {"node": "draft_report", "status": "running"})
    result = await draft_report_section(state["project_id"], state["question"])
    section = result.get("section", "")
    await _event(
        state,
        "workflow_node",
        {"node": "draft_report", "status": "done", "section_chars": len(section)},
    )
    return {"report_section": section}


async def persist_report(state: ProjectWorkflowState) -> dict[str, Any]:
    await _event(state, "workflow_node", {"node": "persist_report", "status": "running"})
    report_id = await sync_to_async(_save_report)(
        state["project_id"],
        state["question"],
        state.get("report_section", ""),
    )
    await _event(
        state,
        "workflow_completed",
        {"node": "persist_report", "status": "done", "report_id": report_id},
    )
    return {"report_id": report_id}


def _enqueue_missing_pdf_ingestion(project_id: int) -> list[int]:
    from api.models import PaperIngestionJob, ProjectPaper
    from api.tasks import ingest_paper_pdf_task

    rows = (
        ProjectPaper.objects.select_related("paper", "project")
        .filter(project_id=project_id)
        .exclude(status="excluded")
    )
    job_ids: list[int] = []
    for row in rows:
        if row.paper.chunks.exists() or not row.paper.pdf_url:
            continue
        job = PaperIngestionJob.objects.create(
            project=row.project,
            paper=row.paper,
            status="pending",
            source_url=row.paper.pdf_url,
            file_name=row.paper.pdf_url.split("/")[-1][:255],
        )
        result = ingest_paper_pdf_task.delay(job.id)
        if result.id:
            job.celery_task_id = result.id
            job.save(update_fields=["celery_task_id", "updated_at"])
        job_ids.append(job.id)
    return job_ids


def _save_report(project_id: int, question: str, content: str) -> int:
    from api.models import ReportVersion

    report = ReportVersion.objects.create(
        project_id=project_id,
        title=f"Expansion report: {question[:80]}",
        content=content or "证据不足，未能生成有效章节。",
        source="langgraph",
    )
    return report.id


async def _event(state: ProjectWorkflowState, event_type: str, payload: dict[str, Any]) -> None:
    from api.models import ProjectRunEvent

    run_id = state.get("run_id")
    if not run_id:
        return
    await sync_to_async(ProjectRunEvent.objects.create)(
        run_id=run_id,
        event_type=event_type,
        payload=payload,
    )
    logger.info(
        "project workflow event",
        extra={
            "event": event_type,
            "project_id": state.get("project_id"),
            "run_id": run_id,
            "workflow_node": payload.get("node", ""),
            "status": payload.get("status", "ok"),
        },
    )


def _rewrite_query(question: str) -> str:
    lowered = question.lower()
    if "mamba" in lowered:
        return "Mamba selective state space model long sequence follow-up"
    if "rag" in lowered or "retrieval augmented generation" in lowered:
        return "retrieval augmented generation evaluation faithfulness benchmark"
    if "citation" in lowered or "引用" in lowered:
        return "citation graph bibliographic coupling paper recommendation"
    return question[:180]


def _dedupe_papers(papers: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    output: list[dict[str, Any]] = []
    for paper in papers:
        key = str(paper.get("doi") or paper.get("arxiv_id") or paper.get("openalex_id") or paper.get("title") or "")
        key = key.lower().strip()
        if not key or key in seen:
            continue
        seen.add(key)
        output.append(paper)
    return output
