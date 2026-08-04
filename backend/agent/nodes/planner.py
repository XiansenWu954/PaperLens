"""planner 节点：问题 → sub_queries（缝合 open_deep_research write_research_brief）。

用 DeepSeek thinking=False 降本（结构化输出不需要思维链），Pydantic 解析。
"""
from __future__ import annotations

import json
import logging

from pydantic import BaseModel, ValidationError

from ..config import AgentConfig
from ..prompts import PLANNER_SYSTEM
from ..state import AgentState

logger = logging.getLogger(__name__)


class ResearchPlan(BaseModel):
    sub_queries: list[str]


async def planner(state: AgentState, config: AgentConfig) -> dict:
    """分解研究问题为 sub_queries。"""
    from llm.deepseek import DeepSeekClient

    question = state["question"]
    client = DeepSeekClient(model=config.planner_model)
    system = PLANNER_SYSTEM.format(max_sub_queries=config.max_sub_queries)
    r = client.complete(
        [{"role": "system", "content": system}, {"role": "user", "content": question}],
        thinking=config.planner_thinking,
        max_tokens=512,
    )
    plan = _parse_plan(r["content"], max_sub_queries=config.max_sub_queries)
    logger.info("planner -> %d sub_queries: %s", len(plan), plan)
    return {"plan": plan}


def _parse_plan(content: str, max_sub_queries: int) -> list[str]:
    """解析 planner 输出为 sub_queries 列表，容错 JSON。"""
    text = content.strip()
    # 尝试抽取 JSON
    if "```" in text:
        # 去掉 markdown 包裹
        text = text.split("```")[-2] if text.count("```") >= 2 else text
        text = text.replace("json", "", 1).strip()
    try:
        data = json.loads(text)
        queries = data.get("sub_queries", [])
    except json.JSONDecodeError:
        # 容错：按行分割
        queries = [ln.strip("- ").strip() for ln in text.splitlines() if ln.strip()]
    queries = [q for q in queries if q and isinstance(q, str)]
    return queries[:max_sub_queries]
