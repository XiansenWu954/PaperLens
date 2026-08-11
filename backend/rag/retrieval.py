"""Hybrid retrieval plus RCS evidence scoring."""
from __future__ import annotations

import asyncio
import logging
import re
import time
from collections import defaultdict
from typing import Any

import numpy as np
from asgiref.sync import sync_to_async
from django.conf import settings
from django.db import connection
from pydantic import BaseModel

from .embedding import embed, embedding_metadata, get_provider
from .models import Evidence, Text
from .store import NumpyVectorStore

logger = logging.getLogger(__name__)

SCORE_CUTOFF = 1


class RCSResult(BaseModel):
    score: int
    summary: str


RCS_PROMPT = """You are scoring whether a paper passage answers a research question.

Question:
{question}

Passage ({docname}):
{chunk}

Return strict JSON only:
{{"score": 0-10, "summary": "one concise evidence summary in the user's language"}}

Scoring:
- 0 means unrelated.
- 5 means partially relevant.
- 10 means directly answers the question.
- Do not invent facts beyond the passage.
"""


async def retrieve_evidence(
    question: str,
    paper_ids: list[int] | None = None,
    k: int | None = None,
    store: NumpyVectorStore | None = None,
) -> list[Evidence]:
    """Retrieve project-scoped candidates, then score them with RCS."""

    final_k = k or int(getattr(settings, "PAPERLENS_RAG_FINAL_K", 8))
    candidates = await hybrid_retrieve_texts(
        question,
        paper_ids=paper_ids,
        final_k=max(final_k * 2, final_k),
        store=store,
    )
    if not candidates:
        logger.warning(
            "RAG retrieval returned no candidates",
            extra={"event": "rag_no_candidates", "status": "empty"},
        )
        return []

    async def _rcs(text: Text) -> Evidence:
        return await _rcs_summary(question, text)

    scored = await _gather_with_concurrency(4, candidates, _rcs)
    evidences = [e for e in scored if e.score > SCORE_CUTOFF]
    evidences.sort(key=lambda e: e.score, reverse=True)
    selected = evidences[:final_k]
    logger.info(
        "RAG evidence selected",
        extra={
            "event": "rag_evidence_selected",
            "candidate_count": len(candidates),
            "selected_evidence_count": len(selected),
            "status": "done" if selected else "empty",
        },
    )
    return selected


async def hybrid_retrieve_texts(
    question: str,
    paper_ids: list[int] | None = None,
    final_k: int | None = None,
    store: NumpyVectorStore | None = None,
) -> list[Text]:
    """Return fused dense + lexical candidates using RRF."""

    started = time.perf_counter()
    dense_k = int(getattr(settings, "PAPERLENS_RAG_DENSE_K", 20))
    lexical_k = int(getattr(settings, "PAPERLENS_RAG_LEXICAL_K", 20))
    limit = final_k or int(getattr(settings, "PAPERLENS_RAG_FINAL_K", 8))
    rrf_k = int(getattr(settings, "PAPERLENS_RAG_RRF_K", 60))
    query_vec = (await sync_to_async(embed, thread_sensitive=False)([question], input_type="query"))[0]
    meta = embedding_metadata()

    if connection.vendor == "postgresql":
        try:
            dense, lexical = await sync_to_async(_postgres_hybrid_candidates)(
                question, query_vec, paper_ids, dense_k, lexical_k, meta
            )
            backend = "postgres_pgvector_fts"
        except Exception as exc:
            logger.warning(
                "Postgres hybrid retrieval failed; using Python fallback",
                extra={
                    "event": "hybrid_retrieval_postgres_fallback",
                    "error": exc.__class__.__name__,
                    "status": "fallback",
                },
            )
            dense, lexical = await sync_to_async(_python_hybrid_candidates)(
                question, query_vec, paper_ids, dense_k, lexical_k, store, meta
            )
            backend = "python_fallback"
    else:
        dense, lexical = await sync_to_async(_python_hybrid_candidates)(
            question, query_vec, paper_ids, dense_k, lexical_k, store, meta
        )
        backend = "python_fallback"

    # 若 provider 支持 sparse（BGE-M3），用 sparse 词级权重重排 dense 候选作为 lexical 路
    provider = get_provider()
    if hasattr(provider, "encode_query_sparse") and dense:
        query_sparse = await sync_to_async(provider.encode_query_sparse)(question)
        if query_sparse:
            lexical = _sparse_rerank(dense, query_sparse, lexical_k)
            backend = backend.replace("fts", "sparse") if "fts" in backend else "python_sparse"

    fused = rrf_fuse(dense, lexical, limit=limit, rrf_k=rrf_k)
    duration_ms = round((time.perf_counter() - started) * 1000, 2)
    logger.info(
        "hybrid retrieval completed",
        extra={
            "event": "hybrid_retrieval",
            "retrieval_backend": backend,
            "dense_candidates_count": len(dense),
            "lexical_candidates_count": len(lexical),
            "fused_candidates_count": len(fused),
            "selected_evidence_count": min(len(fused), limit),
            "embedding_model": meta["embedding_model"],
            "embedding_dim": meta["embedding_dim"],
            "embedding_version": meta["embedding_version"],
            "retrieval_duration_ms": duration_ms,
            "duration_ms": duration_ms,
            "status": "done" if fused else "empty",
        },
    )
    return fused


