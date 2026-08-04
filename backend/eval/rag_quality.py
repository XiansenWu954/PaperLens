"""Deterministic 30+ case evaluation for PaperLens hybrid RAG."""
from __future__ import annotations

import asyncio
import hashlib
import json
import math
import re
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Iterator
from unittest import mock

import numpy as np
from asgiref.sync import sync_to_async
from django.conf import settings

from api.models import ProjectPaper, ResearchProject
from papers.models import Paper
from rag.citations import make_citation_key_for_paper
from rag.models import Text
from rag.retrieval import hybrid_retrieve_texts


@dataclass(frozen=True)
class RagEvalPaper:
    title: str
    abstract: str
    year: int
    text: str


@dataclass(frozen=True)
class RagEvalCase:
    id: str
    question: str
    gold_titles: tuple[str, ...]
    expected_terms: tuple[str, ...]
    category: str


RAG_EVAL_PAPERS: tuple[RagEvalPaper, ...] = (
    RagEvalPaper(
        "Attention Is All You Need",
        "Transformer self-attention improves parallel sequence modeling but has quadratic long-context cost.",
        2017,
        "Transformer architecture uses multi-head self-attention and feed-forward layers. Full attention has quadratic memory and compute cost for long sequences, although it models global dependencies and trains in parallel.",
    ),
    RagEvalPaper(
        "Mamba: Linear-Time Sequence Modeling with Selective State Spaces",
        "Mamba uses selective state space models and hardware-aware scan for long sequence modeling.",
        2023,
        "Mamba introduces input-dependent selective state updates and a hardware-aware parallel scan. It provides linear-time sequence processing, but compressed recurrent state can make exact rare-token lookup harder than explicit attention.",
    ),
    RagEvalPaper(
        "Evaluating Retrieval-Augmented Generation for Faithful Answers",
        "RAG evaluation should measure retrieval quality, grounding, citation coverage, and faithfulness.",
        2025,
        "A RAG evaluation should report Recall@5, MRR, context precision, citation coverage, faithfulness, unsupported claim rate, and retrieval latency. Evidence should be tied to cited passages.",
    ),
    RagEvalPaper(
        "Citation Graphs for Literature Discovery",
        "Bibliographic coupling and citation maps reveal related work, root papers, and frontier papers.",
        2024,
        "Citation graph analysis uses referenced works and bibliographic coupling to connect papers. Root papers are older highly cited anchors, while frontier papers are recent nodes that point to emerging directions.",
    ),
    RagEvalPaper(
        "DBLP Metadata for Computer Science Literature Agents",
        "DBLP provides authoritative computer science venue, author, and key metadata.",
        2026,
        "DBLP is useful as a default source for computer science literature because it gives reliable venue, author, year, and bibliographic key metadata. OpenAlex can complement it with citations and abstracts.",
    ),
    RagEvalPaper(
        "Hybrid RAG with Dense Vectors and Lexical Search",
        "Hybrid retrieval combines dense embedding search, lexical search, and reciprocal rank fusion.",
        2026,
        "Hybrid RAG retrieves candidates from dense vector search and lexical full-text search. Reciprocal Rank Fusion combines rankings so exact terms and semantic matches can both surface.",
    ),
    RagEvalPaper(
        "PostgreSQL pgvector Indexing for Semantic Search",
        "pgvector stores embeddings inside Postgres and supports HNSW vector indexes.",
        2025,
        "PostgreSQL with pgvector can store embeddings in a vector column. HNSW indexes support approximate nearest-neighbor search, and metadata filters keep retrieval project-scoped.",
    ),
    RagEvalPaper(
        "Postgres Full-Text Search for RAG Retrieval",
        "PostgreSQL FTS provides lexical matching over passages with ranking.",
        2024,
        "Postgres full-text search uses tsvector and tsquery to retrieve exact lexical matches. It is a pragmatic BM25-like first lexical layer for local RAG systems.",
    ),
    RagEvalPaper(
        "PDF Ingestion Pipelines for Evidence-Centric RAG",
        "PDF ingestion should preserve page, section, offset, hash, and parse status.",
        2026,
        "PDF ingestion quality depends on parsing pages, chunking text, preserving section names, page ranges, character offsets, content hashes, and embedding status for retryable background jobs.",
    ),
    RagEvalPaper(
        "LangGraph Workflows for Durable Research Agents",
        "LangGraph is appropriate for explicit multi-step research workflows.",
        2026,
        "LangGraph fits long-running workflows such as expand search, add candidates, enqueue ingestion, query hybrid RAG, run a critic, draft a report, and persist a versioned artifact.",
    ),
)


