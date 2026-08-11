"""Run the P0/P1 upgrade quality evaluation report.

聚合 6 项升级的盲区评测（BGE-M3 sparse / 引用语境 / Docling / BibTeX 回环 / verified 判定 / 图谱路径），
用真实 BGE-M3 + 真实 DeepSeek 验证升级效果，产出 JSON 报告。
"""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime
from pathlib import Path

from django.core.management.base import BaseCommand

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Evaluate P0/P1 upgrade quality with real BGE-M3 and DeepSeek models."

    def add_arguments(self, parser):
        parser.add_argument("--write-report", action="store_true")
        parser.add_argument("--skip-real-models", action="store_true", help="跳过需要真实 BGE-M3/DeepSeek 的耗时评测")
        parser.add_argument("--only", choices=["embedding", "relation", "pdf", "bibtex", "verified", "graph"], help="只跑某一项")

    def handle(self, *args, **options):
        started = time.perf_counter()
        skip_real = options["skip_real_models"]
        only = options["only"]
        sections: dict = {}
        passed_all = True

        def _run(key, fn, needs_real=True):
            nonlocal passed_all
            if only and only != key:
                return
            if needs_real and skip_real:
                sections[key] = {"skipped": True, "reason": "--skip-real-models"}
                return
            sec_started = time.perf_counter()
            try:
                result = fn()
                result["duration_ms"] = round((time.perf_counter() - sec_started) * 1000, 2)
                sections[key] = result
                if not result.get("passed", False) and not result.get("skipped"):
                    passed_all = False
            except Exception as exc:
                # §31.1/§32.4: eval artifacts never carry raw exception text.
                from eval.safe_error import exception_record

                logger.error(
                    "upgrade quality section failed",
                    extra={"event": "eval_upgrade_quality_section_failed",
                           "section": key, **exception_record(exc)},
                )
                sections[key] = {"passed": False,
                                 **exception_record(exc),
                                 "error_class": exc.__class__.__name__}
                passed_all = False

        self.stdout.write("Running upgrade quality evaluation...")
        _run("embedding", _eval_embedding)  # 真实 BGE-M3
        _run("relation", _eval_relation)  # 真实 DeepSeek
        _run("pdf", _eval_pdf)  # 真实 Docling
        _run("bibtex", _eval_bibtex, needs_real=False)  # 确定性
        _run("verified", _eval_verified, needs_real=False)  # 确定性
        _run("graph", _eval_graph, needs_real=False)  # 确定性

        result = {
            "version": "1.0",
            "generated_at": datetime.utcnow().isoformat() + "Z",
            "models": {"embedding": "BAAI/bge-m3 (real)", "llm": "deepseek-v4-flash (real)", "pdf": "docling (real)"},
            "sections": sections,
            "passed": passed_all,
            "duration_ms": round((time.perf_counter() - started) * 1000, 2),
        }
        self.stdout.write(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        if options["write_report"]:
            reports_dir = Path(__file__).resolve().parents[2] / "reports"
            reports_dir.mkdir(parents=True, exist_ok=True)
            target = reports_dir / f"upgrade_quality_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}.json"
            target.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
            self.stdout.write(self.style.SUCCESS(f"Wrote {target}"))
        if not passed_all:
            self.stdout.write(self.style.ERROR("Upgrade quality evaluation FAILED"))
            raise SystemExit(1)
        self.stdout.write(self.style.SUCCESS("Upgrade quality evaluation PASSED"))


def _eval_embedding():
    from eval.embedding_quality import run_embedding_ablation
    return run_embedding_ablation()


def _eval_relation():
    from eval.relation_quality import run_relation_quality
    return run_relation_quality()


def _eval_pdf():
    from eval.pdf_parse_quality import run_pdf_parse_quality
    return run_pdf_parse_quality()


def _eval_bibtex():
    from eval.upgrade_quality import run_bibtex_roundtrip
    return run_bibtex_roundtrip()


def _eval_verified():
    from eval.upgrade_quality import run_verified_judgment
    return run_verified_judgment()


def _eval_graph():
    from eval.upgrade_quality import run_graph_paths
    return run_graph_paths()
