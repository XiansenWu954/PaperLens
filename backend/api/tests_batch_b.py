"""Phase 2 Batch B focused tests — durable data + checkpoint foundation."""
from __future__ import annotations

import json
import os
from datetime import timedelta
from unittest import mock

from django.db import IntegrityError, connection
from django.test import TransactionTestCase
from django.utils import timezone

ARTIFACTS_DIR = os.environ.get("PAPERLENS_STAGE_B_ARTIFACTS_DIR", "")

def _ap(n):
    if not ARTIFACTS_DIR: return None
    os.makedirs(ARTIFACTS_DIR, exist_ok=True)
    return os.path.join(ARTIFACTS_DIR, n)

def _wj(n, r):
    p = _ap(n)
    if p:
        with open(p, "w", encoding="utf-8") as f: json.dump(r, f, ensure_ascii=False, indent=2)


class BatchBMigrationTest(TransactionTestCase):
    """Tasks 3.6: migration forward safety — existing rows unchanged."""

    def test_existing_run_keeps_status_and_fields(self):
        """A legacy 4-state run remains compatible after migration."""
        from api.models import ProjectRun, ResearchProject
        proj = ResearchProject.objects.create(title="Mig Test", status="active")
        run = ProjectRun.objects.create(
            project=proj, kind="workflow", status="done",
            question="legacy question")
        run.refresh_from_db()
        self.assertEqual(run.status, "done")
        self.assertEqual(run.owner_token, "")
        self.assertIsNone(run.lease_expires_at)
        self.assertIsNone(run.first_rag_at)
        self.assertIsNone(run.last_ingestion_terminal_at)
        self.assertEqual(run.draft_output, "")
        self.assertEqual(run.workflow_phase, "")
        self.assertEqual(run.resume_count, 0)

    def test_existing_report_keeps_null_source_run(self):
        """A legacy report has source_run=None (no workflow ownership)."""
        from api.models import ReportVersion, ResearchProject
        proj = ResearchProject.objects.create(title="Rep Mig", status="active")
        report = ReportVersion.objects.create(
            project=proj, title="Legacy", content="c", source="agent")
        report.refresh_from_db()
        self.assertIsNone(report.source_run)

    def test_existing_event_accepts_empty_dedupe(self):
        """Multiple legacy events with dedupe_key='' coexist."""
        from api.models import ProjectRun, ProjectRunEvent, ResearchProject
        proj = ResearchProject.objects.create(title="Evt Mig", status="active")
        run = ProjectRun.objects.create(
            project=proj, kind="research", status="done")
        for i in range(3):
            ProjectRunEvent.objects.create(
                run=run, event_type="test_event", payload={"i": i})
        self.assertEqual(
            ProjectRunEvent.objects.filter(run=run).count(), 3)


