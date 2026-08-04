"""Semantic Scholar 数据源（匿名层备用）。

免费、无需 key（匿名层共享池 ~100 req/5min，限流严——故强制 3s 间隔 + 缓存）。
用途：补全 citation_count、references（引用图谱辅助）。本 change 低频使用。
"""
from __future__ import annotations

import os

from .base import cached_search, normalize
from .ratelimit import fetch_json

BASE = os.environ.get("S2_BASE_URL", "https://api.semanticscholar.org/graph/v1")
FIELDS = "title,abstract,year,authors,citationCount,venue,externalIds,openAccessPdf,references.externalIds"


def _parse(p: dict) -> dict | None:
    title = p.get("title") or ""
    if not title:
        return None
    ext = p.get("externalIds") or {}
    authors = [a.get("name", "") for a in (p.get("authors") or [])]
    oa = p.get("openAccessPdf") or {}
    refs = []
    for r in (p.get("references") or []):
        rid = (r.get("externalIds") or {}).get("CorpusId")
        if rid:
            refs.append(str(rid))
    return normalize(
        source="semantic_scholar",
        source_id=p.get("paperId") or "",
        title=title,
        abstract=p.get("abstract") or "",
        year=p.get("year"),
        authors=authors,
        venue=p.get("venue") or "",
        citation_count=p.get("citationCount", 0) or 0,
        doi=ext.get("DOI"),
        arxiv_id=ext.get("ArXiv"),
        pdf_url=oa.get("url"),
        referenced_works=refs,
        raw=p,
    )


async def _fetch(query: str, max_results: int) -> list[dict]:
    params = {"query": query, "limit": str(max_results), "fields": FIELDS}
    data = await fetch_json("semantic_scholar", f"{BASE}/paper/search", params=params)
    if not isinstance(data, dict):
        return []
    results: list[dict] = []
    for p in data.get("data", []):
        parsed = _parse(p)
        if parsed:
            results.append(parsed)
    return results[:max_results]


class SemanticScholarSearcher:
    name = "semantic_scholar"

    async def search(self, query: str, max_results: int = 10) -> list[dict]:
        results, _hit = await cached_search(
            self.name, query, _fetch, max_results=max_results
        )
        return results
