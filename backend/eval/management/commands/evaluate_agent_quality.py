"""Run the aggregated PaperLens Agent quality report."""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from django.core.management.base import BaseCommand

from api.demo import seed_demo_project
from eval.agent_quality import dumps_quality_eval, run_agent_quality_eval


class Command(BaseCommand):
    help = "Evaluate resume-grade PaperLens Agent quality metrics."

    def add_arguments(self, parser):
        parser.add_argument("--project-id", type=int, default=0)
        parser.add_argument("--include-network", action="store_true")
        parser.add_argument("--write-report", action="store_true")

    def handle(self, *args, **options):
        project_id = options["project_id"]
        if not project_id:
            title = f"PaperLens quality eval {datetime.utcnow().strftime('%Y%m%d-%H%M%S')}"
            seeded = seed_demo_project(title, reuse=False, status="archived")
            project_id = seeded["project"]["id"]
            self.stdout.write(f"Seeded archived quality-eval project #{project_id}")

        result = run_agent_quality_eval(
            project_id,
            include_network=options["include_network"],
        )
        self.stdout.write(dumps_quality_eval(result))
        if options["write_report"]:
            reports_dir = Path(__file__).resolve().parents[2] / "reports"
            reports_dir.mkdir(parents=True, exist_ok=True)
            target = reports_dir / f"agent_quality_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}.json"
            target.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
            self.stdout.write(f"Wrote {target}")
        if not result["passed"]:
            raise SystemExit(1)