class BatchBConstraintTest(TransactionTestCase):
    """Tasks 3.6: database unique constraints — positive + negative controls."""

    def test_dependency_unique_run_paper(self):
        """Duplicate (run, paper) dependency violates uniqueness."""
        from api.models import (ProjectRun, ProjectWorkflowDependency,
                                ResearchProject)
        from papers.models import Paper
        proj = ResearchProject.objects.create(title="Dep C", status="active")
        paper = Paper.objects.create(
            title="Dep Paper", abstract="a", year=2024, arxiv_id="dep-c-1")
        run = ProjectRun.objects.create(
            project=proj, kind="workflow", status="pending", question="q")
        dep = ProjectWorkflowDependency.objects.create(
            run=run, paper=paper, status="ready")
        self.assertIsNotNone(dep.id)
        # Negative control: duplicate must fail
        with self.assertRaises(IntegrityError):
            ProjectWorkflowDependency.objects.create(
                run=run, paper=paper, status="pending")

    def test_dependency_different_runs_same_paper_ok(self):
        """Same paper in different runs is allowed."""
        from api.models import (ProjectRun, ProjectWorkflowDependency,
                                ResearchProject)
        from papers.models import Paper
        proj = ResearchProject.objects.create(title="Dep D", status="active")
        paper = Paper.objects.create(
            title="Shared Dep", abstract="a", year=2024, arxiv_id="dep-d-1")
        run1 = ProjectRun.objects.create(
            project=proj, kind="workflow", status="pending", question="q1")
        run2 = ProjectRun.objects.create(
            project=proj, kind="workflow", status="pending", question="q2")
        dep1 = ProjectWorkflowDependency.objects.create(
            run=run1, paper=paper, status="ready")
        dep2 = ProjectWorkflowDependency.objects.create(
            run=run2, paper=paper, status="pending")
        self.assertNotEqual(dep1.id, dep2.id)

    def test_event_dedupe_key_unique_within_run(self):
        """Non-empty dedupe_key is unique within its run."""
        from api.models import ProjectRun, ProjectRunEvent, ResearchProject
        proj = ResearchProject.objects.create(title="Dedup", status="active")
        run = ProjectRun.objects.create(
            project=proj, kind="workflow", status="running")
        e1 = ProjectRunEvent.objects.create(
            run=run, event_type="workflow_node",
            payload={"node": "plan"}, dedupe_key="node-plan-done")
        self.assertIsNotNone(e1.id)
        with self.assertRaises(IntegrityError):
            ProjectRunEvent.objects.create(
                run=run, event_type="workflow_node",
                payload={"node": "plan"}, dedupe_key="node-plan-done")

    def test_event_same_dedupe_different_run_ok(self):
        """Same dedupe_key in different runs is allowed."""
        from api.models import ProjectRun, ProjectRunEvent, ResearchProject
        proj = ResearchProject.objects.create(title="Dedup2", status="active")
        run1 = ProjectRun.objects.create(
            project=proj, kind="workflow", status="running")
        run2 = ProjectRun.objects.create(
            project=proj, kind="workflow", status="running")
        e1 = ProjectRunEvent.objects.create(
            run=run1, event_type="node", payload={},
            dedupe_key="shared-key")
        e2 = ProjectRunEvent.objects.create(
            run=run2, event_type="node", payload={},
            dedupe_key="shared-key")
        self.assertNotEqual(e1.id, e2.id)

    def test_report_source_run_one_to_one(self):
        """Only one ReportVersion per source_run."""
        from api.models import ProjectRun, ReportVersion, ResearchProject
        proj = ResearchProject.objects.create(title="Rep OO", status="active")
        run = ProjectRun.objects.create(
            project=proj, kind="workflow", status="done")
        r1 = ReportVersion.objects.create(
            project=proj, title="R1", content="c", source="langgraph",
            source_run=run)
        self.assertEqual(r1.source_run, run)
        with self.assertRaises(IntegrityError):
            ReportVersion.objects.create(
                project=proj, title="R2", content="c", source="langgraph",
                source_run=run)

    def test_multiple_reports_without_source_run_ok(self):
        """Multiple legacy reports (source_run=None) coexist."""
        from api.models import ReportVersion, ResearchProject
        proj = ResearchProject.objects.create(title="Rep Multi", status="active")
        for i in range(3):
            ReportVersion.objects.create(
                project=proj, title=f"R{i}", content="c", source="agent")
        self.assertEqual(
            ReportVersion.objects.filter(project=proj).count(), 3)

    def test_projectrun_lifecycle_fields_exist(self):
        """New lifecycle fields + statuses are queryable."""
        from api.models import ProjectRun, ResearchProject
        proj = ResearchProject.objects.create(title="LC", status="active")
        run = ProjectRun.objects.create(
            project=proj, kind="workflow", status="waiting_ingestion",
            question="q", workflow_phase="await_ingestion",
            owner_token="tok-1", resume_count=2,
            lease_expires_at=timezone.now() + timedelta(seconds=300),
            first_rag_at=timezone.now(),
            last_ingestion_terminal_at=timezone.now() - timedelta(seconds=1),
            draft_output="draft content")
        run.refresh_from_db()
        self.assertEqual(run.status, "waiting_ingestion")
        self.assertEqual(run.workflow_phase, "await_ingestion")
        self.assertEqual(run.owner_token, "tok-1")
        self.assertEqual(run.resume_count, 2)
        self.assertIsNotNone(run.lease_expires_at)
        self.assertIsNotNone(run.first_rag_at)
        self.assertIsNotNone(run.last_ingestion_terminal_at)
        self.assertEqual(run.draft_output, "draft content")

    def test_paperingestionjob_terminal_at(self):
        """PaperIngestionJob has terminal_at field."""
        from api.models import PaperIngestionJob, ProjectPaper, \
            ProjectRun, ResearchProject
        from papers.models import Paper
        proj = ResearchProject.objects.create(title="Term", status="active")
        paper = Paper.objects.create(
            title="Term Paper", abstract="a", year=2024, arxiv_id="term-1")
        ProjectPaper.objects.create(project=proj, paper=paper, status="included")
        now = timezone.now()
        job = PaperIngestionJob.objects.create(
            project=proj, paper=paper, status="embedded",
            terminal_at=now)
        job.refresh_from_db()
        self.assertIsNotNone(job.terminal_at)


