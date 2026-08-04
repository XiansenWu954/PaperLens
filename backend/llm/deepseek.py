"""DeepSeek 客户端封装。

唯一 LLM（用户约束：只用 DeepSeek）。封装：
- 密钥从环境变量读（DEEPSEEK_API_KEY），绝不硬编码/打印。
- reasoning 开关（地基验证发现）：
  * V4-Flash 默认带思维链，约半数 completion token 花在 reasoning。
  * thinking=False 时传 extra_body={"thinking":{"type":"disabled"}} 关闭降本，
    用于简单生成/工具结果处理。
  * thinking=True（默认）保留，用于 Agent 决策/复杂规划。
- Function Calling 支持（complete_with_tools），保留 reasoning。
- 5xx/超时 重试 3 次。

模型名：deepseek-v4-flash / deepseek-v4-pro（旧名 deepseek-chat/reasoner 已停用）。
"""
from __future__ import annotations

import logging
import os
import time
from typing import Any

from openai import OpenAI
from openai import APIError, APIConnectionError, RateLimitError

logger = logging.getLogger(__name__)

DEFAULT_MODEL = os.environ.get("DEEPSEEK_MODEL", "deepseek-v4-flash")
DEFAULT_BASE_URL = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1")
DEFAULT_TIMEOUT_SECONDS = float(os.environ.get("DEEPSEEK_TIMEOUT_SECONDS", "20"))
DISABLE_THINKING = {"type": "disabled"}


class DeepSeekClient:
    """同步客户端（简单；后续 Agent change 若需异步可加 AsyncOpenAI 变体）。"""

    def __init__(
        self,
        model: str = DEFAULT_MODEL,
        base_url: str | None = None,
        api_key: str | None = None,
        max_retries: int = 3,
        timeout_seconds: float | None = None,
    ) -> None:
        self.model = model
        self.max_retries = max_retries
        self._client = OpenAI(
            api_key=api_key or os.environ["DEEPSEEK_API_KEY"],
            base_url=base_url or DEFAULT_BASE_URL,
            timeout=timeout_seconds or DEFAULT_TIMEOUT_SECONDS,
        )

    def complete(
        self,
        messages: list[dict],
        *,
        thinking: bool = False,
        temperature: float = 0.0,
        max_tokens: int = 1024,
        response_format: dict | None = None,
        stop: list[str] | None = None,
    ) -> dict:
        """普通补全。

        thinking=False（默认）关闭思维链降本，用于简单生成。
        thinking=True 保留 reasoning（Agent 决策用）。
        返回 {content, usage, raw}。
        """
        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if not thinking:
            kwargs["extra_body"] = {"thinking": DISABLE_THINKING}
        if response_format:
            kwargs["response_format"] = response_format
        if stop:
            kwargs["stop"] = stop

        data = self._call_with_retry(kwargs)
        msg = data.choices[0].message
        return {
            "content": msg.content or "",
            "reasoning": getattr(msg, "reasoning_content", None),
            "usage": _usage_dict(data),
            "raw": data.model_dump(),
        }

    def complete_with_tools(
        self,
        messages: list[dict],
        tools: list[dict],
        *,
        tool_choice: str | dict = "auto",
        temperature: float = 0.0,
        max_tokens: int = 2048,
        thinking: bool = True,
    ) -> dict:
        """Function Calling 补全。默认保留 reasoning（模型想清楚再调工具更准）。

        thinking=False 时也可关闭降本（但 Function Calling 建议保留）。
        返回 {content, tool_calls, usage}。
        """
        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "tools": tools,
            "tool_choice": tool_choice,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if not thinking:
            kwargs["extra_body"] = {"thinking": DISABLE_THINKING}
        data = self._call_with_retry(kwargs)
        msg = data.choices[0].message
        tool_calls = []
        for tc in (msg.tool_calls or []):
            tool_calls.append(
                {
                    "id": tc.id,
                    "name": tc.function.name,
                    "arguments": tc.function.arguments,
                }
            )
        return {
            "content": msg.content or "",
            "tool_calls": tool_calls,
            "usage": _usage_dict(data),
            "raw": data.model_dump(),
        }

    def _call_with_retry(self, kwargs: dict) -> Any:
        last_exc: Exception | None = None
        for attempt in range(self.max_retries + 1):
            started = time.perf_counter()
            try:
                data = self._client.chat.completions.create(**kwargs)
                logger.info(
                    "llm call completed",
                    extra={
                        "event": "llm_call_completed",
                        "model": self.model,
                        "attempt": attempt,
                        "duration_ms": round((time.perf_counter() - started) * 1000, 2),
                        "usage": _usage_dict(data),
                        "tool_count": len(kwargs.get("tools") or []),
                    },
                )
                return data
            except (APIConnectionError, RateLimitError) as e:
                last_exc = e
                wait = min(2**attempt, 8)
                logger.warning(
                    "llm transient error",
                    extra={
                        "event": "llm_transient_error",
                        "model": self.model,
                        "attempt": attempt,
                        "wait_seconds": wait,
                        "error": e.__class__.__name__,
                    },
                )
                time.sleep(wait)
            except APIError as e:
                # 5xx 类可重试
                if getattr(e, "status_code", 0) and 500 <= e.status_code < 600:
                    last_exc = e
                    logger.warning(
                        "llm server error",
                        extra={
                            "event": "llm_server_error",
                            "model": self.model,
                            "attempt": attempt,
                            "status": e.status_code,
                            "error": e.__class__.__name__,
                        },
                    )
                    time.sleep(min(2**attempt, 8))
                    continue
                raise
        logger.error(
            "llm call failed",
            extra={
                "event": "llm_call_failed",
                "model": self.model,
                "error": last_exc.__class__.__name__ if last_exc else "unknown",
            },
        )
        raise last_exc  # type: ignore[misc]


def _usage_dict(data) -> dict:
    """提取 usage，含 reasoning_tokens（地基发现的关键字段）。"""
    u = getattr(data, "usage", None)
    if u is None:
        return {}
    d = u.model_dump() if hasattr(u, "model_dump") else dict(u)
    return d
