"""Phase 2 Batch B CX fix tests — P2-B-CX-01..05 focused suite."""
from __future__ import annotations

import json
import os
from datetime import timedelta
from unittest import mock

from django.core.management import call_command
from django.db import IntegrityError, connection, migrations
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase, Client
from django.utils import timezone

ARTIFACTS_DIR = os.environ.get("PAPERLENS_STAGE_B_ARTIFACTS_DIR", "")

def _wj(n, r):
    if ARTIFACTS_DIR:
        os.makedirs(ARTIFACTS_DIR, exist_ok=True)
        with open(os.path.join(ARTIFACTS_DIR, n), "w", encoding="utf-8") as f:
            json.dump(r, f, ensure_ascii=False, indent=2)


def _proj(title="CX Fix"):
    from api.models import ResearchProject
    return ResearchProject.objects.create(title=title, status="active")

def _paper(title="P", arxiv="cx-1"):
    from papers.models import Paper
    return Paper.objects.create(
        title=title, abstract="a", year=2024, arxiv_id=arxiv)

def _link(proj, paper, status="included"):
    from api.models import ProjectPaper
    return ProjectPaper.objects.create(
        project=proj, paper=paper, status=status)


# ════════════════════════════════════════════════════════════════════════
# P2-B-CX-01: lifecycle timestamps + MigrationExecutor
# ════════════════════════════════════════════════════════════════════════

class CX01TimestampsTest(TransactionTestCase):

    def test_new_fields_nullable_and_settable(self):
        from api.models import ProjectRun
        proj = _proj("CX01")
        run = ProjectRun.objects.create(
            project=proj, kind="workflow", status="done", question="q")
        self.assertIsNone(run.started_at)
        self.assertIsNone(run.waiting_at)
        self.assertIsNone(run.completed_at)
        now = timezone.now()
        run.started_at = now - timedelta(seconds=60)
        run.waiting_at = now - timedelta(seconds=30)
        run.completed_at = now
        run.save()
        run.refresh_from_db()
        self.assertIsNotNone(run.started_at)
        self.assertIsNotNone(run.waiting_at)
        self.assertIsNotNone(run.completed_at)

    def test_migration_forward_backward_executor(self):
        """MigrationExecutor: migrate to 0006 (pre-timestamps), insert a run,
        migrate forward to 0007, verify row intact, then reverse back."""
        app = "api"
        target_from = [("api", "0006_workflow_lifecycle_v1")]
        target_to = [("api", "0007_workflow_lifecycle_timestamps")]
        executor = MigrationExecutor(connection)
        executor.migrate(target_from)
        executor.loader.build_graph()

        apps_from = executor.loader.project_state(target_from).apps
        OldRun = apps_from.get_model("api", "ProjectRun")
        OldProject = apps_from.get_model("api", "ResearchProject")
        proj = OldProject.objects.create(title="MigExec", status="active")
        run = OldRun.objects.create(
            project=proj, kind="workflow", status="done", question="legacy")

        executor.loader.build_graph()
        executor.migrate(target_to)
        executor.loader.build_graph()

        apps_to = executor.loader.project_state(target_to).apps
        NewRun = apps_to.get_model("api", "ProjectRun")
        migrated = NewRun.objects.get(id=run.id)
        self.assertEqual(migrated.status, "done")
        self.assertIsNone(migrated.started_at)
        self.assertIsNone(migrated.waiting_at)
        self.assertIsNone(migrated.completed_at)

        # Reverse back
        executor.migrate(target_from)
        executor.loader.build_graph()
        # Row still exists with original status
        apps_back = executor.loader.project_state(target_from).apps
        BackRun = apps_back.get_model("api", "ProjectRun")
        back = BackRun.objects.get(id=run.id)
        self.assertEqual(back.status, "done")

        # Restore leaf (the CURRENT latest migration, not a hardcoded one,
        # so additive migrations keep this test suite-safe)
        executor.migrate(executor.loader.graph.leaf_nodes())


# ════════════════════════════════════════════════════════════════════════
# P2-B-CX-02: startup + readiness + fail-closed
# ════════════════════════════════════════════════════════════════════════

