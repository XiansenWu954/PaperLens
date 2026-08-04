"""Run the project Agent evaluation harness."""
from __future__ import annotations

from datetime import datetime

from django.core.management.base import BaseCommand

from api.demo import seed_demo_project
from eval.agent_harness import dumps_eval, run_project_agent_eval_sync


class Command(BaseCommand):
    help = "Evaluate project Agent tool policy, RAG grounding, and report drafting."

    def add_arguments(self, parser):
        parser.add_argument("--project-id", type=int, default=0)
        parser.add_argument("--include-network", action="store_true")

    def handle(self, *args, **options):
        project_id = options["project_id"]
        if not project_id:
            title = f"PaperLens harness eval {datetime.utcnow().strftime('%Y%m%d-%H%M%S')}"
            seeded = seed_demo_project(title, reuse=False, status="archived")
            project_id = seeded["project"]["id"]
            self.stdout.write(f"Seeded archived harness-eval project #{project_id}")
        result = run_project_agent_eval_sync(
            project_id,
            include_network=options["include_network"],
        )
        self.stdout.write(dumps_eval(result))
        if not result["passed"]:
            raise SystemExit(1)
