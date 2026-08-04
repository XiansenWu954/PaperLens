"""评测对比脚本：baseline vs PaperLens（同一评测集，同一 LLM）。

用法：python -m eval.run_eval [--limit N] [--skip-baseline]
诚实记录结果，不粉饰（吸取 AppPilot 教训）。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import time
from pathlib import Path

logger = logging.getLogger(__name__)

REPORTS_DIR = Path(__file__).parent / "reports"


def _setup_django():
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    import django
    django.setup()


async def eval_one(item, run_baseline: bool) -> dict:
    """对一题跑 baseline + paperlens，算指标。"""
    from eval.metrics import coverage, faithfulness, recall_at_k
    from eval.variants import baseline_research, paperlens_research

    result = {"id": item.id, "type": item.type, "question": item.question}

    # PaperLens
    try:
        pl = await paperlens_research(item.question)
        result["paperlens"] = {
            "recall_at_10": recall_at_k(pl["retrieved_titles"], item.gold_titles, k=10),
            "faithfulness": faithfulness(item.question, pl["report"]),
            "coverage": coverage(item.question, pl["report"], item.gold_topics),
            "n_sources": len(pl["sources"]),
            "report_len": len(pl["report"]),
        }
    except Exception as e:
        logger.exception("paperlens 失败 %s", item.id)
        result["paperlens"] = {"error": str(e)[:200]}

    # baseline
    if run_baseline:
        try:
            bl = await baseline_research(item.question)
            result["baseline"] = {
                "recall_at_10": recall_at_k(bl["retrieved_titles"], item.gold_titles, k=10),
                "faithfulness": faithfulness(item.question, bl["report"]),
                "coverage": coverage(item.question, bl["report"], item.gold_topics),
                "n_sources": len(bl["sources"]),
                "report_len": len(bl["report"]),
            }
        except Exception as e:
            logger.exception("baseline 失败 %s", item.id)
            result["baseline"] = {"error": str(e)[:200]}

    return result


def _avg(items, variant, metric):
    vals = [r[variant][metric] for r in items if isinstance(r.get(variant), dict) and metric in r[variant]]
    return sum(vals) / len(vals) if vals else 0.0


async def main_async(limit: int, run_baseline: bool) -> int:
    from eval.dataset import EVAL_ITEMS, type_distribution

    _setup_django()
    items = EVAL_ITEMS[:limit] if limit > 0 else EVAL_ITEMS
    print(f"=== 评测对比（{len(items)} 题，类型分布 {type_distribution()}）===")

    results = []
    for item in items:
        print(f"\n[{item.id}] {item.question}")
        t0 = time.time()
        r = await eval_one(item, run_baseline)
        results.append(r)
        elapsed = time.time() - t0
        pl = r.get("paperlens", {})
        bl = r.get("baseline", {})
        pl_str = f"PL recall={pl.get('recall_at_10','?')} faith={pl.get('faithfulness','?'):.2f} cov={pl.get('coverage','?'):.2f}" if "recall_at_10" in pl else f"PL error"
        bl_str = f"BL recall={bl.get('recall_at_10','?')} faith={bl.get('faithfulness','?'):.2f} cov={bl.get('coverage','?'):.2f}" if run_baseline and "recall_at_10" in bl else "BL skipped/error"
        print(f"  {pl_str} | {bl_str} ({elapsed:.0f}s)")

    # 汇总
    print("\n" + "=" * 60)
    print("汇总（均值）")
    print("=" * 60)
    pl_recall = _avg(results, "paperlens", "recall_at_10")
    pl_faith = _avg(results, "paperlens", "faithfulness")
    pl_cov = _avg(results, "paperlens", "coverage")
    print(f"PaperLens : Recall@10={pl_recall:.2f}  faithfulness={pl_faith:.2f}  coverage={pl_cov:.2f}")
    if run_baseline:
        bl_recall = _avg(results, "baseline", "recall_at_10")
        bl_faith = _avg(results, "baseline", "faithfulness")
        bl_cov = _avg(results, "baseline", "coverage")
        print(f"Baseline  : Recall@10={bl_recall:.2f}  faithfulness={bl_faith:.2f}  coverage={bl_cov:.2f}")
        print(f"Δ(PaperLens-Baseline): Recall={pl_recall-bl_recall:+.2f}  faith={pl_faith-bl_faith:+.2f}  cov={pl_cov-bl_cov:+.2f}")

    # 落盘
    REPORTS_DIR.mkdir(exist_ok=True)
    ts = time.strftime("%Y%m%d_%H%M%S")
    out = REPORTS_DIR / f"eval_{ts}.json"
    with open(out, "w", encoding="utf-8") as f:
        json.dump({"results": results, "summary": {
            "paperlens": {"recall": pl_recall, "faithfulness": pl_faith, "coverage": pl_cov},
            "baseline": {"recall": _avg(results, "baseline", "recall_at_10"),
                         "faithfulness": _avg(results, "baseline", "faithfulness"),
                         "coverage": _avg(results, "baseline", "coverage")} if run_baseline else None,
        }}, f, ensure_ascii=False, indent=2)
    print(f"\n报告已存: {out}")
    return 0


def main():
    logging.basicConfig(level=logging.WARNING, format="%(name)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="限制题数（0=全部）")
    ap.add_argument("--skip-baseline", action="store_true", help="跳过 baseline（只跑 paperlens）")
    args = ap.parse_args()
    sys.exit(asyncio.run(main_async(args.limit, not args.skip_baseline)))


if __name__ == "__main__":
    main()