def _sparse_rerank(dense: list[Text], query_sparse: dict[str, float], k: int) -> list[Text]:
    """对 dense 候选按 sparse 词级权重点积重排，作为 lexical 路。

    query_sparse: {token_id_str: weight}（来自 BGE-M3 query 编码）。
    每个 Text 的 sparse_weights 也是 {token_id_str: weight}，点积越大越相关。
    """
    scored = []
    for text in dense:
        doc_sparse = getattr(text, "sparse_weights", None) or {}
        if not doc_sparse:
            continue
        score = sum(w * doc_sparse.get(tok, 0.0) for tok, w in query_sparse.items())
        scored.append((score, text))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [t for _s, t in scored[:k]]


def rrf_fuse(
    dense: list[Text],
    lexical: list[Text],
    *,
    limit: int,
    rrf_k: int = 60,
) -> list[Text]:
    """Reciprocal Rank Fusion over dense and lexical candidate lists."""

    scores: dict[str, float] = defaultdict(float)
    items: dict[str, Text] = {}
    for ranked in (dense, lexical):
        for rank, text in enumerate(ranked, start=1):
            key = _text_key(text)
            scores[key] += 1.0 / (rrf_k + rank)
            items[key] = text
    ordered = sorted(scores.items(), key=lambda item: item[1], reverse=True)
    return [items[key] for key, _score in ordered[:limit]]


def _postgres_hybrid_candidates(
    question: str,
    query_vec: np.ndarray,
    paper_ids: list[int] | None,
    dense_k: int,
    lexical_k: int,
    meta: dict[str, Any],
) -> tuple[list[Text], list[Text]]:
    dense_ids = _postgres_dense_ids(query_vec, paper_ids, dense_k, meta)
    lexical_ids = _postgres_lexical_ids(question, paper_ids, lexical_k, meta)
    return _texts_by_ids(dense_ids), _texts_by_ids(lexical_ids)


def _postgres_dense_ids(
    query_vec: np.ndarray, paper_ids: list[int] | None, k: int,
    meta: dict[str, Any],
) -> list[int]:
    where, params = _paper_scope_sql(paper_ids)
    scope = f"{where} AND" if where else "WHERE"
    sql = f"""
        SELECT id
        FROM rag_text
        {scope} embedding_model = %s AND embedding_version = %s
        ORDER BY embedding <=> %s::vector
        LIMIT %s
    """
    params.extend([meta["embedding_model"], meta["embedding_version"],
                   _vector_literal(query_vec), k])
    with connection.cursor() as cursor:
        cursor.execute(sql, params)
        return [row[0] for row in cursor.fetchall()]


def _postgres_lexical_ids(
    question: str, paper_ids: list[int] | None, k: int,
    meta: dict[str, Any],
) -> list[int]:
    where, params = _paper_scope_sql(paper_ids, prefix="WHERE")
    scope = f"{where} AND" if where else "WHERE"
    sql = f"""
        SELECT id
        FROM rag_text
        {scope} embedding_model = %s AND embedding_version = %s
          AND to_tsvector('english', search_vector) @@ plainto_tsquery('english', %s)
        ORDER BY ts_rank_cd(to_tsvector('english', search_vector), plainto_tsquery('english', %s)) DESC
        LIMIT %s
    """
    params.extend([meta["embedding_model"], meta["embedding_version"],
                   question, question, k])
    with connection.cursor() as cursor:
        cursor.execute(sql, params)
        return [row[0] for row in cursor.fetchall()]


def _paper_scope_sql(paper_ids: list[int] | None, prefix: str = "WHERE") -> tuple[str, list[Any]]:
    """Scope SQL with explicit None/[] semantics (Task 2.6).

    None -> NO scope clause (bottom-level retriever is global by contract; the
           project boundary is enforced by the resolver / project wrapper).
    []   -> FAIL CLOSED: an explicit empty set matches NOTHING. It must never
           degrade to a full-library query.
    """
    if paper_ids is None:
        return "", []
    return f"{prefix} paper_id = ANY(%s)", [paper_ids]


