"""引用图谱端到端 smoke 验证（★护城河）。

检索一批 CS 论文 → 建相似图（bibliographic coupling）→ 三类标注 → 输出 vis_data。
用法：python -m citation.smoke
"""
from __future__ import annotations

import asyncio
import os
import sys


def _setup_django() -> None:
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    import django
    django.setup()


async def main() -> int:
    from asgiref.sync import sync_to_async
    from datasources.openalex import OpenAlexSearcher
    from papers.models import upsert_paper

    from .analyze import label_nodes
    from .graph_build import build_similarity_graph
    from .visualize import summarize_for_synthesis, to_vis_data

    print("=" * 60)
    print("PaperLens 引用图谱 smoke 验证（护城河）")
    print("=" * 60)

    # 1. 检索一批 Mamba 论文（取 referenced_works 丰富的）
    print("\n--- 1. 检索种子论文 ---")
    results = await OpenAlexSearcher().search("Mamba state space model", max_results=8)
    print(f"检索到 {len(results)} 篇")
    papers = []
    for r in results:
        p = await sync_to_async(upsert_paper)(r)
        papers.append(p)
    # 过滤有 referenced_works 的
    papers = [p for p in papers if p.referenced_works]
    print(f"有 referenced_works 的种子: {len(papers)}")
    if len(papers) < 2:
        print("✗ 种子不足")
        return 1

    # 2. 建相似图
    print("\n--- 2. 构建 bibliographic coupling 相似图 ---")
    G = await sync_to_async(build_similarity_graph)(papers)
    print(f"节点: {G.number_of_nodes()}, 边: {G.number_of_edges()}")
    if G.number_of_edges() == 0:
        print("✗ 无共参考边（种子间参考文献无重叠）")
        return 1

    # 3. 三类标注
    print("\n--- 3. 三类标注（奠基性/前沿/子主题）---")
    labels = await sync_to_async(label_nodes)(G)
    roots = [n for n, l in labels.items() if l.get("is_root")]
    frontiers = [n for n, l in labels.items() if l.get("is_frontier")]
    clusters = set(l.get("cluster") for l in labels.values())
    print(f"奠基性根节点: {len(roots)}")
    print(f"最新前沿: {len(frontiers)}")
    print(f"子主题簇: {len(clusters)}")

    # 4. vis_data
    print("\n--- 4. 生成可视化数据 ---")
    vis_data = await sync_to_async(to_vis_data)(G, labels)
    print(f"vis nodes: {len(vis_data['nodes'])}, edges: {len(vis_data['edges'])}")
    for node in vis_data["nodes"][:3]:
        print(f"  - {node['title'][:40]} year={node['year']} size={node['size']} "
              f"root={node['is_root']} frontier={node['is_frontier']} cluster={node['cluster']}")

    # 5. 综述注入摘要
    print("\n--- 5. 综述注入摘要 ---")
    papers_by_id = {p.id: p for p in papers}
    summary = await sync_to_async(summarize_for_synthesis)(labels, papers_by_id)
    print(summary[:400])

    ok = G.number_of_edges() > 0 and len(vis_data["nodes"]) > 0
    print("\n" + "=" * 60)
    print(f"{'引用图谱验证通过 ✓' if ok else '失败 ✗'}")
    print(f"  bibliographic coupling 建图: {'✓' if G.number_of_edges()>0 else '✗'}")
    print(f"  三类标注: {'✓' if labels else '✗'}")
    print(f"  vis_data: {'✓' if vis_data['nodes'] else '✗'}")
    print("=" * 60)
    return 0 if ok else 1


if __name__ == "__main__":
    _setup_django()
    sys.exit(asyncio.run(main()))
