"""DeepEval 评判模型接入：让 DeepEval 的指标用 DeepSeek 作 judge。

DeepEval 4.x 通过 DeepEvalBaseLLM 抽象类支持自定义评判模型。这里封装 DeepSeekClient
为 DeepEval 可用的 judge，所有 RAG/Agent 指标（FaithfulnessMetric 等）共享同一 judge。

优势：DeepSeek 中文能力强、成本可控、与生产模型同源（self-judge 风险见 metrics 说明）。
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

_judge_singleton: Any | None = None


def make_judge():
    """返回复用的 DeepSeek 评判模型实例（惰性初始化，单例）。"""
    global _judge_singleton
    if _judge_singleton is not None:
        return _judge_singleton
    try:
        from deepeval.models import DeepEvalBaseLLM
    except ImportError:
        logger.warning("deepeval 未安装，judge 不可用；退回 None")
        return None

    from llm.deepseek import DeepSeekClient

    class DeepSeekJudgeModel(DeepEvalBaseLLM):
        """把 DeepSeekClient 适配为 DeepEval 评判模型。"""

        def __init__(self) -> None:
            self._client = DeepSeekClient()

        def load_model(self) -> Any:
            return self._client

        def get_model_name(self) -> str:
            return "deepseek-v4-flash (judge)"

        def generate(self, prompt: str, **kwargs) -> str:
            """同步生成，DeepEval 指标核心入口。thinking=False 降本。"""
            schema = kwargs.get("schema")
            response_format = {"type": "json_object"} if schema else None
            r = self._client.complete(
                [{"role": "user", "content": prompt}],
                thinking=False,
                max_tokens=kwargs.get("max_tokens", 1200),
                response_format=response_format,
            )
            content = r["content"]
            # DeepEval 部分指标期望 pydantic schema 对象；若提供 schema，尝试 JSON 解析
            if schema:
                import json

                try:
                    data = json.loads(content)
                    return data
                except (json.JSONDecodeError, ValueError):
                    return content
            return content

        async def a_generate(self, prompt: str, **kwargs) -> str:
            """异步生成（DeepEval 4.x 要求实现）。当前同步执行，简单可用。"""
            return self.generate(prompt, **kwargs)

        def supports_json_mode(self) -> bool:
            return True

    _judge_singleton = DeepSeekJudgeModel()
    logger.info("deepeval judge 已就绪: deepseek-v4-flash")
    return _judge_singleton


def judge_available() -> bool:
    """DeepSeek key 是否配置（judge 是否可用）。"""
    import os
    return bool(os.environ.get("DEEPSEEK_API_KEY", ""))
