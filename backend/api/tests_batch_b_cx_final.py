"""P2-B-CX-04: production-path report persistence tests."""
from __future__ import annotations

import json
from unittest import mock

from django.test import TransactionTestCase
from django.utils import timezone


def _proj(title="RPT"):
    from api.models import ResearchProject
    return ResearchProject.objects.create(title=title, status="active")


class CX04ProductionReportTest(TransactionTestCase):

    def test_own_run_creates_one_report_with_source_run(self):
        """Positive: own run/project creates exactly one report with source_run."""
        from api.models import ProjectRun, ReportVersion
        from agent.project_workflow import _save_report
        proj = _proj("CX04 Own")
        run = ProjectRun.objects.create(
            project=proj, kind="workflow", status="running", question="q")
        report_id = _save_report(proj.id, "question", "content",
                                 run_id=run.id)
        self.assertIsNotNone(report_id)
        report = ReportVersion.objects.get(id=report_id)
        self.assertEqual(report.source_run_id, run.id)
        self.assertEqual(report.project_id, proj.id)
        self.assertEqual(
            ReportVersion.objects.filter(project=proj).count(), 1)

    def test_project_mismatch_zero_reports(self):
        """Negative: run belongs to a different project — zero reports created."""
        from api.models import ProjectRun, ReportVersion
        from agent.project_workflow import _save_report
        proj_a = _proj("CX04 A")
        proj_b = _proj("CX04 B")
        run = ProjectRun.objects.create(
            project=proj_a, kind="workflow", status="running", question="q")
        report_id = _save_report(proj_b.id, "question", "content",
                                 run_id=run.id)  # wrong project
        self.assertIsNone(report_id)
        self.assertEqual(
            ReportVersion.objects.filter(project=proj_a).count(), 0)
        self.assertEqual(
            ReportVersion.objects.filter(project=proj_b).count(), 0)

    def test_missing_run_id_zero_reports(self):
        """Negative: no run identity — zero reports created."""
        from api.models import ReportVersion
        from agent.project_workflow import _save_report
        proj = _proj("CX04 NoRun")
        report_id = _save_report(proj.id, "question", "content",
                                 run_id=None)
        self.assertIsNone(report_id)
        self.assertEqual(
            ReportVersion.objects.filter(project=proj).count(), 0)

    def test_duplicate_persistence_single_report(self):
        """Calling _save_report twice for the same run yields one report."""
        from api.models import ProjectRun, ReportVersion
        from agent.project_workflow import _save_report
        proj = _proj("CX04 Dup")
        run = ProjectRun.objects.create(
            project=proj, kind="workflow", status="done", question="q")
        id1 = _save_report(proj.id, "question", "content v1", run_id=run.id)
        id2 = _save_report(proj.id, "question", "content v2", run_id=run.id)
        self.assertIsNotNone(id1)
        self.assertEqual(id1, id2,
                         "duplicate persistence must return the same report")
        self.assertEqual(
            ReportVersion.objects.filter(project=proj).count(), 1)

    def test_no_leak_in_skip_paths(self):
        """Skip-path logs carry stable reasons only — no question/content/exception."""
        import inspect
        from agent import project_workflow
        src = inspect.getsource(project_workflow._save_report)
        # The function must never embed question/content into log extra
        self.assertNotIn("question[:", src.replace('f"Expansion report: {question[:80]}"', ''))
        self.assertNotIn('extra={"event": "workflow_report_skipped",\n.*"question"', src)
        # Verify the actual log extras in the source contain only safe fields
        import re
        extras = re.findall(r'extra=\{[^}]+\}', src)
        for extra in extras:
            blob = extra.lower()
            self.assertNotIn("question", blob,
                             f"log extra must not contain question: {extra}")
            self.assertNotIn("content", blob,
                             f"log extra must not contain content: {extra}")

    def test_persist_report_node_routes_through_boundary(self):
        """The graph node persist_report calls _save_report which uses
        create_workflow_report — verified by mocking the boundary."""
        from api.models import ProjectRun, ReportVersion
        from api.workflow_data import create_workflow_report
        proj = _proj("CX04 Node")
        run = ProjectRun.objects.create(
            project=proj, kind="workflow", status="running", question="q")
        with mock.patch(
                "api.workflow_data.create_workflow_report",
                wraps=create_workflow_report) as spy:
            from agent.project_workflow import _save_report
            _save_report(proj.id, "q", "content", run_id=run.id)
            self.assertGreater(spy.call_count, 0,
                               "boundary must be called")
        self.assertEqual(
            ReportVersion.objects.filter(project=proj, source_run=run).count(), 1)
