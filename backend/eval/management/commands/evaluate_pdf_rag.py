"""Run PDF ingestion and project RAG quality evaluation."""
from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from eval.pdf_rag import dumps_pdf_rag_eval, run_pdf_rag_eval


class Command(BaseCommand):
    help = "Evaluate local PDF ingestion, project RAG retrieval, and optional live Agent output."

    def add_arguments(self, parser):
        parser.add_argument("--include-live-agent", action="store_true")
        parser.add_argument("--use-critic", action="store_true")
        parser.add_argument("--production-embedding", action="store_true")
        parser.add_argument("--write-report", action="store_true")

    def handle(self, *args, **options):
        if options["include_live_agent"] and not os.environ.get("DEEPSEEK_API_KEY"):
            raise CommandError("DEEPSEEK_API_KEY is required for --include-live-agent.")
        if options["use_critic"] and not options["include_live_agent"]:
            raise CommandError("--use-critic requires --include-live-agent.")

        result = run_pdf_rag_eval(
            include_live_agent=options["include_live_agent"],
            use_production_embedding=options["production_embedding"],
            use_critic=options["use_critic"],
        )
        self.stdout.write(dumps_pdf_rag_eval(result))
        if options["write_report"]:
            reports_dir = Path(__file__).resolve().parents[2] / "reports"
            reports_dir.mkdir(parents=True, exist_ok=True)
            target = reports_dir / f"pdf_rag_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}.json"
            target.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
            self.stdout.write(f"Wrote {target}")
        if not result["passed"]:
            raise SystemExit(1)
