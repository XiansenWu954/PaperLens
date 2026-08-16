"""Phase 2 Batch B — idempotent setup for LangGraph PostgreSQL checkpoints."""
from django.core.management.base import BaseCommand
from django.db import connection


class Command(BaseCommand):
    help = "Create LangGraph checkpoint tables in PostgreSQL (idempotent)."

    def handle(self, *args, **options):
        if connection.vendor != "postgresql":
            self.stdout.write(self.style.WARNING(
                "Skipped: checkpoint setup requires PostgreSQL."))
            return

        from langgraph.checkpoint.postgres import PostgresSaver
        import psycopg

        # Safe conninfo: pass key/value params to psycopg.connect — psycopg
        # handles escaping internally. NEVER manually concatenate a DSN
        # string (passwords may contain spaces/quotes/special characters).
        db_settings = connection.settings_dict
        sync_connection = psycopg.connect(
            host=db_settings.get("HOST") or "localhost",
            port=int(db_settings.get("PORT") or 5432),
            dbname=db_settings.get("NAME") or "",
            user=db_settings.get("USER") or "",
            password=db_settings.get("PASSWORD") or "",
            autocommit=True,  # CREATE INDEX CONCURRENTLY needs no-transaction
        )
        try:
            PostgresSaver(sync_connection).setup()
            self.stdout.write(self.style.SUCCESS(
                "LangGraph checkpoint tables created (idempotent)."))
        finally:
            sync_connection.close()