class CX02ReadinessTest(TransactionTestCase):

    def test_health_reports_both_fields(self):
        client = Client(HTTP_HOST="localhost")
        resp = client.get("/health/workflow")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertIn("durable_workflow_enabled", data)
        self.assertIn("workflow_checkpointer_ready", data)
        self.assertIn("durable_workflow_available", data)
        # No DSN/host/user/password leakage
        blob = json.dumps(data)
        for forbidden in ("postgres://", "@", "password", "user=", "host="):
            self.assertNotIn(forbidden, blob)

    def test_ready_only_on_postgresql_with_tables(self):
        """Readiness returns True only on PostgreSQL with checkpoint tables.
        The test DB may not have checkpoint tables (TransactionTestCase
        rebuilds schema from migrations which don't include checkpoint DDL).
        We verify the logic: mock the cursor to return a table row and
        confirm readiness returns True."""
        from config.health import workflow_checkpointer_ready
        if connection.vendor != "postgresql":
            self.skipTest("PostgreSQL required")

        class FakeCursor:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def execute(self, *a, **kw): pass
            def fetchone(self): return (1,)  # simulate tables exist
            def fetchall(self): return [(1,)]

        with mock.patch.object(
                connection, "cursor", return_value=FakeCursor()):
            self.assertTrue(workflow_checkpointer_ready())

    def test_fail_closed_when_disabled(self):
        """When flag is disabled, readiness returns False regardless of DB."""
        from config.health import workflow_checkpointer_ready
        with mock.patch(
                "django.conf.settings.PAPERLENS_DURABLE_WORKFLOW_ENABLED",
                False):
            self.assertFalse(workflow_checkpointer_ready())

    def test_fail_closed_sqlite(self):
        """On SQLite, readiness returns False."""
        from config.health import workflow_checkpointer_ready
        with mock.patch.object(connection, "vendor", "sqlite"):
            self.assertFalse(workflow_checkpointer_ready())

    def test_fail_closed_missing_tables(self):
        """When checkpoint tables don't exist, readiness returns False."""
        from config.health import workflow_checkpointer_ready
        if connection.vendor != "postgresql":
            self.skipTest("PostgreSQL required")
        original_execute = connection.cursor

        class FakeCursor:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def execute(self, *a, **kw): pass
            def fetchone(self): return None  # simulate no tables
            def fetchall(self): return []

        with mock.patch.object(
                connection, "cursor", return_value=FakeCursor()):
            self.assertFalse(workflow_checkpointer_ready())

    def test_setup_uses_safe_conninfo(self):
        """setup command source uses psycopg.connect kwargs, not DSN concat."""
        import inspect
        from config.management.commands import setup_langgraph_checkpoints
        src = inspect.getsource(setup_langgraph_checkpoints)
        self.assertNotIn("f\"host=", src, "must not use f-string DSN concat")
        self.assertNotIn("conn_string", src,
                         "must not build a conn_string variable")
        self.assertIn("psycopg.connect(", src, "must use kwargs connect")


# ════════════════════════════════════════════════════════════════════════
# P2-B-CX-03: serializer additive fields via real HTTP
# ════════════════════════════════════════════════════════════════════════

