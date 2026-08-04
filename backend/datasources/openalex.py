"""OpenAlex 数据源（主源）。

免费、无需注册（带 mailto 进礼貌池约 10 req/s）。提供：
- 元数据（title/year/authors/venue/citation_count/doi）
- ★ referenced_works（论文引用列表）—— 引用图谱 bibliographic coupling 数据源
  （地基验证：样本返回 95 条，字段通路成立）
- 摘要（abstract_inverted_index 倒排索引，需解码；部分论文为空需其他源补）
- CS 概念筛选（concept C41008148 = Computer Science）
"""
from __future__ import annotations

import os

from .base import cached_search, normalize
from .ratelimit import fetch_json

BASE = os.environ.get("OPENALEX_BASE_URL", "https://api.openalex.org")
EMAIL = os.environ.get("OPENALEX_EMAIL", "paperlens.dev@example.com")
CS_CONCEPT = "C41008148"  # Computer Science


def _decode_inverted_index(inv: dict | None) -> str:
    """OpenAlex 摘要存为倒排索引 {word: [positions]}，解码回原文。"""
    if not inv:
        return ""
    positions: list[tuple[int, str]] = []
    for word, idxs in inv.items():
        for i in idxs:
            positions.append((i, word))
    positions.sort()
    return " ".join(w for _, w in positions)


def _parse_work(w: dict) -> dict:
    authorships = w.get("authorships") or []
    authors = [a.get("author", {}).get("display_name", "") for a in authorships]
    venue_obj = w.get("primary_location", {}) or {}
    venue = (venue_obj.get("source") or {}).get("display_name", "") if venue_obj else ""
    # OpenAlex work id 形如 https://openalex.org/W123 -> W123
    oa_id = (w.get("id") or "").rsplit("/", 1)[-1] or None
    doi_raw = w.get("doi") or ""
    doi = doi_raw.replace("https://doi.org/", "") if doi_raw else None
    best_oa = w.get("best_oa_location") or {}
    pdf_url = best_oa.get("pdf_url") if best_oa else None
    return normalize(
        source="openalex",
        source_id=oa_id or doi or "",
        title=w.get("title") or "",
        abstract=_decode_inverted_index(w.get("abstract_inverted_index")),
        year=w.get("publication_year"),
        authors=authors,
        venue=venue,
        citation_count=w.get("cited_by_count", 0) or 0,
        doi=doi,
        openalex_id=oa_id,
        pdf_url=pdf_url,
        referenced_works=w.get("referenced_works") or [],
        raw=w,
    )


async def _fetch(query: str, max_results: int) -> list[dict]:
    params = {
        "search": query,
        "filter": f"concepts.id:{CS_CONCEPT}",
        "per-page": max_results,
        "mailto": EMAIL,
    }
    data = await fetch_json("openalex", f"{BASE}/works", params=params)
    works = data.get("results", []) if isinstance(data, dict) else []
    return [_parse_work(w) for w in works[:max_results]]


class OpenAlexSearcher:
    name = "openalex"

    async def search(self, query: str, max_results: int = 10) -> list[dict]:
        results, _hit = await cached_search(
            self.name, query, _fetch, max_results=max_results
        )
        return results
