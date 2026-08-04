"""引用相似图构建（复刻 Connected Papers 的 bibliographic coupling 算法）。

Connected Papers 不是引用树，而是 co-citation + bibliographic coupling 相似图。
networkx 3.6 移除了内置 bibliographic_coupling，手写实现（共享参考文献数）。
数据源：papers.Paper.referenced_works（地基验证 OpenAlex 返回完整列表）。
"""
from __future__ import annotations

import logging

import networkx as nx

logger = logging.getLogger(__name__)


def norm_oid(oid: str) -> str:
    """归一化 openalex id：https://openalex.org/W123 → W123。

    也兼容已经是短 id 的情况。
    """
    if not oid:
        return ""
    if "/" in oid:
        return oid.rsplit("/", 1)[-1]
    return oid


def build_similarity_graph(seed_papers: list, max_nodes: int = 200) -> nx.Graph:
    """从种子论文构建 bibliographic coupling 相似图。

    相似度 = 两篇种子共享的参考文献数（集合交集大小）。
    seed_papers: papers.Paper 实例列表（须有 referenced_works 字段）。
    """
    G = nx.Graph()
    for p in seed_papers:
        G.add_node(p.id, paper=p)

    # 取每篇种子的 referenced_works 集合（归一化）
    refs = {}
    for p in seed_papers:
        raw = getattr(p, "referenced_works", None) or []
        refs[p.id] = {norm_oid(r) for r in raw if r}

    # bibliographic coupling：共享参考数 = 边权重
    ids = list(refs.keys())
    edge_count = 0
    for i, a in enumerate(ids):
        if not refs[a]:
            continue
        for b in ids[i + 1 :]:
            if not refs[b]:
                continue
            w = len(refs[a] & refs[b])
            if w > 0:
                G.add_edge(a, b, weight=w)
                edge_count += 1

    logger.info(
        "相似图: %d 节点, %d 边 (bibliographic coupling)", G.number_of_nodes(), edge_count
    )

    # 候选池封顶：按度数保留 top max_nodes
    if G.number_of_nodes() > max_nodes:
        keep = sorted(G.degree(weight="weight"), key=lambda x: -x[1])[:max_nodes]
        G = G.subgraph([n for n, _ in keep]).copy()
        logger.info("封顶后: %d 节点", G.number_of_nodes())

    return G
