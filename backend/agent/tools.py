"""Agent 工具层：把 datasources.search 包装成 Function Calling 工具。

researcher 通过 DeepSeek complete_with_tools 调用 search_papers，
结果归一化并入 papers 本地库（本地库存约束）。
"""
from __future__ import annotations

import json
import logging

from asgiref.sync import sync_to_async

logger = logging.getLogger(__name__)

SEARCH_PAPERS_TOOL = {
    "type": "function",
    "function": {
        "name": "search_papers",
        "description": "搜索计算机科学论文。返回标题/作者/年份/引用数/doi/arxiv_id 等元数据。用于回答研究问题前检索相关文献。",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "搜索关键词或短语，如 'Mamba state space model'",
                },
                "max_results": {
                    "type": "integer",
                    "description": "最大返回论文数，默认 5",
                },
            },
            "required": ["query"],
        },
    },
}

# gather_evidence 工具延迟导入 schema（避免循环依赖）
def _gather_evidence_tool() -> dict:
    from rag.evidence import GATHER_EVIDENCE_TOOL
    return GATHER_EVIDENCE_TOOL


def get_agent_tools() -> list[dict]:
    """获取 researcher 可用工具列表（含全文 RAG 证据工具）。"""
    return [SEARCH_PAPERS_TOOL, _gather_evidence_tool()]


# 向后兼容
AGENT_TOOLS = [SEARCH_PAPERS_TOOL]


def _to_brief(p: dict) -> dict:
    """把归一化论文裁剪成喂给 LLM 的精简结构（省 token）。"""
    return {
        "title": p.get("title"),
        "authors": (p.get("authors") or [])[:3],
        "year": p.get("year"),
        "venue": p.get("venue"),
        "citation_count": p.get("citation_count", 0),
        "doi": p.get("doi"),
        "arxiv_id": p.get("arxiv_id"),
        "abstract": (p.get("abstract") or "")[:300],
    }


def _upsert_batch(results: list[dict]) -> int:
    """同步把检索结果 upsert 入 papers 本地库。"""
    from papers.models import upsert_paper

    n = 0
    for p in results:
        try:
            upsert_paper(p)
            n += 1
        except Exception as e:
            logger.warning("upsert paper failed: %s", e)
    return n


async def execute_tool(tool_name: str, args: dict) -> str:
    """执行一个工具调用，返回 JSON 字符串结果。"""
    if tool_name == "search_papers":
        query = args.get("query", "")
        max_results = int(args.get("max_results", 5))
        if not query:
            return json.dumps({"error": "query is required"}, ensure_ascii=False)

        from datasources.registry import search as registry_search

        results = await registry_search(query, max_results=max_results)
        # 入本地库（本地库存约束 + 后续引用图谱用）
        n = await sync_to_async(_upsert_batch)(results)
        logger.info(
            "agent tool search_papers completed",
            extra={
                "event": "agent_tool_search_completed",
                "tool": "search_papers",
                "query_preview": query[:120],
                "results": len(results),
                "upserted": n,
            },
        )
        return json.dumps([_to_brief(r) for r in results], ensure_ascii=False)

    if tool_name == "gather_evidence":
        from rag.evidence import gather_evidence

        question = args.get("question", "")
        if not question:
            return json.dumps({"error": "question is required"}, ensure_ascii=False)
        logger.info(
            "agent tool gather_evidence started",
            extra={
                "event": "agent_tool_gather_evidence_started",
                "tool": "gather_evidence",
                "question_preview": question[:120],
            },
        )
        return await gather_evidence(question)

    return json.dumps({"error": f"unknown tool {tool_name}"}, ensure_ascii=False)
