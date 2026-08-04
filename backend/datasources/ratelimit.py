"""限流退避基础设施。

统一封装 httpx 异步请求，捕获 HTTP 429，按 Retry-After 头或指数退避重试。
每源独立最小间隔（ArXiv/S2 3s，DBLP 1s，OpenAlex 无强制——礼貌池 10/s 带 mailto）。

地基验证发现：ArXiv 2026 起 429 变严，DBLP 需 User-Agent 头修 SSL EOF。
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import httpx

logger = logging.getLogger(__name__)

# 每源最小请求间隔（秒）。控制对礼貌限流源的请求频率。
# OpenAlex 礼貌池密集请求会触发 429（Retry-After 数小时），加间隔规避。
SOURCE_MIN_INTERVAL: dict[str, float] = {
    "openalex": 2.0,
    "arxiv": 3.0,
    "dblp": 1.0,
    "semantic_scholar": 3.0,
}

# 默认 User-Agent（部分源如 DBLP 在缺少 UA 时会 SSL EOF）
DEFAULT_HEADERS = {
    "User-Agent": "PaperLens/0.1 (academic paper research agent; +mailto:paperlens.dev@example.com)",
    "Accept": "application/json",
}

# 每源上次的请求时间戳（运行期内存状态，控制最小间隔）
_last_request_at: dict[str, float] = {}


class RateLimitError(Exception):
    """重试耗尽仍被限流。"""


async def _respect_interval(source: str) -> None:
    """等待以满足该源的最小请求间隔。"""
    interval = SOURCE_MIN_INTERVAL.get(source, 0.0)
    if interval <= 0:
        return
    last = _last_request_at.get(source)
    if last is not None:
        elapsed = asyncio.get_event_loop().time() - last
        if elapsed < interval:
            await asyncio.sleep(interval - elapsed)
    _last_request_at[source] = asyncio.get_event_loop().time()


async def fetch_json(
    source: str,
    url: str,
    *,
    params: dict | None = None,
    headers: dict | None = None,
    timeout: float = 30.0,
    max_retries: int = 4,
    base_delay: float = 1.0,
    max_delay: float = 30.0,
) -> Any:
    """发起带限流退避的 GET，返回解析后的 JSON。

    - 429：读 Retry-After，否则指数退避（base_delay * 2^attempt，上限 max_delay）。
    - 5xx/超时：同样指数退避重试。
    - max_retries 次仍失败抛 RateLimitError / 原异常。
    """
    h = {**DEFAULT_HEADERS, **(headers or {})}
    attempt = 0
    last_exc: Exception | None = None
    started = time.perf_counter()
    logger.info(
        "external fetch started",
        extra={
            "event": "external_fetch_started",
            "source": source,
            "url": url,
            "max_retries": max_retries,
        },
    )
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
        while attempt <= max_retries:
            await _respect_interval(source)
            try:
                resp = await client.get(url, params=params, headers=h)
            except (httpx.TimeoutException, httpx.TransportError) as e:
                last_exc = e
                logger.warning(
                    "external fetch transport error",
                    extra={
                        "event": "external_fetch_transport_error",
                        "source": source,
                        "attempt": attempt,
                        "error": e.__class__.__name__,
                    },
                )
            else:
                if resp.status_code == 200:
                    logger.info(
                        "external fetch completed",
                        extra={
                            "event": "external_fetch_completed",
                            "source": source,
                            "status": resp.status_code,
                            "attempt": attempt,
                            "duration_ms": round((time.perf_counter() - started) * 1000, 2),
                        },
                    )
                    try:
                        return resp.json()
                    except Exception:
                        return resp.text
                if resp.status_code == 429:
                    last_exc = RateLimitError(f"{source} 429")
                    # 优先用 Retry-After
                    ra = resp.headers.get("Retry-After")
                    delay = float(ra) if ra and ra.isdigit() else min(
                        max_delay, base_delay * (2**attempt)
                    )
                    logger.warning(
                        "external fetch rate limited",
                        extra={
                            "event": "external_fetch_rate_limited",
                            "source": source,
                            "status": resp.status_code,
                            "retry_after": delay,
                            "attempt": attempt,
                        },
                    )
                    await asyncio.sleep(delay)
                    attempt += 1
                    continue
                if 500 <= resp.status_code < 600:
                    last_exc = httpx.HTTPStatusError(
                        f"{source} {resp.status_code}", request=resp.request, response=resp
                    )
                else:
                    # 4xx 非 429：不重试，直接抛
                    resp.raise_for_status()
            # 5xx/超时 退避
            delay = min(max_delay, base_delay * (2**attempt))
            await asyncio.sleep(delay)
            attempt += 1
    logger.error(
        "external fetch failed",
        extra={
            "event": "external_fetch_failed",
            "source": source,
            "duration_ms": round((time.perf_counter() - started) * 1000, 2),
            "error": last_exc.__class__.__name__ if last_exc else "RateLimitError",
        },
    )
    raise last_exc if last_exc else RateLimitError(f"{source} retries exhausted")
