"""Run the multi-turn conversation evaluation (product & agent manual §5.5/§6.6).

Deterministic by default (``use_llm=False``): exercises the multi-turn driver
and session reuse offline. To validate LLM-level reference resolution /
constraint retention, run against a live backend with a DeepSeek key using
``evaluate_interactive`` (see docs/internal/gate-runbook.md).
"""
from __future__ import annotations

import json
import sys
from datetime import datetime

from django.core.management.base import BaseCommand

from api.demo import seed_demo_project
from eval.conversation_eval import run_conversation_eval_sync


class Command(BaseCommand):
    help = "Run multi-turn conversation eval (deterministic router by default)."

    def add_arguments(self, parser):
        parser.add_argument("--project-id", type=int, default=0)
        parser.add_argument("--use-llm", action="store_true", help="Use the live ReAct loop (needs DeepSeek key).")

    def handle(self, *args, **options):
        # Windows default codepage (CP950) raises UnicodeEncodeError on Chinese
        # JSON with ensure_ascii=False; force UTF-8 on stdout (manual §3.2/PRE-07).
        try:
            sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
        except (AttributeError, OSError):
            pass

        project_id = options["project_id"]
        if not project_id:
            title = f"conversation eval {datetime.utcnow().strftime('%Y%m%d-%H%M%S')}"
            seeded = seed_demo_project(title, reuse=False, status="archived")
            project_id = seeded["project"]["id"]
            self.stdout.write(f"Seeded archived conversation-eval project #{project_id}")

        use_llm = options["use_llm"]
        if not use_llm:
            # Offline isolation (manual §3.2): make sure we never accidentally hit
            # external sources / real embeddings in the deterministic path.
            self.stdout.write("Offline mode: deterministic router, no live LLM / external sources.")
        result = run_conversation_eval_sync(project_id, use_llm=use_llm)

        self.stdout.write(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        self.stdout.write(
            self.style.SUCCESS(
                f"conversation eval: {result['passed_cases']}/{result['case_count']} cases, "
                f"{result['total_turns']} turns, passed={result['passed']}"
            )
        )
        if not result["passed"]:
            raise SystemExit(1)
