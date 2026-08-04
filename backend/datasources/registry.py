"""数据源注册表：多源并发检索 + 去重。

缝合依据：gpt-researcher context_manager.py 的 set().union 去重模式。
多个源并发调用 search()，按 dedupe_source_id 去重后返回。
"""
from __future__ import annotations

import asyncio
import logging
import os

from .arxiv import ArxivSearcher
from .base import PaperSearcher, dedupe_source_id
from .dblp import DblpSearcher
from .openalex import OpenAlexSearcher
from .semantic_scholar import SemanticScholarSearcher

logger = logging.getLogger(__name__)

# 源注册
REGISTRY: dict[str, PaperSearcher] = {
    "openalex": OpenAlexSearcher(),
    "arxiv": ArxivSearcher(),
    "dblp": DblpSearcher(),
    "semantic_scholar": SemanticScholarSearcher(),
}

# 默认启用源
# 注：dblp 在本机代理环境 TLS 握手不可达（curl 直连亦 SSL_ERROR_SYSCALL），
# 但 OpenAlex 已提供 venue/author 元数据，可替代 DBLP 作用。DBLP 实现保留，
# 网络可达时显式传入 sources=["dblp"] 即可用。S2 匿名层慢，默认不开。
DEFAULT_SOURCES = ["dblp", "openalex", "arxiv"]
SOURCE_TIMEOUT_SECONDS = float(os.environ.get("PAPERLENS_SOURCE_TIMEOUT_SECONDS", "12"))


async def search(
    query: str,
    sources: list[str] | None = None,
    max_results: int = 10,
) -> list[dict]:
    """并发查询多源，去重后返回。

    sources 为 None 则用 DEFAULT_SOURCES。每源独立 max_results，去重后整体截断。
    单源失败不阻断其他源（记录 warning）。
    """
    srcs = sources or DEFAULT_SOURCES
    logger.info(
        "datasource registry search started",
        extra={
            "event": "datasource_registry_started",
            "query_preview": query[:120],
            "sources": srcs,
            "max_results": max_results,
        },
    )
    searchers = [REGISTRY[s] for s in srcs if s in REGISTRY]

    async def _safe(s: PaperSearcher) -> list[dict]:
        try:
            return await asyncio.wait_for(
                s.search(query, max_results=max_results),
                timeout=SOURCE_TIMEOUT_SECONDS,
            )
        except TimeoutError:
            logger.warning(
                "datasource source timed out",
                extra={
                    "event": "datasource_source_timeout",
                    "source": s.name,
                    "query_preview": query[:120],
                    "timeout_seconds": SOURCE_TIMEOUT_SECONDS,
                },
            )
            return []
        except Exception as e:
            logger.warning(
                "datasource source failed",
                extra={
                    "event": "datasource_source_failed",
                    "source": s.name,
                    "query_preview": query[:120],
                    "error": e.__class__.__name__,
                },
            )
            return []

    batches = await asyncio.gather(*[_safe(s) for s in searchers])

    # 去重（gpt-researcher 模式）：保留首次出现
    seen: set[str] = set()
    merged: list[dict] = []
    for batch in batches:
        for p in batch:
            key = dedupe_source_id(p)
            if key in seen:
                continue
            seen.add(key)
            merged.append(p)
    final = merged[:max_results]
    logger.info(
        "datasource registry search completed",
        extra={
            "event": "datasource_registry_completed",
            "query_preview": query[:120],
            "sources": srcs,
            "results": len(final),
        },
    )
    return final
