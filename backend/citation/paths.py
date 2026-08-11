"""两篇论文之间的引用连接路径（缝合 Inciteful 的 Literature Connector 思路）。

基于 bibliographic coupling 相似图（networkx 无向图），用最短路径找出两篇论文
如何通过共享参考文献网络联系起来。综述写作时回答“A 和 B 怎么连接”。
"""
from __future__ import annotations

import logging
from typing import Any

import networkx as nx

logger = logging.getLogger(__name__)


def find_connection_path(G: nx.Graph, source: int, target: int) -> dict[str, Any]:
    """在相似图上找 source → target 的最短路径。

    返回 {path: [node_ids], nodes: [{id,title,year}], edges: [{source,target,weight}], hops}。
    若不连通返回 {path: [], reachable: false}。
    """
    if source not in G or target not in G:
        return {"path": [], "reachable": False, "reason": "论文不在图谱中"}
    if source == target:
        p = G.nodes[source].get("paper")
        return {
            "path": [source],
            "reachable": True,
            "hops": 0,
            "nodes": [_node_info(source, G)],
            "edges": [],
        }
    try:
        # 用 weight 的倒数（相似度越高权重越小，越优先走）
        path = nx.shortest_path(G, source=source, target=target, weight=_inv_weight)
    except nx.NetworkXNoPath:
        return {"path": [], "reachable": False, "reason": "两篇论文在当前图谱中不连通"}

    nodes = [_node_info(n, G) for n in path]
    edges = []
    for u, v in zip(path, path[1:]):
        edge_data = G.get_edge_data(u, v) or {}
        edges.append({"source": u, "target": v, "weight": edge_data.get("weight", 1)})
    logger.info("connection path %d -> %d: %d hops", source, target, len(path) - 1)
    return {
        "path": path,
        "reachable": True,
        "hops": len(path) - 1,
        "nodes": nodes,
        "edges": edges,
    }


def _inv_weight(u, v, d):
    """networkx 最短路径权重函数：相似度取倒数（高相似 = 低成本）。"""
    return 1.0 / (d.get("weight", 1) or 1)


def _node_info(node_id: int, G: nx.Graph) -> dict[str, Any]:
    p = G.nodes[node_id].get("paper")
    return {
        "id": node_id,
        "title": (p.title[:80] if p else ""),
        "year": (p.year if p else None),
        "citation_count": (p.citation_count if p else 0),
        "arxiv_id": (p.arxiv_id if p else None),
        "doi": (p.doi if p else None),
    }