def _texts_by_ids(ids: list[int]) -> list[Text]:
    if not ids:
        return []
    rows = {text.id: text for text in Text.objects.filter(id__in=ids).select_related("paper", "paper__venue")}
    return [rows[text_id] for text_id in ids if text_id in rows]


def _python_hybrid_candidates(
    question: str,
    query_vec: np.ndarray,
    paper_ids: list[int] | None,
    dense_k: int,
    lexical_k: int,
    store: NumpyVectorStore | None,
    meta: dict[str, Any],
) -> tuple[list[Text], list[Text]]:
    qs = Text.objects.select_related("paper", "paper__venue").filter(
        embedding_model=meta["embedding_model"],
        embedding_version=meta["embedding_version"],
    )
    if paper_ids is not None:
        # [] -> empty queryset (fail closed, Task 2.6); never the full library.
        qs = qs.filter(paper_id__in=paper_ids)
    texts = list(qs)
    if not texts:
        return [], []
    # §21.3/§22: a caller-supplied prebuilt store may contain stale / foreign /
    # out-of-scope Texts, and filtering AFTER a Top-K search can starve legal
    # candidates (forbidden items occupying the top K). Rebuild the store from
    # the scoped+active texts for THIS query so dense candidates are searched
    # entirely within the allowed set; the allowed-ids intersection below stays
    # as defense in depth.
    allowed_ids = set(text.id for text in texts)
    dense_store = NumpyVectorStore()
    dense_store.build_from(texts)
    dense = dense_store.search(query_vec, k=dense_k)
    dense = [t for t in dense if t.id in allowed_ids]
    lexical = _python_lexical_search(question, texts, lexical_k)
    return dense, lexical


def _python_lexical_search(question: str, texts: list[Text], k: int) -> list[Text]:
    query_terms = _terms(question)
    if not query_terms:
        return []
    scored: list[tuple[float, Text]] = []
    for text in texts:
        haystack = " ".join([text.search_vector or "", text.content or "", text.docname or ""]).lower()
        terms = _terms(haystack)
        if not terms:
            continue
        overlap = query_terms & terms
        if not overlap:
            continue
        score = sum(1.0 + min(3, haystack.count(term)) * 0.25 for term in overlap)
        scored.append((score, text))
    scored.sort(key=lambda item: item[0], reverse=True)
    return [text for _score, text in scored[:k]]


async def _rcs_summary(question: str, text: Text) -> Evidence:
    from llm.deepseek import DeepSeekClient

    client = DeepSeekClient(max_retries=1)
    prompt = RCS_PROMPT.format(question=question, docname=text.docname, chunk=text.content[:3000])
    response = await sync_to_async(client.complete, thread_sensitive=False)(
        [{"role": "user", "content": prompt}],
        thinking=False,
        max_tokens=300,
        response_format={"type": "json_object"},
    )
    score, summary = _parse_rcs(response["content"])
    evidence = Evidence(
        text=text,
        question=question,
        summary=summary,
        score=score,
        citation_key=text.citation_key,
    )
    logger.info(
        "RCS passage scored",
        extra={
            "event": "rag_rcs_scored",
            "text_id": text.id,
            "paper_id": text.paper_id,
            "score": score,
            "status": "done",
        },
    )
    return evidence


def _parse_rcs(content: str) -> tuple[int, str]:
    import json

    try:
        data = json.loads(content)
        score = int(data.get("score", 0))
        score = max(0, min(10, score))
        summary = str(data.get("summary", "")).strip()
        return score, summary
    except (json.JSONDecodeError, ValueError, TypeError):
        return 0, content[:100]


async def _gather_with_concurrency(limit: int, items: list, fn) -> list:
    sem = asyncio.Semaphore(limit)

    async def _wrap(item):
        async with sem:
            try:
                return await fn(item)
            except Exception as exc:
                logger.warning(
                    "RCS scoring failed",
                    extra={
                        "event": "rag_rcs_failed",
                        "error": exc.__class__.__name__,
                        "status": "error",
                    },
                )
                return None

    tasks = [asyncio.create_task(_wrap(item)) for item in items]
    try:
        results = await asyncio.gather(*tasks)
    except asyncio.CancelledError:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise
    return [result for result in results if result is not None]


def _vector_literal(vector: np.ndarray) -> str:
    return "[" + ",".join(f"{float(item):.8f}" for item in vector.tolist()) + "]"


def _text_key(text: Text) -> str:
    return str(text.id or f"{text.paper_id}:{text.chunk_index}:{text.docname}")


def _terms(text: str) -> set[str]:
    return set(re.findall(r"[a-z0-9][a-z0-9_\-]{1,}", (text or "").lower()))