class CX03SerializerTest(TransactionTestCase):

    def test_get_run_response_has_additive_fields(self):
        from api.models import (ProjectRun, ProjectWorkflowDependency,
                                ReportVersion)
        proj = _proj("CX03")
        paper = _paper("CX03 P", "cx03-p")
        _link(proj, paper)
        run = ProjectRun.objects.create(
            project=proj, kind="workflow", status="waiting_ingestion",
            question="q", workflow_phase="await_ingestion",
            resume_count=1,
            started_at=timezone.now() - timedelta(seconds=120),
            waiting_at=timezone.now() - timedelta(seconds=60),
            completed_at=None)
        ProjectWorkflowDependency.objects.create(
            run=run, paper=paper, status="ready")
        ReportVersion.objects.create(
            project=proj, title="R1", content="c", source="langgraph",
            source_run=run)

        client = Client(HTTP_HOST="localhost")
        resp = client.get(f"/api/projects/{proj.id}/runs")
        self.assertEqual(resp.status_code, 200)
        blob = json.dumps(resp.json(), default=str)
        for field in ("workflow_phase", "resume_count", "started_at",
                      "waiting_at", "last_ingestion_terminal_at",
                      "first_rag_at", "completed_at",
                      "dependency_summary", "report_id"):
            self.assertIn(field, blob, f"GET runs must expose {field}")

    def test_post_run_response_has_additive_fields(self):
        from api.models import ProjectRun
        proj = _proj("CX03 POST")
        client = Client(HTTP_HOST="localhost")
        with mock.patch(
                "api.tasks.run_research_expand_workflow_task") as task_mock:
            task_mock.delay.return_value = type("R", (), {"id": "fake"})()
            resp = client.post(
                f"/api/projects/{proj.id}/workflows/research-expand",
                {"question": "test q"}, format="json")
        self.assertEqual(resp.status_code, 201)
        data = resp.json()
        for field in ("workflow_phase", "resume_count", "started_at",
                      "waiting_at", "dependency_summary", "report_id"):
            self.assertIn(field, data,
                          f"POST workflow must expose {field}")

    def test_serializer_excludes_private_fields(self):
        from api.models import ProjectRun
        from api.serializers import ProjectRunSerializer
        proj = _proj("CX03 Priv")
        run = ProjectRun.objects.create(
            project=proj, kind="workflow", status="running",
            owner_token="secret-tok", question="q",
            lease_expires_at=timezone.now() + timedelta(seconds=300),
            draft_output="secret draft")
        data = ProjectRunSerializer(run).data
        blob = json.dumps(data, default=str)
        for forbidden in ("owner_token", "lease_expires_at", "draft_output",
                          "secret-tok", "secret draft"):
            self.assertNotIn(forbidden, blob,
                             f"serializer leaked {forbidden!r}")

    def test_dependency_summary_counts_only(self):
        from api.models import (ProjectRun, ProjectWorkflowDependency)
        from api.serializers import ProjectRunSerializer
        proj = _proj("CX03 Sum")
        run = ProjectRun.objects.create(
            project=proj, kind="workflow", status="pending")
        for i, st in enumerate(("ready", "pending", "failed")):
            p = _paper(f"Sum P{i}", f"sum-{i}")
            _link(proj, p)
            ProjectWorkflowDependency.objects.create(
                run=run, paper=p, status=st)
        data = ProjectRunSerializer(run).data
        ds = data["dependency_summary"]
        self.assertEqual(ds["ready"], 1)
        self.assertEqual(ds["pending"], 1)
        self.assertEqual(ds["failed"], 1)
        self.assertEqual(ds["succeeded"], 0)
        self.assertEqual(ds["unavailable"], 0)
        self.assertEqual(ds["total"], 3)
        # No paper title/URL/error body in summary
        ds_blob = json.dumps(ds)
        for forbidden in ("Sum P", "cdn.example", "error", "content"):
            self.assertNotIn(forbidden, ds_blob)


# ════════════════════════════════════════════════════════════════════════
# P2-B-CX-04: workflow data boundary
# ════════════════════════════════════════════════════════════════════════