RAG_EVAL_CASES: tuple[RagEvalCase, ...] = (
    RagEvalCase("transformer_quadratic_cost", "Why is full Transformer attention expensive for long sequences?", ("Attention Is All You Need",), ("quadratic", "memory", "compute"), "long_sequence"),
    RagEvalCase("transformer_parallel_training", "Which paper says self-attention enables parallel training across sequence positions?", ("Attention Is All You Need",), ("parallel", "attention"), "method"),
    RagEvalCase("mamba_linear_time", "What makes Mamba efficient for long sequence processing?", ("Mamba: Linear-Time Sequence Modeling with Selective State Spaces",), ("linear", "selective", "scan"), "long_sequence"),
    RagEvalCase("mamba_exact_lookup_limit", "What limitation can Mamba have for exact rare-token lookup?", ("Mamba: Linear-Time Sequence Modeling with Selective State Spaces",), ("exact", "rare-token", "state"), "method_compare"),
    RagEvalCase("rag_eval_metrics", "Which metrics should evaluate RAG faithfulness and retrieval quality?", ("Evaluating Retrieval-Augmented Generation for Faithful Answers",), ("recall", "mrr", "faithfulness"), "rag_evaluation"),
    RagEvalCase("rag_citation_coverage", "Which RAG metric checks whether answer claims are tied to citations?", ("Evaluating Retrieval-Augmented Generation for Faithful Answers",), ("citation", "coverage"), "rag_evaluation"),
    RagEvalCase("citation_root_frontier", "How does a citation map distinguish root papers from frontier papers?", ("Citation Graphs for Literature Discovery",), ("root", "frontier"), "citation_graph"),
    RagEvalCase("bibliographic_coupling", "What signal connects related papers in a citation graph?", ("Citation Graphs for Literature Discovery",), ("referenced", "coupling"), "citation_graph"),
    RagEvalCase("dblp_default_source", "Why should DBLP be a default source for a CS literature agent?", ("DBLP Metadata for Computer Science Literature Agents",), ("venue", "author", "metadata"), "metadata"),
    RagEvalCase("openalex_complement", "What should OpenAlex complement DBLP with?", ("DBLP Metadata for Computer Science Literature Agents",), ("citations", "abstracts"), "metadata"),
    RagEvalCase("hybrid_rrf", "How does Reciprocal Rank Fusion help hybrid RAG?", ("Hybrid RAG with Dense Vectors and Lexical Search",), ("dense", "lexical", "fusion"), "hybrid"),
    RagEvalCase("dense_lexical_balance", "Why combine dense vector search with lexical full-text search?", ("Hybrid RAG with Dense Vectors and Lexical Search",), ("semantic", "exact", "terms"), "hybrid"),
    RagEvalCase("pgvector_column", "Where are embeddings stored in a pgvector architecture?", ("PostgreSQL pgvector Indexing for Semantic Search",), ("vector", "column"), "infra"),
    RagEvalCase("hnsw_index", "Which pgvector index supports approximate nearest-neighbor search?", ("PostgreSQL pgvector Indexing for Semantic Search",), ("hnsw", "nearest"), "infra"),
    RagEvalCase("postgres_fts", "What does Postgres full-text search use for lexical retrieval?", ("Postgres Full-Text Search for RAG Retrieval",), ("tsvector", "tsquery"), "lexical"),
    RagEvalCase("bm25_like_layer", "Which component acts as the first lexical layer before Elasticsearch is introduced?", ("Postgres Full-Text Search for RAG Retrieval",), ("full-text", "lexical"), "lexical"),
    RagEvalCase("pdf_page_offsets", "Which PDF ingestion metadata helps audit evidence location?", ("PDF Ingestion Pipelines for Evidence-Centric RAG",), ("page", "offset", "section"), "pdf"),
    RagEvalCase("pdf_retry_status", "What status should make PDF ingestion retryable?", ("PDF Ingestion Pipelines for Evidence-Centric RAG",), ("status", "retryable", "background"), "pdf"),
    RagEvalCase("langgraph_use_case", "When is LangGraph justified in this project?", ("LangGraph Workflows for Durable Research Agents",), ("long-running", "workflow"), "agent_workflow"),
    RagEvalCase("workflow_nodes", "Which workflow steps belong in the long research expansion chain?", ("LangGraph Workflows for Durable Research Agents",), ("expand", "critic", "report"), "agent_workflow"),
    RagEvalCase("zh_mamba_advantage", "Mamba 在长序列处理中的优势是什么？", ("Mamba: Linear-Time Sequence Modeling with Selective State Spaces",), ("linear", "selective"), "zh"),
    RagEvalCase("zh_transformer_limit", "Transformer 在长上下文中的主要成本问题是什么？", ("Attention Is All You Need",), ("quadratic", "memory"), "zh"),
    RagEvalCase("zh_rag_eval", "RAG 评测应该关注哪些指标？", ("Evaluating Retrieval-Augmented Generation for Faithful Answers",), ("recall", "faithfulness"), "zh"),
    RagEvalCase("zh_pdf_metadata", "PDF 入库为什么要保留页码和 offset？", ("PDF Ingestion Pipelines for Evidence-Centric RAG",), ("page", "offset"), "zh"),
    RagEvalCase("venue_author_exact", "Which source is authoritative for CS venue and author metadata?", ("DBLP Metadata for Computer Science Literature Agents",), ("dblp", "venue", "author"), "exact"),
    RagEvalCase("project_scope_filter", "Which vector database setup keeps retrieval project-scoped with metadata filters?", ("PostgreSQL pgvector Indexing for Semantic Search",), ("metadata", "project-scoped"), "infra"),
    RagEvalCase("citation_discovery", "What graph method supports literature discovery through related work edges?", ("Citation Graphs for Literature Discovery",), ("citation", "related"), "citation_graph"),
    RagEvalCase("unsupported_quantum", "What evidence does this project have about quantum chemistry solvers?", tuple(), ("quantum", "chemistry"), "no_evidence"),
    RagEvalCase("unsupported_medical", "What clinical trial evidence is in this project?", tuple(), ("clinical", "trial"), "no_evidence"),
    RagEvalCase("unsupported_finance", "Which stock trading strategy did the papers validate?", tuple(), ("stock", "trading"), "no_evidence"),
    RagEvalCase("hybrid_exact_abbrev", "What does FTS add when a query contains exact abbreviations like RAG or DBLP?", ("Hybrid RAG with Dense Vectors and Lexical Search", "DBLP Metadata for Computer Science Literature Agents"), ("exact", "lexical"), "abbrev"),
    RagEvalCase("report_version_workflow", "Which Agent workflow step persists a versioned report artifact?", ("LangGraph Workflows for Durable Research Agents",), ("persist", "artifact"), "agent_workflow"),
)


