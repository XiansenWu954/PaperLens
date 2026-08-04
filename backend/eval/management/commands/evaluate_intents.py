"""Run the deterministic project intent classifier golden matrix."""
from __future__ import annotations

from django.core.management.base import BaseCommand

from eval.intent_eval import dumps_intent_eval, evaluate_intent_classifier


class Command(BaseCommand):
    help = "Evaluate project Agent intent classification against the golden matrix."

    def add_arguments(self, parser):
        parser.add_argument("--project-id", type=int, default=1)

    def handle(self, *args, **options):
        result = evaluate_intent_classifier(project_id=options["project_id"])
        self.stdout.write(dumps_intent_eval(result))
        if not result["passed"]:
            raise SystemExit(1)