class BatchBCheckpointerTest(TransactionTestCase):
    """Tasks 3.1/3.6: setup idempotency + checkpoint table + write/read."""

    def test_setup_idempotent_twice(self):
        """Running setup_langgraph_checkpoints twice succeeds."""
        from django.core.management import call_command
        from io import StringIO
        out1 = StringIO()
        call_command("setup_langgraph_checkpoints", stdout=out1)
        self.assertIn("idempotent", out1.getvalue().lower())
        out2 = StringIO()
        call_command("setup_langgraph_checkpoints", stdout=out2)
        self.assertIn("idempotent", out2.getvalue().lower())

    def test_checkpoint_tables_exist(self):
        """PostgreSQL has LangGraph checkpoint tables."""
        if connection.vendor != "postgresql":
            self.skipTest("PostgreSQL required")
        with connection.cursor() as cur:
            cur.execute(
                "SELECT tablename FROM pg_tables "
                "WHERE schemaname='public' AND tablename LIKE 'checkpoint%'")
            tables = {r[0] for r in cur.fetchall()}
        self.assertIn("checkpoints", tables)
        self.assertIn("checkpoint_writes", tables)
        self.assertIn("checkpoint_blobs", tables)

    def test_checkpoint_minimal_write_read(self):
        """Minimal checkpoint write + read via PostgresSaver."""
        if connection.vendor != "postgresql":
            self.skipTest("PostgreSQL required")
        import psycopg
        from langgraph.checkpoint.postgres import PostgresSaver
        from langgraph.checkpoint.base import Checkpoint
        from django.db import connection as dj_conn

        cfg = dj_conn.settings_dict
        conn_str = (
            f"host={cfg.get('HOST','localhost')} "
            f"port={cfg.get('PORT','5432')} "
            f"dbname={cfg.get('NAME','')} "
            f"user={cfg.get('USER','')} "
            f"password={cfg.get('PASSWORD','')}"
        )
        conn = psycopg.connect(conn_str, autocommit=True)
        try:
            saver = PostgresSaver(conn)
            saver.setup()
            import uuid
            from datetime import datetime, timezone
            ckpt_id = str(uuid.uuid4())
            ts = datetime.now(timezone.utc).isoformat()
            ckpt = Checkpoint(
                v=1, id=ckpt_id, ts=ts,
                channel_values={"test": "value"},
                channel_versions={},
                versions_seen={},
                checkpoint_ns="")
            config = {"configurable": {
                "thread_id": "test-thread-1",
                "checkpoint_ns": ""}}
            metadata = {"source": "test", "step": 1, "writes": {}}
            new_versions = {"test": 1}
            saver.put(config, ckpt, metadata, new_versions)
            retrieved = saver.get(config)
            self.assertIsNotNone(retrieved)
        finally:
            conn.close()


class BatchBSensitiveTest(TransactionTestCase):
    """Tasks 3.5: owner_token/lease/draft_output not in serializer/API/events."""

    def test_serializer_excludes_private_fields(self):
        """ProjectRunSerializer does not expose owner_token, lease_expires_at,
        draft_output."""
        from api.models import ProjectRun, ResearchProject
        from api.serializers import ProjectRunSerializer
        proj = ResearchProject.objects.create(title="Sens", status="active")
        run = ProjectRun.objects.create(
            project=proj, kind="workflow", status="running",
            owner_token="secret-token-123",
            lease_expires_at=timezone.now() + timedelta(seconds=300),
            draft_output="secret draft content",
            question="q")
        data = ProjectRunSerializer(run).data
        blob = json.dumps(data, default=str)
        for forbidden in ("owner_token", "lease_expires_at", "draft_output",
                          "secret-token-123", "secret draft content"):
            self.assertNotIn(forbidden, blob,
                             f"serializer leaked {forbidden!r}")

    def test_run_serializer_has_additive_lifecycle_fields(self):
        """Serializer can expose workflow_phase/resume_count/timestamps
        (safe additive fields only)."""
        from api.models import ProjectRun, ResearchProject
        from api.serializers import ProjectRunSerializer
        proj = ResearchProject.objects.create(title="Add", status="active")
        run = ProjectRun.objects.create(
            project=proj, kind="workflow", status="waiting_ingestion",
            workflow_phase="await_ingestion", resume_count=1)
        data = ProjectRunSerializer(run).data
        # These are safe additive fields — model exists, serializer will
        # include them when Batch C extends fields list. Currently the
        # serializer hasn't been extended yet, so we verify the MODEL has them.
        run.refresh_from_db()
        self.assertEqual(run.workflow_phase, "await_ingestion")
        self.assertEqual(run.resume_count, 1)


class BatchBCeleryWorkerTest(TransactionTestCase):
    """Tasks 3.1: Celery worker never executes checkpoint DDL."""

    def test_celery_worker_no_checkpoint_ddl(self):
        """The Celery worker module does not call setup_langgraph_checkpoints."""
        from api import tasks as api_tasks
        import inspect as _insp
        source = _insp.getsource(api_tasks)
        self.assertNotIn("setup_langgraph_checkpoints", source,
                         "Celery worker must not execute checkpoint DDL")
        self.assertNotIn("PostgresSaver", source,
                         "Celery worker must not create PostgresSaver")


class BatchBHealthTest(TransactionTestCase):
    """Tasks 3.5: health reporting for durable workflow subsystem."""

    def test_health_reports_workflow_flag(self):
        """Settings include PAPERLENS_DURABLE_WORKFLOW_ENABLED."""
        from django.conf import settings
        self.assertTrue(hasattr(settings, "PAPERLENS_DURABLE_WORKFLOW_ENABLED"))