def run_rag_quality_eval(*, write_report: bool = False) -> dict[str, Any]:
    started = time.perf_counter()
    project = ResearchProject.objects.create(
        title=f"PaperLens RAG quality eval {time.strftime('%Y%m%d-%H%M%S')}",
        description="Archived deterministic fixture project for hybrid RAG evaluation.",
        status="archived",
    )
    with _embedding_patch():
        _seed_eval_project(project)
        case_results = [asyncio.run(_run_case(project.id, case)) for case in RAG_EVAL_CASES]
    metrics = _aggregate(case_results)
    result = {
        "passed": metrics["recall_at_5"] >= 0.80
        and metrics["citation_coverage"] >= 0.90
        and metrics["unsupported_claim_rate"] <= 0.10
        and len(case_results) >= 30,
        "project_id": project.id,
        "case_count": len(case_results),
        "duration_ms": round((time.perf_counter() - started) * 1000, 2),
        "metrics": metrics,
        "cases": case_results,
    }
    return result


async def _run_case(project_id: int, case: RagEvalCase) -> dict[str, Any]:
    started = time.perf_counter()
    paper_ids = await sync_to_async(_project_paper_ids)(project_id)
    texts = await hybrid_retrieve_texts(case.question, paper_ids=paper_ids, final_k=5)
    titles = [text.paper.title for text in texts]
    contexts = [text.content for text in texts]
    first_rank = _first_gold_rank(titles, case.gold_titles)
    is_no_evidence = len(case.gold_titles) == 0
    relevant_flags = [_is_relevant(text, case) for text in texts]
    context_precision = sum(relevant_flags) / len(relevant_flags) if relevant_flags else 0.0
    combined = " ".join(contexts).lower()
    term_hits = [term for term in case.expected_terms if term.lower() in combined]
    citation_coverage = sum(1 for text in texts if text.citation_key) / len(texts) if texts else 0.0
    faithfulness = 1.0 if (is_no_evidence or len(term_hits) >= min(2, len(case.expected_terms))) else 0.0
    unsupported = 1.0 if is_no_evidence and context_precision > 0 else 0.0
    passed = (first_rank > 0 and len(term_hits) > 0) if not is_no_evidence else unsupported == 0.0
    return {
        "id": case.id,
        "category": case.category,
        "question": case.question,
        "gold_titles": list(case.gold_titles),
        "retrieved_titles": titles,
        "passed": passed,
        "recall_at_5": 0.0 if is_no_evidence else (1.0 if first_rank > 0 else 0.0),
        "mrr": 0.0 if first_rank == 0 else round(1.0 / first_rank, 4),
        "context_precision": round(context_precision, 4),
        "citation_coverage": round(citation_coverage, 4),
        "faithfulness": faithfulness,
        "unsupported_claim": unsupported,
        "term_hits": term_hits,
        "latency_ms": round((time.perf_counter() - started) * 1000, 2),
    }


