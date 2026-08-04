"""Agent State 定义（缝合 open_deep_research state.py）。

关键设计：researcher 的完整 ReAct messages 不上浮到 AgentState，
只累加 notes + sources（输入/输出 state 分离，防上下文爆炸）。
"""
from __future__ import annotations

import operator
from typing import Annotated, TypedDict


def add(left, right):
    """累加 reducer：列表拼接，None 容错。"""
    if left is None:
        return right
    if right is None:
        return left
    return left + right


class AgentState(TypedDict, total=False):
    """主图状态。"""

    question: str
    plan: list[str]  # planner 产出的 sub_queries
    notes: Annotated[list[str], add]  # 所有 researcher 累加的笔记
    sources: Annotated[list[dict], add]  # 累加的论文来源
    citation_graph: dict  # 引用图谱可视化数据（★护城河）
    final_report: str


class ResearcherState(TypedDict, total=False):
    """单个 researcher 子图状态。"""

    sub_query: str
    messages: Annotated[list[dict], operator.add]  # ReAct 对话（累加，不上浮）
    tool_call_iterations: int
    notes: Annotated[list[str], add]
    sources: Annotated[list[dict], add]


class ResearcherOutputState(TypedDict, total=False):
    """researcher 子图输出（只这些上浮到 AgentState）。"""

    notes: list[str]
    sources: list[dict]
