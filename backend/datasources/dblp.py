"""DBLP 数据源（CS 会议/作者元数据补全）。

免费、无需注册（1000 hits/查询，礼貌 1/s）。
工程注记：httpx 走本地代理(7897)对 dblp.org 的 TLS 隧道会 ConnectError（curl 能通），
故本源用 curl 子进程兜底取数——绕过 httpx 代理 TLS 实现问题，结果仍是统一归一化结构。
DBLP 无摘要、无 citation_count，主要补 venue/author 元数据。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import shlex
import urllib.parse

from .base import cached_search, normalize

logger = logging.getLogger(__name__)

BASE = os.environ.get("DBLP_BASE_URL", "https://dblp.org/search/publ/api")
_UA = "PaperLens/0.1 (academic paper research agent)"


def _parse_hit(hit: dict) -> dict | None:
    info = hit.get("info", {})
    title = info.get("title", "")
    if not title:
        return None
    venue = info.get("venue", "")
    year = info.get("year")
    try:
        year_i = int(year)
    except (TypeError, ValueError):
        year_i = None
    authors_raw = info.get("authors", {}).get("author", [])
    if isinstance(authors_raw, dict):  # 单作者时是 dict
        authors_raw = [authors_raw]
    authors = [a.get("text", "") if isinstance(a, dict) else str(a) for a in authors_raw]
    doi = info.get("doi")
    return normalize(
        source="dblp",
        source_id=info.get("key") or doi or title,
        title=title.rstrip("."),
        abstract="",
        year=year_i,
        authors=authors,
        venue=venue,
        citation_count=0,
        doi=doi,
        raw=info,
    )


async def _fetch(query: str, max_results: int) -> list[dict]:
    params = {"q": query, "format": "json", "h": str(max_results)}
    url = f"{BASE}?{urllib.parse.urlencode(params)}"
    # curl 子进程（继承环境代理，已验证能通 dblp）
    cmd = ["curl", "-sS", "-m", "20", "-A", _UA, "-H", "Accept: application/json", url]
    for attempt in range(3):
        proc = None
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
            )
            stdout, stderr = await proc.communicate()
            if proc.returncode == 0 and stdout:
                data = json.loads(stdout)
                hits = data.get("result", {}).get("hits", {}).get("hit", [])
                return [_parse_hit(h) for h in hits if _parse_hit(h)][:max_results]
        except asyncio.CancelledError:
            if proc and proc.returncode is None:
                proc.kill()
                await proc.communicate()
            raise
        except (json.JSONDecodeError, Exception) as e:
            logger.warning("dblp curl attempt %d failed: %s", attempt, e)
        await asyncio.sleep(1.0 * (attempt + 1))
    return []


class DblpSearcher:
    name = "dblp"

    async def search(self, query: str, max_results: int = 10) -> list[dict]:
        results, _hit = await cached_search(
            self.name, query, _fetch, max_results=max_results
        )
        return results