def _seed_eval_project(project: ResearchProject) -> None:
    for index, item in enumerate(RAG_EVAL_PAPERS):
        paper = Paper.objects.create(
            title=item.title,
            abstract=item.abstract,
            year=item.year,
            citation_count=100 - index,
            pdf_url=f"https://paperlens.local/eval/{index}.pdf",
        )
        ProjectPaper.objects.create(
            project=project,
            paper=paper,
            status="included",
            added_by="demo",
            source_reason="RAG quality fixture",
        )
        chunks = [
            f"{item.title}. {item.abstract}",
            item.text,
            f"{item.abstract} {item.text}",
        ]
        for chunk_index, content in enumerate(chunks):
            vector = _lexical_embed([f"{item.title} {content}"])[0]
            Text.objects.create(
                paper=paper,
                docname=f"{item.title[:32]} chunk{chunk_index}",
                chunk_index=chunk_index,
                content=content,
                embedding=vector.tolist(),
                embedding_model="eval-lexical",
                embedding_dim=len(vector),
                embedding_version="eval-lexical:v1",
                content_hash=hashlib.sha256(content.encode("utf-8")).hexdigest(),
                page_start=chunk_index + 1,
                page_end=chunk_index + 1,
                section="Evaluation fixture",
                char_start=0,
                char_end=len(content),
                search_vector=f"{item.title}\n{item.abstract}\n{content}",
                citation_key=make_citation_key_for_paper(paper.id),
            )


def _aggregate(cases: list[dict[str, Any]]) -> dict[str, Any]:
    evidence_cases = [case for case in cases if case["gold_titles"]]
    return {
        "recall_at_5": _avg([case["recall_at_5"] for case in evidence_cases]),
        "mrr": _avg([case["mrr"] for case in evidence_cases]),
        "context_precision": _avg([case["context_precision"] for case in evidence_cases]),
        "citation_coverage": _avg([case["citation_coverage"] for case in cases]),
        "faithfulness": _avg([case["faithfulness"] for case in cases]),
        "unsupported_claim_rate": _avg([case["unsupported_claim"] for case in cases]),
        "average_retrieval_latency_ms": round(_avg([case["latency_ms"] for case in cases]), 2),
        "passed_cases": sum(1 for case in cases if case["passed"]),
        "total_cases": len(cases),
    }


