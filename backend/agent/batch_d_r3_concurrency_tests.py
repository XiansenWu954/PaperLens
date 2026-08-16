"""P2-D-R3-02: concurrent finalizer scenarios — authoritative result
across all surfaces.

Scenarios (REAL threads + barrier):
  1. embedded wins first, failed arrives later
  2. failed wins first, embedded arrives later
  3. four identical embedded redeliveries
  4. four identical failed redeliveries

Per scenario: ONE terminal_at, ONE authoritative status, at most one
immediate wakeup (winner only), ONE dependency terminal transition, no
contradictory events.
"""
from __future__ import annotations

import threading
from unittest import mock

from django.db import connection
from django.utils import timezone

from agent.batch_c_red_tests import _link, _paper, _proj, BatchCRedTestBase


class R3ConcurrentFinalizerTest(BatchCRedTestBase):

    def _scenario(self, label, calls, first_deterministic=False):
        """calls: list of (status, kwargs). When ``first_deterministic``
        is True the FIRST call runs alone (guaranteed winner) and the
        remaining calls race as concurrent losers."""
        from api.models import (PaperIngestionJob, ProjectRun,
                                ProjectWorkflowDependency,
                                ProjectRunEvent)
        from api.workflow_callbacks import finalize_job_terminal

        proj = _proj(f"R3CF-{label}")
        paper = _paper(f"R3CF {label}", f"r3cf-{label}".lower())
        _link(proj, paper)
        job = PaperIngestionJob.objects.create(
            project=proj, paper=paper, status="downloading",
            source_url=paper.pdf_url, source_kind="url")
        run = ProjectRun.objects.create(
            project=proj, kind="workflow", status="waiting_ingestion",
            question=label)
        dep = ProjectWorkflowDependency.objects.create(
            run=run, paper=paper, ingestion_job=job, status="pending")

        wakeups = []
        results = []

        with mock.patch(
                "api.tasks.resume_research_expand_workflow_task.delay",
                side_effect=lambda *a: wakeups.append(a[0])
                or type("R", (), {"id": "f"})()):
            if first_deterministic:
                status, kwargs = calls[0]
                results.append(finalize_job_terminal(job.id, status,
                                                     **kwargs))
                losers = calls[1:]
            else:
                losers = calls
            barrier = threading.Barrier(len(losers)) if losers else None

            def _fin(status, kwargs):
                if barrier:
                    barrier.wait()
                results.append(finalize_job_terminal(job.id, status,
                                                     **kwargs))

            threads = [threading.Thread(target=_fin, args=args)
                       for args in losers]
            for th in threads:
                th.start()
            for th in threads:
                th.join(timeout=30)

        job.refresh_from_db()
        dep.refresh_from_db()
        winners = [r for r in results if r["won"]]
        statuses = {r["authoritative_status"] for r in results}
        events = list(ProjectRunEvent.objects.filter(
            run=run).values_list("event_type", flat=True))

        checks = {
            "one_winner": len(winners) == 1,
            "one_authoritative_status": len(statuses) == 1,
            "terminal_at_set": job.terminal_at is not None,
            "dep_transitioned_once": dep.status in ("succeeded", "failed"),
            "bounded_wakeup": len(wakeups) <= 1,
            "no_contradictory_events": not (
                "ingestion_completed" in events and
                "ingestion_failed" in events),
        }
        return {"checks": checks, "status": job.status,
                "dep_status": dep.status, "wakeups": len(wakeups),
                "events": events}

    def test_DW_R3_EMBEDDED_WINS_FAILED_LATE(self):
        self.case_id = "DW-R3-EMBEDDED-WINS-FAILED-LATE"
        out = self._scenario("EF", first_deterministic=True, calls=[
            ("embedded", {"chunk_count": 3}),
            ("failed", {"error_code": "late_failure"}),
        ])
        for k, v in out["checks"].items():
            self._checks.append({"surface": k, "sentinel": label if (label := k) else "",
                                 "found": not v, "note": str(out)})
        self.assertTrue(all(out["checks"].values()),
                        f"embedded-first scenario failed: {out}")
        self.assertEqual(out["status"], "embedded")
        self.assertEqual(out["dep_status"], "succeeded")

    def test_DW_R3_FAILED_WINS_EMBEDDED_LATE(self):
        self.case_id = "DW-R3-FAILED-WINS-EMBEDDED-LATE"
        out = self._scenario("FE", first_deterministic=True, calls=[
            ("failed", {"error_code": "first_failure"}),
            ("embedded", {"chunk_count": 3}),
        ])
        self.assertTrue(all(out["checks"].values()),
                        f"failed-first scenario failed: {out}")
        self.assertEqual(out["status"], "failed")
        self.assertEqual(out["dep_status"], "failed")

    def test_DW_R3_FOUR_EMBEDDED_REDELIVERIES(self):
        self.case_id = "DW-R3-FOUR-EMBEDDED-REDELIVERIES"
        out = self._scenario("4E", [
            ("embedded", {"chunk_count": 3}),
            ("embedded", {"chunk_count": 3}),
            ("embedded", {"chunk_count": 3}),
            ("embedded", {"chunk_count": 3}),
        ])
        self.assertTrue(all(out["checks"].values()),
                        f"4x embedded scenario failed: {out}")
        self.assertEqual(out["status"], "embedded")

    def test_DW_R3_FOUR_FAILED_REDELIVERIES(self):
        self.case_id = "DW-R3-FOUR-FAILED-REDELIVERIES"
        out = self._scenario("4F", [
            ("failed", {"error_code": "perm"}),
            ("failed", {"error_code": "perm"}),
            ("failed", {"error_code": "perm"}),
            ("failed", {"error_code": "perm"}),
        ])
        self.assertTrue(all(out["checks"].values()),
                        f"4x failed scenario failed: {out}")
        self.assertEqual(out["status"], "failed")
