"""评测 variants：baseline（朴素搜索）vs PaperLens（图谱+RAG）。

两者用同一 DeepSeek 模型，控制变量。baseline 不走图谱/RAG/multi-agent，
仅 datasources.search 直出 → DeepSeek 摘要，体现 PaperLens 增量的价值。
"""
from __future__ import annotations

import logging
from typing import Awaitable, Callable

logger = logging.getLogger(__name__)


async def baseline_research(question: str) -> dict:
    """baseline：datasources.search 直出 top-5 → DeepSeek 摘要（无图谱无 RAG）。"""
    from asgiref.sync import sync_to_async
    from datasources.registry import search as registry_search
    from llm.deepseek import DeepSeekClient

    results = await registry_search(question, max_results=5)
    # 入库（与 paperlens 一致的本地库）
    from papers.models import upsert_paper
    await sync_to_async(lambda: [upsert_paper(p) for p in results])()

    # 摘要（无 RAG，只用 title+abstract 元数据）
    client = DeepSeekClient()
    brief = "\n".join(
        f"- {r.get('title','')} ({r.get('year')}) [引用{r.get('citation_count',0)}]: {r.get('abstract','')[:150]}"
        for r in results
    )
    report = client.complete(
        [
            {"role": "system", "content": "你是科研综述助手。基于检索到的论文元数据写一篇简短综述（含来源）。"},
            {"role": "user", "content": f"问题：{question}\n\n论文：\n{brief}\n\n写综述。"},
        ],
        thinking=False,
        max_tokens=1500,
    )["content"]

    titles = [r.get("title", "") for r in results]
    logger.info("baseline %r -> %d 结果, 综述 %d 字", question, len(results), len(report))
    return {"report": report, "sources": results, "retrieved_titles": titles}


async def paperlens_research(question: str) -> dict:
    """PaperLens：完整 agent（planner→researcher→citation_graph→synthesizer）。"""
    from agent.config import DEFAULT_CONFIG
    from agent.graph import build_graph

    graph = build_graph(DEFAULT_CONFIG)
    state = await graph.ainvoke({"question": question})
    sources = state.get("sources", [])
    titles = [s.get("title", "") for s in sources]
    logger.info("paperlens %r -> %d sources, 综述 %d 字", question, len(sources), len(state.get("final_report", "")))
    return {
        "report": state.get("final_report", ""),
        "sources": sources,
        "retrieved_titles": titles,
        "citation_graph": state.get("citation_graph", {}),
    }
