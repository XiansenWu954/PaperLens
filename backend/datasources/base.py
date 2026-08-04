"""数据源统一契约与缓存包装。

缝合依据：gpt-researcher 的 retriever 契约（gpt_researcher/retrievers/__init__.py）。
每个源实现统一 search()，返回归一化结构，便于后续作为 Function Calling 工具并去重。
"""
from __future__ import annotations

import logging
import time
from typing import Protocol, runtime_checkable

from asgiref.sync import sync_to_async

from .models import DatasourceCache, query_hash

logger = logging.getLogger(__name__)


def normalize(
    *,
    source: str,
    source_id: str,
    title: str,
    abstract: str = "",
    year: int | None = None,
    authors: list[str] | None = None,
    venue: str = "",
    citation_count: int = 0,
    doi: str | None = None,
    arxiv_id: str | None = None,
    openalex_id: str | None = None,
    pdf_url: str | None = None,
    referenced_works: list[str] | None = None,
    raw: dict | None = None,
) -> dict:
    """归一化单条论文为统一结构。"""
    return {
        "source": source,
        "source_id": source_id,
        "title": (title or "").strip(),
        "abstract": (abstract or "").strip(),
        "year": year,
        "authors": authors or [],
        "venue": (venue or "").strip(),
        "citation_count": citation_count or 0,
        "doi": doi,
        "arxiv_id": arxiv_id,
        "openalex_id": openalex_id,
        "pdf_url": pdf_url,
        "referenced_works": referenced_works or [],
        "raw": raw or {},
    }


def dedupe_source_id(paper: dict) -> str:
    """用于去重的稳定键：优先 doi，其次 arxiv_id，再次 openalex_id/source_id，
    最后归一化 title。"""
    for k in ("doi", "arxiv_id", "openalex_id", "source_id"):
        v = paper.get(k)
        if v and str(v).strip():
            return f"{k}:{str(v).strip().lower()}"
    t = (paper.get("title") or "").strip().lower()
    return f"title:{t}"


@runtime_checkable
class PaperSearcher(Protocol):
    """论文数据源统一接口（缝合 gpt-researcher retriever）。"""

    name: str

    async def search(self, query: str, max_results: int = 10) -> list[dict]:
        """搜索论文，返回归一化列表。"""
        ...


async def cached_search(
    searcher_name: str,
    query: str,
    fetch_fn,
    max_results: int = 10,
    ttl_days: int = 7,
    **params,
) -> tuple[list[dict], bool]:
    """缓存包装：命中即返回（cache hit），未命中调用 fetch_fn 并落库。

    返回 (results, hit)。
    """
    started = time.perf_counter()
    qh = query_hash(searcher_name, query, max_results=max_results, **params)
    cached = await sync_to_async(DatasourceCache.get)(searcher_name, qh, ttl_days=ttl_days)
    if cached is not None:
        logger.info(
            "datasource cache hit",
            extra={
                "event": "datasource_cache_hit",
                "source": searcher_name,
                "query_preview": query[:120],
                "results": len(cached) if isinstance(cached, list) else "-",
                "duration_ms": round((time.perf_counter() - started) * 1000, 2),
            },
        )
        return cached, True
    results = await fetch_fn(query, max_results)
    await sync_to_async(DatasourceCache.set)(searcher_name, qh, results)
    logger.info(
        "datasource cache miss",
        extra={
            "event": "datasource_cache_miss",
            "source": searcher_name,
            "query_preview": query[:120],
            "results": len(results),
            "duration_ms": round((time.perf_counter() - started) * 1000, 2),
        },
    )
    return results, False
