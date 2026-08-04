"""Run deterministic hybrid RAG quality evaluation."""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from django.core.management.base import BaseCommand

from eval.rag_quality import dumps_rag_quality_eval, run_rag_quality_eval


class Command(BaseCommand):
    help = "Evaluate PaperLens hybrid RAG with 30+ deterministic cases."

    def add_arguments(self, parser):
        parser.add_argument("--write-report", action="store_true")

    def handle(self, *args, **options):
        result = run_rag_quality_eval(write_report=options["write_report"])
        self.stdout.write(json.dumps(result, ensure_ascii=True, indent=2, default=str))
        if options["write_report"]:
            reports_dir = Path(__file__).resolve().parents[2] / "reports"
            reports_dir.mkdir(parents=True, exist_ok=True)
            target = reports_dir / f"rag_quality_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}.json"
            target.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
            self.stdout.write(f"Wrote {target}")
        if not result["passed"]:
            raise SystemExit(1)
