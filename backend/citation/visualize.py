"""生成前端可渲染的图 JSON（缝合 Connected Papers 视觉编码）。

node size ∝ citation_count, color ∝ year, position ∝ spring_layout（力导向，相似拉近）。
"""
from __future__ import annotations

import logging

import networkx as nx

logger = logging.getLogger(__name__)


def to_vis_data(G: nx.Graph, labels: dict) -> dict:
    """生成 {nodes, edges} 供前端 d3-force / force-graph 渲染。"""
    if G.number_of_nodes() == 0:
        return {"nodes": [], "edges": []}

    try:
        pos = nx.spring_layout(G, weight="weight", seed=42, dim=2)
    except Exception:
        pos = {n: (0.0, 0.0) for n in G.nodes()}

    # 每个 cluster 取 pagerank(seminal) 最高的论文标题前缀作为主题标签
    cluster_best: dict[int, tuple[float, str]] = {}
    for n, lab in labels.items():
        cid = lab.get("cluster", 0)
        seminal = lab.get("seminal", 0)
        p = G.nodes[n].get("paper")
        title = (p.title[:40] if p else "")
        if title and (cid not in cluster_best or seminal > cluster_best[cid][0]):
            cluster_best[cid] = (seminal, title)

    nodes = []
    for n in G.nodes():
        p = G.nodes[n].get("paper")
        lab = labels.get(n, {})
        cluster = lab.get("cluster", 0)
        nodes.append(
            {
                "id": n,
                "title": (p.title[:80] if p else ""),
                "year": (p.year if p else None),
                "citation_count": (p.citation_count if p else 0),
                "size": max(1, (p.citation_count if p else 1)),  # ∝ 引用数，下限 1
                "color_year": (p.year if p else None),  # 前端按年份映射颜色
                "cluster": cluster,
                "cluster_label": cluster_best.get(cluster, (0, f"主题 {cluster}"))[1],
                "is_root": lab.get("is_root", False),
                "is_frontier": lab.get("is_frontier", False),
                "seminal": lab.get("seminal", 0),
                "frontier": lab.get("frontier", 0),
                "x": float(pos[n][0]),
                "y": float(pos[n][1]),
                "arxiv_id": (p.arxiv_id if p else None),
                "doi": (p.doi if p else None),
            }
        )

    edges = [
        {"source": u, "target": v, "weight": d.get("weight", 1)}
        for u, v, d in G.edges(data=True)
    ]
    logger.info("vis_data: %d nodes, %d edges", len(nodes), len(edges))
    return {"nodes": nodes, "edges": edges}


def summarize_for_synthesis(labels: dict, papers_by_id: dict) -> str:
    """生成三类标注摘要，注入 synthesizer 让综述按三类组织。"""
    roots = []
    frontiers = []
    clusters: dict[int, list[str]] = {}
    for n, lab in labels.items():
        p = papers_by_id.get(n)
        title = (p.title[:50] if p else f"paper#{n}")
        if lab.get("is_root"):
            roots.append(f"{title}({lab.get('year')})")
        if lab.get("is_frontier"):
            frontiers.append(f"{title}({lab.get('year')})")
        clusters.setdefault(lab.get("cluster", 0), []).append(title)

    lines = ["## 引用图谱分析（基于共参考文献相似度）"]
    if roots:
        lines.append(f"### 奠基性论文（高影响力根节点）\n{'; '.join(roots[:10])}")
    if frontiers:
        lines.append(f"### 最新前沿（高影响力且近期）\n{'; '.join(frontiers[:10])}")
    if clusters:
        for cid, members in sorted(clusters.items()):
            if len(members) > 1:
                lines.append(f"### 子主题簇 {cid}\n{'; '.join(members[:8])}")
    if not roots and not frontiers:
        lines.append("（图谱节点间共参考度低，无明显聚类）")
    return "\n\n".join(lines)
