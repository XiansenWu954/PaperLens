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
                    "project_id": {"type": "integer"},
                    "question": {"type": "string"},
                    "k": {"type": "integer", "default": 6},
                },
                "required": ["project_id", "question"],
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
                    "max_results": {"type": "integer", "default": 5},
                },
                "required": ["query"],
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
                    "project_id": {"type": "integer"},
                    "papers": {"type": "array", "items": {"type": "object"}},
                    "reason": {"type": "string"},
                },
                "required": ["project_id", "papers"],
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
                "properties": {"project_id": {"type": "integer"}},
                "required": ["project_id"],
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
                "properties": {"project_id": {"type": "integer"}},
                "required": ["project_id"],
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
                    "project_id": {"type": "integer"},
                    "question": {"type": "string"},
                },
                "required": ["project_id", "question"],
            },
        },
    },
]


def available_tool_names() -> list[str]:
    return [tool["function"]["name"] for tool in PROJECT_AGENT_TOOLS]


async def execute_project_tool(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    started = time.perf_counter()
    safe_args = _safe_arguments(arguments)
    logger.info(
        "project tool started",
        extra={"event": "project_tool_started", "tool_name": name, **safe_args},
    )
    try:
        if name == "query_project_rag":
            result = await query_project_rag(
                int(arguments["project_id"]),
                str(arguments["question"]),
                int(arguments.get("k", 6)),
            )
        elif name == "search_papers":
            result = await search_papers(
                str(arguments["query"]),
                int(arguments.get("max_results", 5)),
            )
        elif name == "add_papers_to_project":
            result = await add_papers_to_project(
                int(arguments["project_id"]),
                list(arguments.get("papers") or []),
                str(arguments.get("reason") or "Agent selected relevant papers."),
            )
        elif name == "list_project_papers":
            result = await list_project_papers(int(arguments["project_id"]))
        elif name == "get_project_citation_graph":
            result = await get_project_citation_graph(int(arguments["project_id"]))
        elif name == "draft_report_section":
            result = await draft_report_section(
                int(arguments["project_id"]),
                str(arguments["question"]),
            )
        else:
            result = {"error": f"unknown tool {name}"}
        duration_ms = int((time.perf_counter() - started) * 1000)
        logger.info(
            "project tool completed",
            extra={
                "event": "project_tool_completed",
                "tool_name": name,
                "duration_ms": duration_ms,
                **safe_args,
                **_result_summary(name, result),
            },
        )
        return result
    except Exception as exc:
        duration_ms = int((time.perf_counter() - started) * 1000)
        logger.exception(
            "project tool failed",
            extra={
                "event": "project_tool_failed",
                "tool_name": name,
                "duration_ms": duration_ms,
                "error": str(exc),
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
    from api.models import ProjectPaper, ResearchProject
    from papers.models import upsert_paper

    def _add() -> list[dict]:
        project = ResearchProject.objects.get(id=project_id)
        added: list[dict] = []
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
            added.append({"paper_id": paper.id, "title": paper.title, "created": created})
        return added

    added = await sync_to_async(_add)()
    return {"added": added, "count": len(added)}


async def list_project_papers(project_id: int) -> dict[str, Any]:
    from api.models import ProjectPaper

    def _list() -> list[dict]:
        rows = ProjectPaper.objects.select_related("paper", "paper__venue").filter(
            project_id=project_id
        ).order_by("-paper__citation_count", "paper__title")
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
    from api.models import ProjectPaper

    return await sync_to_async(
        lambda: list(
            ProjectPaper.objects.filter(project_id=project_id)
            .exclude(status="excluded")
            .values_list("paper_id", flat=True)
        )
    )()


async def query_project_rag(project_id: int, question: str, k: int = 6) -> dict[str, Any]:
    from rag.models import Text

    paper_ids = await project_paper_ids(project_id)
    if not paper_ids:
        return {"evidence": [], "fallback": "项目论文库为空。"}

    chunked_paper_ids = await sync_to_async(
        lambda: set(
            Text.objects.filter(paper_id__in=paper_ids)
            .values_list("paper_id", flat=True)
            .distinct()
        )
    )()
    if not chunked_paper_ids:
        metadata_evidence = await sync_to_async(_metadata_evidence)(paper_ids, question, k)
        return {"evidence": metadata_evidence, "fallback": "项目论文尚未完成全文向量入库，已使用元数据回答。"}

    from rag.retrieval import retrieve_evidence

    try:
        evidences = await asyncio.wait_for(
            retrieve_evidence(question, paper_ids=paper_ids, k=k),
            timeout=PROJECT_RAG_RCS_TIMEOUT_SECONDS,
        )
    except TimeoutError:
        metadata_evidence = await sync_to_async(_metadata_evidence)(paper_ids, question, k)
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

    fulltext_evidence = [
        {
            "paper_id": ev.text.paper_id,
            "docname": ev.text.docname,
            "summary": ev.summary,
            "score": ev.score,
            "citation": ev.citation_key,
            "source_marker": ev.citation_key or ev.text.docname,
            "evidence_type": "fulltext",
            "page_start": ev.text.page_start,
            "page_end": ev.text.page_end,
            "section": ev.text.section,
            "chunk_index": ev.text.chunk_index,
            "embedding_model": ev.text.embedding_model,
        }
        for ev in evidences
    ]
    seen_papers = {item["paper_id"] for item in fulltext_evidence if item.get("paper_id")}
    combined = list(fulltext_evidence)
    fallback = ""
    missing_chunk_paper_ids = [paper_id for paper_id in paper_ids if paper_id not in chunked_paper_ids]
    if missing_chunk_paper_ids:
        metadata_evidence = await sync_to_async(_metadata_evidence)(missing_chunk_paper_ids, question, k)
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
    from api.models import ProjectPaper

    def _build() -> dict:
        papers = [
            row.paper
            for row in ProjectPaper.objects.select_related("paper")
            .filter(project_id=project_id)
            .exclude(status="excluded")
        ]
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
        lines.append(f"- {item.get('summary', '')} ({source})")
    return {"section": "\n".join(lines), "evidence": evidence}


def dumps_tool_result(result: dict[str, Any]) -> str:
    return json.dumps(result, ensure_ascii=False, default=str)


def _safe_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    safe: dict[str, Any] = {}
    if "project_id" in arguments:
        safe["project_id"] = arguments["project_id"]
    if "query" in arguments:
        safe["query_preview"] = str(arguments.get("query") or "")[:120]
    if "question" in arguments:
        safe["question_preview"] = str(arguments.get("question") or "")[:120]
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


def _metadata_evidence(paper_ids: list[int], question: str, k: int) -> list[dict[str, Any]]:
    from papers.models import Paper

    papers = list(Paper.objects.select_related("venue").filter(id__in=paper_ids))
    ranked = sorted(
        papers,
        key=lambda paper: (_metadata_overlap_score(question, paper), paper.citation_count or 0, paper.year or 0),
        reverse=True,
    )
    return [_paper_metadata_evidence(paper, question) for paper in ranked[:k]]


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


def _paper_metadata_evidence(paper, question: str) -> dict[str, Any]:
    source_marker = paper.title
    abstract = (paper.abstract or "").strip()
    if abstract:
        summary = abstract[:420]
    else:
        year = paper.year or "n.d."
        venue = paper.venue.name if paper.venue_id else "unknown venue"
        summary = f"元数据证据：{paper.title}，年份 {year}，来源/venue {venue}。"
    return {
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
