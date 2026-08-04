"""RAG 管线端到端 smoke 验证。

验证完整链路：下载 ArXiv PDF → 解析切片 → 嵌入 → 检索某问题 → RCS 评分 → 带 pqac 证据。
用法：python -m rag.smoke
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
    from papers.models import Paper, upsert_paper
    from rag.ingest import ingest_paper
    from rag.retrieval import retrieve_evidence

    print("=" * 60)
    print("PaperLens RAG 管线 smoke 验证")
    print("=" * 60)

    # 1. 准备一篇有 PDF 的论文（用 ArXiv 检索 + upsert）
    from datasources.arxiv import ArxivSearcher

    print("\n--- 1. 检索一篇 ArXiv 论文 ---")
    results = await ArxivSearcher().search("attention is all you need", max_results=1)
    if not results:
        print("✗ 无结果")
        return 1
    p_data = results[0]
    print(f"论文: {p_data['title'][:60]} arxiv={p_data.get('arxiv_id')} pdf={bool(p_data.get('pdf_url'))}")

    paper = await sync_to_async(upsert_paper)(p_data)
    print(f"入库 paper id={paper.id}")

    # 2. ingest（下载+解析+切片+嵌入）
    print("\n--- 2. PDF ingest（下载+解析+切片+嵌入）---")
    n = await ingest_paper(paper)
    print(f"切片数: {n}")
    if n == 0:
        print("✗ ingest 失败（可能扫描版或下载失败）")
        return 1

    # 3. 检索证据
    print("\n--- 3. 检索证据（召回→RCS→过滤）---")
    question = "attention 机制的核心思想是什么？"
    evidences = await retrieve_evidence(question, paper_ids=[paper.id])
    print(f"问题: {question}")
    print(f"证据数: {len(evidences)}")
    for e in evidences[:5]:
        print(f"  [{e.citation_key}] score={e.score}: {e.summary[:80]}")

    ok = len(evidences) >= 1
    print("\n" + "=" * 60)
    print(f"{'RAG 管线通过 ✓' if ok else 'RAG 管线失败 ✗'}")
    print(f"  PDF下载解析切片嵌入: {'✓' if n > 0 else '✗'}")
    print(f"  RCS检索返回带pqac证据: {'✓' if ok else '✗'}")
    print("=" * 60)
    return 0 if ok else 1


if __name__ == "__main__":
    _setup_django()
    sys.exit(asyncio.run(main()))
