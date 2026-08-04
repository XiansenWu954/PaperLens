"""三类节点标注（缝合 Connected Papers 视觉编码理念）。

- 奠基性根节点：相似图上的 pagerank（重要性/中心性）
- 最新前沿：pagerank × 年份衰减（重要且新）
- 子主题簇：louvain 社区检测
"""
from __future__ import annotations

import logging
from typing import Any

import networkx as nx

logger = logging.getLogger(__name__)


def _percentile(values: list[float], p: int) -> float:
    """简单分位数（p=80 → 第80百分位）。values 可含 dict.values()。"""
    nums = sorted(float(v) for v in values)
    if not nums:
        return 0.0
    idx = int(len(nums) * p / 100)
    idx = min(idx, len(nums) - 1)
    return nums[idx]


def label_nodes(G: nx.Graph, current_year: int = 2026) -> dict[int, dict[str, Any]]:
    """标注三类：奠基性/最新前沿/子主题簇。

    返回 {node_id: {seminal, frontier, cluster, is_root, is_frontier, year}}。
    """
    if G.number_of_nodes() == 0:
        return {}

    # pagerank（重要性）
    try:
        pr = nx.pagerank(G, weight="weight")
    except (ZeroDivisionError, nx.PowerIterationFailedConvergence):
        pr = {n: 1.0 / G.number_of_nodes() for n in G.nodes()}

    # louvain 社区（子主题）
    try:
        communities = nx.community.louvain_communities(G, seed=42, weight="weight")
    except Exception as e:
        logger.warning("louvain 失败，单社区: %s", e)
        communities = [set(G.nodes())]
    comm_id = {n: i for i, comm in enumerate(communities) for n in comm}

    seminal_p80 = _percentile(pr.values(), 80)
    frontiers = []
    labels: dict[int, dict] = {}

    # 第一遍：算 seminal + frontier 原始值 + cluster + year
    for n in G.nodes():
        paper = G.nodes[n].get("paper")
        year = paper.year if paper else None
        seminal = pr.get(n, 0)
        if year:
            frontier = seminal * (0.5 ** ((current_year - year) / 5))
        else:
            frontier = 0.0
        frontiers.append(frontier)
        labels[n] = {
            "seminal": seminal,
            "frontier": frontier,
            "cluster": comm_id.get(n, 0),
            "year": year,
        }

    frontier_p80 = _percentile(frontiers, 80)

    # 第二遍：标 is_root / is_frontier
    for n in G.nodes():
        labels[n]["is_root"] = labels[n]["seminal"] >= seminal_p80 and seminal_p80 > 0
        labels[n]["is_frontier"] = labels[n]["frontier"] >= frontier_p80 and frontier_p80 > 0

    n_roots = sum(1 for v in labels.values() if v["is_root"])
    n_front = sum(1 for v in labels.values() if v["is_frontier"])
    logger.info(
        "标注: %d 节点, %d 根(奠基), %d 前沿, %d 社区",
        len(labels), n_roots, n_front, len(communities),
    )
    return labels
