"""researcher 节点：ReAct 子图，调 search_papers 检索并整理笔记。

缝合 open_deep_research researcher 子图：react_agent ⇄ tool_node，tool_call_iterations 预算兜底。
researcher 用 complete_with_tools 保留 reasoning（地基验证：Function Calling 保留 reasoning 更准）。
完整 ReAct messages 不上浮，只 notes + sources 上浮（输入/输出 state 分离）。
"""
from __future__ import annotations

import json
import logging

from ..config import AgentConfig
from ..prompts import RESEARCHER_EXTRACT, RESEARCHER_SYSTEM
from ..state import ResearcherState
from ..tools import execute_tool, get_agent_tools

logger = logging.getLogger(__name__)


async def researcher(state: ResearcherState, config: AgentConfig) -> dict:
    """单 sub_query 的 ReAct 检索 + 笔记提取。

    输出 ResearcherOutputState（notes + sources），不上浮 messages。
    """
    from llm.deepseek import DeepSeekClient

    sub_query = state["sub_query"]
    client = DeepSeekClient(model=config.researcher_model)
    messages = [
        {"role": "system", "content": RESEARCHER_SYSTEM},
        {"role": "user", "content": sub_query},
    ]

    sources: list[dict] = []
    evidence_notes: list[str] = []  # gather_evidence 返回的 grounded 证据
    max_iters = config.max_tool_calls_per_researcher
    iters = 0
    tools = get_agent_tools()

    # ReAct 循环：调工具直到模型不再 tool_call 或达预算
    while iters <= max_iters:
        r = client.complete_with_tools(
            messages, tools, thinking=config.researcher_thinking, max_tokens=2048
        )
        tool_calls = r.get("tool_calls", [])
        # 追加 assistant 消息（含 tool_calls）
        assistant_msg = {"role": "assistant", "content": r.get("content", "")}
        if tool_calls:
            assistant_msg["tool_calls"] = [
                {"id": tc["id"], "type": "function",
                 "function": {"name": tc["name"], "arguments": tc["arguments"]}}
                for tc in tool_calls
            ]
        messages.append(assistant_msg)

        if not tool_calls:
            break  # 模型不再调工具，结束 ReAct
        if iters >= max_iters:
            break  # 达预算，停止调工具

        # 执行工具调用
        for tc in tool_calls:
            try:
                args = json.loads(tc["arguments"]) if tc["arguments"] else {}
            except json.JSONDecodeError:
                args = {}
            result = await execute_tool(tc["name"], args)
            messages.append({"role": "tool", "tool_call_id": tc["id"], "content": result})
            # 按工具类型收集
            if tc["name"] == "search_papers":
                _collect_sources(result, sources)
            elif tc["name"] == "gather_evidence":
                _collect_evidence(result, evidence_notes)
        iters += 1

    # 提取笔记（把 ReAct 历史 + grounded 证据压缩成针对 sub_query 的笔记）
    history = _format_history(messages)
    if evidence_notes:
        history = "## 全文 RAG 证据（带 pqac 引用，优先采用）\n" + "\n".join(evidence_notes) + "\n\n## 检索历史\n" + history
    notes_text = client.complete(
        [
            {"role": "user", "content": RESEARCHER_EXTRACT.format(sub_query=sub_query, history=history)},
        ],
        thinking=False,
        max_tokens=1024,
    )["content"]

    # 去重 sources
    seen = set()
    unique_sources = []
    for s in sources:
        key = s.get("doi") or s.get("arxiv_id") or s.get("title")
        if key and key not in seen:
            seen.add(key)
            unique_sources.append(s)

    logger.info("researcher %r -> %d iters, %d notes, %d sources", sub_query, iters, len(notes_text), len(unique_sources))
    return {"notes": [notes_text], "sources": unique_sources}


def _collect_sources(tool_result_json: str, sources: list[dict]) -> None:
    """从工具返回的 JSON 里收集论文来源（去重前）。"""
    try:
        papers = json.loads(tool_result_json)
    except json.JSONDecodeError:
        return
    if not isinstance(papers, list):
        return
    for p in papers:
        if isinstance(p, dict) and p.get("title"):
            sources.append(p)


def _collect_evidence(tool_result_json: str, evidence_notes: list[str]) -> None:
    """从 gather_evidence 返回的 JSON 里收集 grounded 证据（含 pqac 引用）。"""
    try:
        evidences = json.loads(tool_result_json)
    except json.JSONDecodeError:
        return
    if not isinstance(evidences, list):
        return
    for e in evidences:
        if isinstance(e, dict) and e.get("summary"):
            citation = e.get("citation", "")
            evidence_notes.append(f"[{citation}] {e['summary']}")


def _format_history(messages: list[dict]) -> str:
    """把 ReAct 历史格式化为给 extract 的文本。"""
    lines = []
    for m in messages:
        role = m.get("role", "")
        if role == "user":
            lines.append(f"[用户] {m['content']}")
        elif role == "assistant":
            tc = m.get("tool_calls")
            if tc:
                names = ",".join(t["function"]["name"] for t in tc)
                lines.append(f"[助手] 调用工具: {names}")
            elif m.get("content"):
                lines.append(f"[助手] {m['content'][:500]}")
        elif role == "tool":
            lines.append(f"[工具结果] {m['content'][:800]}")
    return "\n".join(lines)
