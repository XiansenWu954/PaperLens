"""LangGraph 主图编译（缝合 open_deep_research supervisor 模式）。

拓扑：planner → fan_out_researchers(asyncio.gather) → citation_graph → synthesizer
researcher 子图输出裁剪：只 notes + sources 上浮，messages 不上浮（防上下文爆炸）。
citation_graph（★护城河）：基于 sources 建引用图谱，标注三类，注入 synthesizer。
"""
from __future__ import annotations

import asyncio
import logging

from asgiref.sync import sync_to_async
from langgraph.graph import END, START, StateGraph

from .config import AgentConfig, DEFAULT_CONFIG
from .nodes.planner import planner
from .nodes.researcher import researcher
from .nodes.synthesizer import synthesizer
from .state import AgentState

logger = logging.getLogger(__name__)


async def fan_out_researchers(state: AgentState, config: AgentConfig) -> dict:
    """对所有 sub_query 并行跑 researcher 子图，合并 notes + sources 上浮。

    缝合 open_deep_research supervisor_tools 的 asyncio.gather 模式。
    """
    plan = state.get("plan", [])
    if not plan:
        return {"notes": [], "sources": []}

    max_concurrent = config.max_concurrent_researchers
    # 控制并发：用 Semaphore 限制
    sem = asyncio.Semaphore(max_concurrent)

    async def _run(sub_query: str) -> dict:
        async with sem:
            return await researcher({"sub_query": sub_query}, config)

    results = await asyncio.gather(*[_run(q) for q in plan], return_exceptions=True)

    all_notes: list[str] = []
    all_sources: list[dict] = []
    for r in results:
        if isinstance(r, Exception):
            logger.warning(
                "researcher failed",
                extra={"event": "researcher_failed", "error": r.__class__.__name__},
            )
            continue
        all_notes.extend(r.get("notes", []))
        all_sources.extend(r.get("sources", []))

    logger.info(
        "fan out researchers completed",
        extra={
            "event": "fan_out_completed",
            "notes": len(all_notes),
            "sources": len(all_sources),
            "sub_queries": len(plan),
        },
    )
    return {"notes": all_notes, "sources": all_sources}


async def citation_graph_node(state: AgentState, config: AgentConfig) -> dict:
    """★护城河：基于 sources 建引用图谱，标注三类，生成 vis_data + 综述注入摘要。

    缝合 Connected Papers 算法：bibliographic coupling 相似图 + pagerank/年衰减/louvain。
    sources 里的论文已入库（papers.Paper），用其 referenced_works 建图。
    """
    sources = state.get("sources", [])
    if not sources:
        logger.info("citation graph skipped", extra={"event": "citation_graph_skipped"})
        return {"citation_graph": {}}

    # 从 DB 取已入库的 Paper 实例（sources 是 dict，需转 Paper）
    # 注意：所有 ORM 调用必须经 sync_to_async（async 上下文）
    from papers.models import Paper

    def _resolve_seed_ids():
        """同步：把 sources(dict) 解析成已入库的 Paper id 列表。"""
        ids = []
        for s in sources:
            doi = s.get("doi")
            arxiv = s.get("arxiv_id")
            p = None
            if doi:
                p = Paper.objects.filter(doi=doi.lower() if doi else None).first()
            if not p and arxiv:
                p = Paper.objects.filter(arxiv_id=arxiv).first()
            if p:
                ids.append(p.id)
        return list(Paper.objects.filter(id__in=ids))

    seed_papers = await sync_to_async(_resolve_seed_ids)()
    logger.info(
        "citation graph seed resolved",
        extra={"event": "citation_graph_seed_resolved", "seed_papers": len(seed_papers)},
    )
    if len(seed_papers) < 2:
        return {"citation_graph": {}}

    from citation.analyze import label_nodes
    from citation.graph_build import build_similarity_graph
    from citation.visualize import summarize_for_synthesis, to_vis_data

    G = await sync_to_async(build_similarity_graph)(seed_papers)
    labels = await sync_to_async(label_nodes)(G)
    vis_data = await sync_to_async(to_vis_data)(G, labels)

    # 综述注入摘要（让 synthesizer 按三类组织）
    papers_by_id = {p.id: p for p in seed_papers}
    summary = await sync_to_async(summarize_for_synthesis)(labels, papers_by_id)

    logger.info(
        "citation graph completed",
        extra={
            "event": "citation_graph_completed",
            "graph_nodes": len(vis_data["nodes"]),
            "graph_edges": len(vis_data["edges"]),
        },
    )
    return {"citation_graph": {"vis": vis_data, "synthesis_hint": summary}}


def build_graph(config: AgentConfig = DEFAULT_CONFIG):
    """编译主图。节点绑定 config。

    注：节点用 async def 显式包裹（不能用 lambda 返回 coroutine），
    LangGraph 1.x 要求 async 节点本身是 async function。
    """

    async def _planner(s):
        return await planner(s, config)

    async def _fan_out(s):
        return await fan_out_researchers(s, config)

    async def _citation_graph(s):
        return await citation_graph_node(s, config)

    async def _synthesizer(s):
        return await synthesizer(s, config)

    g = StateGraph(AgentState)
    g.add_node("planner", _planner)
    g.add_node("fan_out_researchers", _fan_out)
    g.add_node("citation_graph", _citation_graph)
    g.add_node("synthesizer", _synthesizer)
    g.add_edge(START, "planner")
    g.add_edge("planner", "fan_out_researchers")
    g.add_edge("fan_out_researchers", "citation_graph")
    g.add_edge("citation_graph", "synthesizer")
    g.add_edge("synthesizer", END)
    return g.compile()
