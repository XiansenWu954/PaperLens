"""Run interactive end-to-end Agent evaluation against a live backend.

需要后端正在运行（python manage.py runserver）+ DEEPSEEK_API_KEY。
"""
from __future__ import annotations

import json

from django.core.management.base import BaseCommand

from eval.interactive_eval import run_interactive_eval, write_report


class Command(BaseCommand):
    help = "Interactive end-to-end Agent evaluation: 5-intent real Q&A + DeepSeek judge."

    def add_arguments(self, parser):
        parser.add_argument("--project-id", type=int, default=None, help="复用已有项目；不传则 seed demo")
        parser.add_argument("--write-report", action="store_true")

    def handle(self, *args, **options):
        result = run_interactive_eval(project_id=options["project_id"])
        self.stdout.write(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        if options["write_report"]:
            target = write_report(result)
            self.stdout.write(self.style.SUCCESS(f"Wrote {target}"))
        if result["passed"]:
            self.stdout.write(self.style.SUCCESS(
                f"Interactive eval PASSED (avg score {result['average_judge_score']}, "
                f"{result['passed_rounds']}/{result['total_rounds']} rounds)"
            ))
        else:
            self.stdout.write(self.style.ERROR(
                f"Interactive eval FAILED (avg score {result['average_judge_score']})"
            ))
            raise SystemExit(1)