class CX04BoundaryTest(TransactionTestCase):

    def test_positive_own_included_paper(self):
        from api.models import ProjectRun, ProjectWorkflowDependency
        from api.workflow_data import create_workflow_dependency
        proj = _proj("CX04 Pos")
        paper = _paper("Own Inc", "cx04-oi")
        _link(proj, paper)
        run = ProjectRun.objects.create(
            project=proj, kind="workflow", status="pending")
        dep = create_workflow_dependency(run=run, paper=paper, status="ready")
        self.assertEqual(dep.status, "ready")

    def test_negative_foreign_paper(self):
        from api.models import ProjectRun
        from api.workflow_data import (create_workflow_dependency,
                                       WorkflowDataError)
        proj = _proj("CX04 Own")
        proj2 = _proj("CX04 Foreign")
        foreign_paper = _paper("Foreign", "cx04-f")
        _link(proj2, foreign_paper)  # linked to different project
        run = ProjectRun.objects.create(
            project=proj, kind="workflow", status="pending")
        with self.assertRaises(WorkflowDataError) as ctx:
            create_workflow_dependency(run=run, paper=foreign_paper)
        self.assertEqual(ctx.exception.code, "foreign_paper")

    def test_negative_excluded_paper(self):
        from api.models import ProjectRun
        from api.workflow_data import (create_workflow_dependency,
                                       WorkflowDataError)
        proj = _proj("CX04 Exc")
        paper = _paper("Exc", "cx04-e")
        _link(proj, paper, status="excluded")
        run = ProjectRun.objects.create(
            project=proj, kind="workflow", status="pending")
        with self.assertRaises(WorkflowDataError) as ctx:
            create_workflow_dependency(run=run, paper=paper)
        self.assertEqual(ctx.exception.code, "excluded_paper")

    def test_negative_unlinked_paper(self):
        from api.models import ProjectRun
        from api.workflow_data import (create_workflow_dependency,
                                       WorkflowDataError)
        proj = _proj("CX04 Unl")
        paper = _paper("Unlinked", "cx04-u")
        # No ProjectPaper created — paper is global, not in this project
        run = ProjectRun.objects.create(
            project=proj, kind="workflow", status="pending")
        with self.assertRaises(WorkflowDataError) as ctx:
            create_workflow_dependency(run=run, paper=paper)
        self.assertEqual(ctx.exception.code, "foreign_paper")

    def test_negative_job_project_mismatch(self):
        from api.models import (PaperIngestionJob, ProjectRun)
        from api.workflow_data import (create_workflow_dependency,
                                       WorkflowDataError)
        proj = _proj("CX04 JM")
        proj2 = _proj("CX04 Other")
        paper = _paper("JM P", "cx04-jm")
        _link(proj, paper)
        _link(proj2, paper)
        job = PaperIngestionJob.objects.create(
            project=proj2, paper=paper, status="pending")  # wrong project
        run = ProjectRun.objects.create(
            project=proj, kind="workflow", status="pending")
        with self.assertRaises(WorkflowDataError) as ctx:
            create_workflow_dependency(
                run=run, paper=paper, ingestion_job=job)
        self.assertEqual(ctx.exception.code, "job_project_mismatch")

    def test_negative_job_paper_mismatch(self):
        from api.models import (PaperIngestionJob, ProjectRun)
        from api.workflow_data import (create_workflow_dependency,
                                       WorkflowDataError)
        proj = _proj("CX04 JP")
        paper1 = _paper("JP 1", "cx04-jp1")
        paper2 = _paper("JP 2", "cx04-jp2")
        _link(proj, paper1)
        _link(proj, paper2)
        job = PaperIngestionJob.objects.create(
            project=proj, paper=paper2, status="pending")  # different paper
        run = ProjectRun.objects.create(
            project=proj, kind="workflow", status="pending")
        with self.assertRaises(WorkflowDataError) as ctx:
            create_workflow_dependency(
                run=run, paper=paper1, ingestion_job=job)
        self.assertEqual(ctx.exception.code, "job_paper_mismatch")

    def test_positive_same_project_job(self):
        from api.models import (PaperIngestionJob, ProjectRun)
        from api.workflow_data import create_workflow_dependency
        proj = _proj("CX04 OK")
        paper = _paper("OK P", "cx04-ok")
        _link(proj, paper)
        job = PaperIngestionJob.objects.create(
            project=proj, paper=paper, status="pending")
        run = ProjectRun.objects.create(
            project=proj, kind="workflow", status="pending")
        dep = create_workflow_dependency(
            run=run, paper=paper, ingestion_job=job, status="pending")
        self.assertIsNotNone(dep.id)

    def test_report_positive_and_duplicate(self):
        from api.models import ProjectRun
        from api.workflow_data import (create_workflow_report,
                                       WorkflowDataError)
        proj = _proj("CX04 Rep")
        run = ProjectRun.objects.create(
            project=proj, kind="workflow", status="done")
        r1 = create_workflow_report(
            run=run, title="R1", content="c")
        self.assertEqual(r1.source_run, run)
        with self.assertRaises(WorkflowDataError) as ctx:
            create_workflow_report(run=run, title="R2", content="c2")
        self.assertEqual(ctx.exception.code, "duplicate_report_for_run")
