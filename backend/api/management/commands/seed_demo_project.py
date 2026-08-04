"""Create a PaperLens V2 demo project."""
from __future__ import annotations

from django.core.management.base import BaseCommand

from api.demo import seed_demo_project


class Command(BaseCommand):
    help = "Seed a demo PaperLens project with papers and a report version."

    def add_arguments(self, parser):
        parser.add_argument("--title", default="", help="Optional demo project title.")
        parser.add_argument("--fresh", action="store_true", help="Create a new demo project instead of reusing the default one.")

    def handle(self, *args, **options):
        result = seed_demo_project(options["title"] or None, reuse=not options["fresh"])
        project = result["project"]
        self.stdout.write(
            self.style.SUCCESS(
                f"Seeded demo project #{project['id']} with {result['count']} papers: {project['title']}"
            )
        )
