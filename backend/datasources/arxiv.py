"""ArXiv 数据源。

免费、无需注册（限流 1 req/3s，2026 起 429 变严——由 ratelimit 控制间隔+退避）。
提供：预印本元数据（title/abstract/year/authors）+ PDF 链接（RAG 全文用）。
注意：ArXiv 返回 Atom XML，需解析。
"""
from __future__ import annotations

import os
import re
import xml.etree.ElementTree as ET

from .base import cached_search, normalize
from .ratelimit import fetch_json

BASE = os.environ.get("ARXIV_BASE_URL", "http://export.arxiv.org/api/query")
NS = {"a": "http://www.w3.org/2005/Atom", "arxiv": "http://arxiv.org/schemas/atom"}


def _parse_entry(entry: ET.Element) -> dict | None:
    def find_text(path: str) -> str:
        el = entry.find(path, NS)
        return el.text.strip() if el is not None and el.text else ""

    arxiv_id_raw = find_text("a:id")  # http://arxiv.org/abs/2401.12345v1
    arxiv_id = arxiv_id_raw.rsplit("/abs/", 1)[-1] if "/abs/" in arxiv_id_raw else arxiv_id_raw
    if not arxiv_id:
        return None
    title = " ".join(find_text("a:title").split())
    abstract = " ".join(find_text("a:summary").split())
    authors = [a.find("a:name", NS).text for a in entry.findall("a:author", NS) if a.find("a:name", NS) is not None]
    published = find_text("a:published")[:4]
    year = int(published) if published.isdigit() else None
    # PDF 链接：link rel="related" type="application/pdf"，或 arxiv id 推导
    pdf_url = f"https://arxiv.org/pdf/{arxiv_id.split('v')[0]}.pdf"
    for link in entry.findall("a:link", NS):
        if (link.get("title") == "pdf") or (link.get("type") == "application/pdf"):
            href = link.get("href") or ""
            if href:
                pdf_url = href
                break
    doi = find_text("arxiv:doi") or None
    return normalize(
        source="arxiv",
        source_id=arxiv_id,
        title=title,
        abstract=abstract,
        year=year,
        authors=authors,
        venue="arXiv",
        citation_count=0,
        doi=doi,
        arxiv_id=arxiv_id.split("v")[0],
        pdf_url=pdf_url,
        raw={"arxiv_id": arxiv_id},
    )


async def _fetch(query: str, max_results: int) -> list[dict]:
    params = {"search_query": f"all:{query}", "max_results": str(max_results)}
    text = await fetch_json("arxiv", BASE, params=params)
    if not isinstance(text, str):
        text = str(text)
    # ArXiv 返回 XML（非 JSON），fetch_json 回退为 text
    results: list[dict] = []
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return results
    for entry in root.findall("a:entry", NS):
        parsed = _parse_entry(entry)
        if parsed:
            results.append(parsed)
    return results[:max_results]


class ArxivSearcher:
    name = "arxiv"

    async def search(self, query: str, max_results: int = 10) -> list[dict]:
        results, _hit = await cached_search(
            self.name, query, _fetch, max_results=max_results
        )
        return results
