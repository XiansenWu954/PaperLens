"""Agent 运行入口 + 端到端验证。

用法：python -m agent.runner "<研究问题>"
需 Django 环境（datasources/papers ORM）。本模块自调 django.setup()。
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


async def run_agent(question: str, config=None) -> dict:
    """运行完整 agent graph，返回最终状态。"""
    from .config import DEFAULT_CONFIG
    from .graph import build_graph

    cfg = config or DEFAULT_CONFIG
    graph = build_graph(cfg)
    # LangGraph 支持 async coroutine 节点，用 ainvoke
    result = await graph.ainvoke({"question": question})
    return result


def main() -> int:
    _setup_django()
    question = sys.argv[1] if len(sys.argv) > 1 else "Mamba 状态空间模型的最新进展"

    print("=" * 60)
    print("PaperLens Agent 端到端运行")
    print(f"问题: {question}")
    print("=" * 60)

    result = asyncio.run(run_agent(question))

    print("\n" + "=" * 60)
    print("最终综述")
    print("=" * 60)
    print(result.get("final_report", "(空)"))
    print("\n" + "=" * 60)
    print(f"来源论文数: {len(result.get('sources', []))}")
    print(f"研究笔记数: {len(result.get('notes', []))}")
    for s in result.get("sources", [])[:5]:
        print(f"  - {s.get('title','')[:60]} ({s.get('year')}) 引用={s.get('citation_count',0)}")
    print("=" * 60)

    # 验证标准
    sources = result.get("sources", [])
    report = result.get("final_report", "")
    ok = len(sources) >= 1 and len(report) > 100
    print(f"\n验证: sources≥1={len(sources)>=1}, 综述>100字={len(report)>100} -> {'通过 ✓' if ok else '失败 ✗'}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
