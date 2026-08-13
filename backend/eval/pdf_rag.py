"""PDF ingestion and project-scoped RAG quality evaluation.

The evaluator uses generated local PDFs so the PDF -> text -> chunk -> vector ->
project RAG path is repeatable and does not depend on third-party PDF hosting.
It can optionally call DeepSeek through the normal Agent harness to validate the
final user-facing answer on top of full-text evidence.
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import re
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Iterator
from unittest import mock

import numpy as np
from django.conf import settings

from agent.harness import ProjectAgentHarness
from agent.project_tools import query_project_rag
from api.models import ProjectPaper, ResearchProject
from papers.models import Paper, upsert_paper
from rag.ingest import ingest_pdf_bytes
from rag.models import Evidence, Text

logger = logging.getLogger(__name__)


PDF_RAG_PAPERS: tuple[dict[str, Any], ...] = (
    {
        "source": "pdf-eval",
        "source_id": "pdf-transformer-long-sequence",
        "title": "Attention Is All You Need",
        "abstract": "Transformer self-attention enables global dependency modeling but has quadratic long-sequence cost.",
        "year": 2017,
        "arxiv_id": "1706.03762-pdf-eval",
        "referenced_works": ["W1", "W2", "W3"],
        "text": (
            "Attention Is All You Need introduced the Transformer architecture. "
            "The method replaces recurrence with multi-head self-attention and feed-forward layers. "
            "A key strength is global dependency modeling: every token can attend to every other token, "
            "and training can be parallelized across positions. "
            "A limitation for very long sequences is that full self-attention compares every token with "
            "every other token. This creates quadratic memory and compute cost as sequence length grows. "
            "Long-context applications therefore need efficient attention, retrieval, compression, or "
            "alternative sequence models to avoid expensive all-pairs attention. "
            "Evaluation should report accuracy, latency, memory, and behavior on rare long-distance facts. "
        ),
    },
    {
        "source": "pdf-eval",
        "source_id": "pdf-mamba-selective-state-space",
        "title": "Mamba: Linear-Time Sequence Modeling with Selective State Spaces",
        "abstract": "Mamba uses selective state spaces and hardware-aware scan for efficient long sequence modeling.",
        "year": 2023,
        "arxiv_id": "2312.00752-pdf-eval",
        "referenced_works": ["W2", "W3", "W5"],
        "text": (
            "Mamba proposes selective state space models for long sequence modeling. "
            "The architecture uses input-dependent selective state updates and a hardware-aware parallel scan. "
            "Its main advantage is linear-time sequence processing, which can reduce memory and compute cost "
            "relative to full attention on long contexts. "
            "A limitation is that recurrent state compression may make exact content lookup harder than "
            "explicit attention when the answer depends on a rare token or precise copied detail. "
            "Strong evaluation should test long-context retrieval, throughput, memory, and quality on tasks "
            "that require both broad compression and exact recall. "
        ),
    },
    {
        "source": "pdf-eval",
        "source_id": "pdf-rag-evaluation",
        "title": "Evaluating Retrieval-Augmented Generation for Faithful Answers",
        "abstract": "RAG evaluation should measure retrieval quality, grounding, citation coverage, and answer faithfulness.",
        "year": 2025,
        "arxiv_id": "2501.00001-pdf-eval",
        "referenced_works": ["W6", "W7"],
        "text": (
            "Retrieval-Augmented Generation systems require evaluation beyond final answer accuracy. "
            "A useful RAG benchmark measures retrieval recall, context precision, citation coverage, "
            "answer faithfulness, and unsupported claim rate. "
            "The evidence board should expose which passages were used, which citations support claims, "
            "and which project papers remain uncovered by a report. "
            "A practical Agent harness should log tool calls, evidence counts, source markers, latency, "
            "timeouts, and fallback status so failures can be diagnosed. "
        ),
    },
)


@dataclass(frozen=True)
class PdfRagCase:
    id: str
    question: str
    expected_title: str
    expected_terms: tuple[str, ...]


PDF_RAG_CASES: tuple[PdfRagCase, ...] = (
    PdfRagCase(
        id="transformer_long_sequence_cost",
        question="What limitation does Transformer self-attention have for very long sequences?",
        expected_title="Attention Is All You Need",
        expected_terms=("quadratic", "memory", "compute"),
    ),
    PdfRagCase(
        id="mamba_strength_and_limit",
        question="What advantage and limitation does Mamba have for long context retrieval?",
        expected_title="Mamba: Linear-Time Sequence Modeling with Selective State Spaces",
        expected_terms=("linear", "state", "exact"),
    ),
    PdfRagCase(
        id="rag_evaluation_metrics",
        question="Which metrics should a RAG evaluation track for faithful answers?",
        expected_title="Evaluating Retrieval-Augmented Generation for Faithful Answers",
        expected_terms=("retrieval", "citation", "faithfulness"),
    ),
)


def run_pdf_rag_eval(
    *,
    include_live_agent: bool = False,
    use_production_embedding: bool = False,
    use_critic: bool = False,
) -> dict[str, Any]:
    started = time.perf_counter()
    logger.info(
        "PDF RAG evaluation started",
        extra={
            "event": "pdf_rag_evaluation_started",
            "include_live_agent": include_live_agent,
            "embedding_mode": "production" if use_production_embedding else "lexical_fixture",
        },
    )
    project = ResearchProject.objects.create(
        title=f"PaperLens PDF RAG eval {time.strftime('%Y%m%d-%H%M%S')}",
        description="Archived scratch project for PDF ingestion and project RAG quality evaluation.",
        status="archived",
    )
    with _embedding_patch(enabled=not use_production_embedding):
        ingestion = asyncio.run(_seed_project_from_generated_pdfs(project))
        rag_cases = [
            asyncio.run(_run_rag_case(project.id, case))
            for case in PDF_RAG_CASES
        ]
        agent_case = (
            asyncio.run(_run_live_agent_case(project.id, use_critic=use_critic))
            if include_live_agent
            else {}
        )

    passed = all(item["passed"] for item in ingestion["papers"]) and all(case["passed"] for case in rag_cases)
    if include_live_agent:
        passed = passed and bool(agent_case.get("passed"))
    result = {
        "passed": passed,
        "project_id": project.id,
        "embedding_mode": "production" if use_production_embedding else "lexical_fixture",
        "duration_ms": round((time.perf_counter() - started) * 1000, 2),
        "ingestion": ingestion,
        "rag_cases": rag_cases,
        "agent_case": agent_case,
        "summary": {
            "papers_ingested": len(ingestion["papers"]),
            "chunks_ingested": ingestion["chunk_count"],
            "rag_cases": len(rag_cases),
            "rag_cases_passed": sum(1 for case in rag_cases if case["passed"]),
            "live_agent_passed": agent_case.get("passed") if include_live_agent else None,
        },
    }
    logger.info(
        "PDF RAG evaluation completed",
        extra={
            "event": "pdf_rag_evaluation_completed",
            "project_id": project.id,
            "status": "passed" if result["passed"] else "failed",
            "chunk_count": ingestion["chunk_count"],
            "rag_cases_passed": result["summary"]["rag_cases_passed"],
            "rag_cases": len(rag_cases),
            "duration_ms": result["duration_ms"],
        },
    )
    return result


async def _seed_project_from_generated_pdfs(project: ResearchProject) -> dict[str, Any]:
    rows = []
    total_chunks = 0
    for item in PDF_RAG_PAPERS:
        paper = await asyncio.to_thread(_upsert_eval_paper, item, project.id)
        await asyncio.to_thread(
            ProjectPaper.objects.get_or_create,
            project=project,
            paper=paper,
            defaults={"status": "included", "added_by": "demo", "source_reason": "PDF RAG quality fixture"},
        )
        chunk_count = await ingest_pdf_bytes(paper, _simple_pdf_bytes(_long_enough_text(item["text"])))
        # ING-B-CX-05 (P0): retrieval serves ONLY ACTIVE index versions.
        # Ingest writes a building version; the eval fixture must ACTIVATE it
        # (the atomic IngestionService build later formalizes this step).
        await asyncio.to_thread(_activate_paper_version, paper)
        persisted = await asyncio.to_thread(lambda: Text.objects.filter(paper=paper).count())
        passed = chunk_count > 0 and persisted == chunk_count
        total_chunks += chunk_count
        rows.append(
            {
                "paper_id": paper.id,
                "title": paper.title,
                "chunk_count": chunk_count,
                "persisted_chunks": persisted,
                "passed": passed,
            }
        )
        logger.info(
            "PDF RAG paper fixture ingested",
            extra={
                "event": "pdf_rag_fixture_ingested",
                "project_id": project.id,
                "paper_id": paper.id,
                "chunk_count": chunk_count,
                "status": "passed" if passed else "failed",
            },
        )
    return {"papers": rows, "chunk_count": total_chunks}


def _activate_paper_version(paper) -> None:
    """ING-B-CX-05: flip the paper's newest building version to active,
    superseding any prior active version (one active version per paper)."""
    from rag.models import PaperIndexVersion

    building = (
        PaperIndexVersion.objects.filter(paper=paper, status="building")
        .order_by("-id")
        .first()
    )
    if building is None:
        return
    PaperIndexVersion.objects.filter(paper=paper, status="active").update(
        status="superseded")
    building.status = "active"
    building.save(update_fields=["status", "updated_at"])


def _upsert_eval_paper(item: dict[str, Any], project_id: int) -> Paper:
    source_id = f"{item['source_id']}-{project_id}"
    return upsert_paper(
        {
            "source": item["source"],
            "source_id": source_id,
            "title": item["title"],
            "abstract": item["abstract"],
            "year": item["year"],
            "arxiv_id": f"{item['arxiv_id']}-{project_id}"[:32],
            "referenced_works": item["referenced_works"],
            "pdf_url": f"https://paperlens.local/{source_id}.pdf",
        }
    )


async def _run_rag_case(project_id: int, case: PdfRagCase) -> dict[str, Any]:
    started = time.perf_counter()
    result = await query_project_rag(project_id, case.question, k=4)
    evidence = result.get("evidence") or []
    combined = " ".join(
        str(item.get(key) or "")
        for item in evidence
        for key in ("summary", "docname", "title", "source_marker", "citation")
    ).lower()
    source_hit = any(case.expected_title[:20].lower() in str(item.get("docname", "")).lower() for item in evidence)
    term_hits = [term for term in case.expected_terms if term.lower() in combined]
    source_markers = [
        item.get("source_marker") or item.get("citation") or item.get("docname")
        for item in evidence
        if item.get("source_marker") or item.get("citation") or item.get("docname")
    ]
    passed = bool(evidence) and not result.get("fallback") and source_hit and len(term_hits) >= 1 and bool(source_markers)
    logger.info(
        "PDF RAG case completed",
        extra={
            "event": "pdf_rag_case_completed",
            "project_id": project_id,
            "case_id": case.id,
            "status": "passed" if passed else "failed",
            "evidence_count": len(evidence),
            "term_hit_count": len(term_hits),
            "duration_ms": round((time.perf_counter() - started) * 1000, 2),
        },
    )
    return {
        "id": case.id,
        "passed": passed,
        "question": case.question,
        "expected_title": case.expected_title,
        "expected_terms": list(case.expected_terms),
        "term_hits": term_hits,
        "source_hit": source_hit,
        "source_marker_count": len(source_markers),
        "evidence_count": len(evidence),
        "fallback": result.get("fallback", ""),
        "evidence": evidence,
        "duration_ms": round((time.perf_counter() - started) * 1000, 2),
    }


async def _run_live_agent_case(project_id: int, *, use_critic: bool) -> dict[str, Any]:
    from eval.live_agent import _answer_has_source_marker, _collect_evidence, _critic_case
    from eval.live_agent import LiveAgentCase

    message = "基于全文证据，说明 Transformer 和 Mamba 在长序列场景下各自的优势和局限。"
    started = time.perf_counter()
    result = await ProjectAgentHarness(project_id, use_llm=True, tool_timeout_seconds=30.0).run(message)
    events = result["events"]
    answer = result["answer"].strip()
    tools = [
        event["data"].get("name")
        for event in events
        if event["event"] == "tool_call" and isinstance(event.get("data"), dict)
    ]
    evidence = _collect_evidence(events)
    quality = next((event["data"] for event in events if event["event"] == "quality_check"), {})
    llm_result = next((event["data"] for event in events if event["event"] == "llm_result"), {})
    expected_terms = ("quadratic", "线性", "linear", "精确", "exact")
    term_hit = any(term.lower() in answer.lower() for term in expected_terms)
    source_marker_present = _answer_has_source_marker(answer, evidence)
    critic = {}
    if use_critic:
        critic_case = LiveAgentCase(
            id="pdf_fulltext_long_sequence_cn",
            message=message,
            expected_tools=("query_project_rag",),
        )
        critic = _critic_case(critic_case, answer, evidence, tools, quality, {})
    passed = (
        "query_project_rag" in tools
        and llm_result.get("status") == "ok"
        and quality.get("verdict") == "grounded"
        and source_marker_present
        and term_hit
        and bool(critic.get("passed", True))
    )
    return {
        "id": "pdf_fulltext_long_sequence_cn",
        "passed": passed,
        "message": message,
        "answer": answer,
        "answer_chars": len(answer),
        "tools": tools,
        "quality": quality,
        "llm_result": llm_result,
        "evidence_count": len(evidence),
        "source_marker_present": source_marker_present,
        "term_hit": term_hit,
        "critic": critic,
        "duration_ms": round((time.perf_counter() - started) * 1000, 2),
    }


@contextmanager
def _embedding_patch(*, enabled: bool) -> Iterator[None]:
    if not enabled:
        yield
        return
    with (
        mock.patch("rag.ingest.embed", _lexical_embed),
        mock.patch("rag.retrieval.embed", _lexical_embed),
        mock.patch("rag.retrieval._rcs_summary", _lexical_rcs_summary),
    ):
        yield


_LEXICAL_FEATURES = (
    "transformer",
    "attention",
    "quadratic",
    "memory",
    "compute",
    "mamba",
    "selective",
    "state",
    "linear",
    "exact",
    "retrieval",
    "rag",
    "citation",
    "faithfulness",
    "evaluation",
    "long",
)


def _lexical_embed(texts: list[str], *_, **__) -> np.ndarray:
    dimension = int(getattr(settings, "PAPERLENS_EMBEDDING_DIM", 1024))
    rows = []
    for text in texts:
        lowered = text.lower()
        values = [float(lowered.count(term)) for term in _LEXICAL_FEATURES]
        vector = np.zeros(dimension, dtype=np.float32)
        vector[: min(len(values), dimension)] = values[:dimension]
        norm = float(np.linalg.norm(vector))
        if norm:
            vector = vector / norm
        else:
            fallback_width = min(len(_LEXICAL_FEATURES), dimension)
            vector[:fallback_width] = 1.0 / math.sqrt(fallback_width)
        rows.append(vector)
    return np.array(rows, dtype=np.float32)


async def _lexical_rcs_summary(question: str, text: Text) -> Evidence:
    query_terms = _query_terms(question)
    content = text.content.lower()
    overlap = sum(1 for term in query_terms if term in content)
    score = 1 if overlap == 0 else min(10, 4 + overlap)
    summary = _best_summary_sentence(text.content, query_terms)
    return Evidence(
        text=text,
        question=question,
        summary=summary,
        score=score,
        citation_key=text.citation_key,
    )


def _best_summary_sentence(content: str, query_terms: set[str]) -> str:
    sentences = re.split(r"(?<=[.!?])\s+", content)
    ranked = sorted(
        sentences,
        key=lambda sentence: sum(1 for term in query_terms if term in sentence.lower()),
        reverse=True,
    )
    chosen = " ".join(sentence.strip() for sentence in ranked[:3] if sentence.strip())
    return chosen[:420] or content[:420]


def _query_terms(question: str) -> set[str]:
    lowered = question.lower()
    terms = set(re.findall(r"[a-z][a-z\-]{2,}", lowered))
    if "长序列" in lowered or "long" in lowered:
        terms.update({"long", "sequence", "context"})
    if "优势" in lowered or "advantage" in lowered:
        terms.update({"advantage", "strength", "linear", "parallel"})
    if "局限" in lowered or "limit" in lowered:
        terms.update({"limitation", "quadratic", "memory", "compute", "exact", "compression"})
    if "transformer" in lowered:
        terms.update({"transformer", "attention", "quadratic"})
    if "mamba" in lowered:
        terms.update({"mamba", "selective", "state", "linear", "exact"})
    if "rag" in lowered or "retrieval" in lowered or "检索增强" in lowered:
        terms.update({"rag", "retrieval", "citation", "faithfulness", "evaluation"})
    return terms


def _simple_pdf_bytes(text: str) -> bytes:
    escaped = text.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")
    stream = f"BT /F1 11 Tf 72 720 Td ({escaped}) Tj ET".encode("latin-1", errors="replace")
    objects = [
        b"1 0 obj << /Type /Catalog /Pages 2 0 R >> endobj\n",
        b"2 0 obj << /Type /Pages /Kids [3 0 R] /Count 1 >> endobj\n",
        b"3 0 obj << /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >> endobj\n",
        b"4 0 obj << /Type /Font /Subtype /Type1 /BaseFont /Helvetica >> endobj\n",
        b"5 0 obj << /Length " + str(len(stream)).encode("ascii") + b" >> stream\n" + stream + b"\nendstream endobj\n",
    ]
    output = bytearray(b"%PDF-1.4\n")
    offsets = []
    for obj in objects:
        offsets.append(len(output))
        output.extend(obj)
    xref = len(output)
    output.extend(f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode("ascii"))
    for offset in offsets:
        output.extend(f"{offset:010d} 00000 n \n".encode("ascii"))
    output.extend(f"trailer << /Root 1 0 R /Size {len(objects) + 1} >>\nstartxref\n{xref}\n%%EOF\n".encode("ascii"))
    return bytes(output)


def _long_enough_text(text: str) -> str:
    compact = re.sub(r"\s+", " ", text).strip()
    if len(compact) >= 220:
        return compact
    return f"{compact} {compact}"


def dumps_pdf_rag_eval(result: dict[str, Any]) -> str:
    return json.dumps(result, ensure_ascii=True, indent=2, default=str)