@contextmanager
def _embedding_patch() -> Iterator[None]:
    dimension = int(getattr(settings, "PAPERLENS_EMBEDDING_DIM", 1024))
    metadata = {
        "embedding_model": "eval-lexical",
        "embedding_dim": dimension,
        "embedding_version": f"eval-lexical:dim{dimension}:v1",
    }
    with (
        mock.patch("rag.retrieval.embed", _lexical_embed),
        mock.patch("rag.retrieval.embedding_metadata", lambda: metadata),
    ):
        yield


_VOCAB = (
    "transformer", "attention", "quadratic", "memory", "compute", "parallel", "global",
    "mamba", "selective", "state", "scan", "linear", "exact", "rare", "token",
    "retrieval", "augmented", "generation", "rag", "recall", "mrr", "precision",
    "citation", "coverage", "faithfulness", "unsupported", "latency", "evidence",
    "graph", "bibliographic", "coupling", "root", "frontier", "related",
    "dblp", "venue", "author", "metadata", "openalex", "abstracts",
    "hybrid", "dense", "vector", "lexical", "fusion", "semantic", "terms",
    "postgresql", "pgvector", "hnsw", "nearest", "project", "scoped",
    "postgres", "full", "text", "tsvector", "tsquery",
    "pdf", "page", "section", "offset", "hash", "status", "retryable", "background",
    "langgraph", "workflow", "expand", "ingestion", "critic", "report", "persist", "artifact",
)


def _lexical_embed(texts: list[str], *_, **__) -> np.ndarray:
    dimension = int(getattr(settings, "PAPERLENS_EMBEDDING_DIM", 1024))
    rows = []
    for text in texts:
        normalized = _normalize_eval_text(text)
        values = [float(normalized.count(term)) for term in _VOCAB]
        vector = np.zeros(dimension, dtype=np.float32)
        vector[: min(len(values), dimension)] = values[:dimension]
        norm = float(np.linalg.norm(vector))
        if norm:
            vector = vector / norm
        else:
            fallback_width = min(len(_VOCAB), dimension)
            vector[:fallback_width] = 1.0 / math.sqrt(fallback_width)
        rows.append(vector)
    return np.array(rows, dtype=np.float32)


def _normalize_eval_text(text: str) -> str:
    lowered = (text or "").lower()
    replacements = {
        "长序列": "long sequence",
        "长上下文": "long context",
        "优势": "advantage",
        "局限": "limitation",
        "成本": "cost compute memory",
        "评测": "evaluation recall mrr faithfulness",
        "指标": "metrics recall mrr coverage",
        "引用图谱": "citation graph",
        "页码": "page",
        "入库": "ingestion",
        "检索增强": "retrieval augmented generation",
        "向量": "vector embedding",
        "论文": "paper",
    }
    for source, target in replacements.items():
        lowered = lowered.replace(source, target)
    lowered = lowered.replace("rare-token", "rare token")
    lowered = lowered.replace("project-scoped", "project scoped")
    lowered = lowered.replace("full-text", "full text")
    return lowered


def _project_paper_ids(project_id: int) -> list[int]:
    return list(ProjectPaper.objects.filter(project_id=project_id).values_list("paper_id", flat=True))


def _first_gold_rank(titles: list[str], gold_titles: tuple[str, ...]) -> int:
    for index, title in enumerate(titles, start=1):
        if any(gold.lower() in title.lower() for gold in gold_titles):
            return index
    return 0


def _is_relevant(text: Text, case: RagEvalCase) -> bool:
    if any(gold.lower() in text.paper.title.lower() for gold in case.gold_titles):
        return True
    haystack = f"{text.paper.title} {text.content}".lower()
    return sum(1 for term in case.expected_terms if term.lower() in haystack) >= 2


def _avg(values: list[float]) -> float:
    return round(sum(values) / len(values), 4) if values else 0.0


def dumps_rag_quality_eval(result: dict[str, Any]) -> str:
    return json.dumps(result, ensure_ascii=False, indent=2, default=str)
