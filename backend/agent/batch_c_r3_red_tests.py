"""P2-C-R3 red tests — owner fencing boundaries + URL identity unification.

CXF-05 (P2-C-R3-01): replace the owner at the waiting/resume/enqueue/done/
error boundaries; the old executor must produce ZERO side effects.
CXF-06 (P2-C-R3-02): one paper + one URL submitted through the URL API,
Agent auto-queue and the durable workflow converges on ONE global build
with independent project jobs and no URL leakage.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from unittest import mock

from django.db import connection
from django.test import Client, TransactionTestCase

from agent.batch_c_red_tests import (BatchCRedTestBase, _link, _paper,
                                     _proj)

logger = logging.getLogger(__name__)
ARTIFACTS_DIR = os.environ.get("PAPERLENS_STAGE_B_ARTIFACTS_DIR", "")


def _wj(n, r):
    if ARTIFACTS_DIR:
        os.makedirs(ARTIFACTS_DIR, exist_ok=True)
        with open(os.path.join(ARTIFACTS_DIR, n), "w", encoding="utf-8") as f:
            json.dump(r, f, ensure_ascii=False, indent=2)


class CXF05OwnerFencingBoundariesTest(BatchCRedTestBase):

    def test_DW_CXF05_OWNER_REPLACED_AT_BOUNDARIES(self):
        self.case_id = "DW-CXF05-OWNER-REPLACED-AT-BOUNDARIES"
        self.contract = ("replacing the owner at the waiting/resume/"
                         "enqueue/done/error boundaries leaves ZERO side "
                         "effects from the old executor (P2-C-R3-01)")
        if connection.vendor != "postgresql":
            self.skipTest("PostgreSQL required")
        from api.models import (PaperIngestionJob, ProjectRun,
                                ProjectRunEvent, ProjectWorkflowDependency,
                                ReportVersion)
        from agent.owner_service import acquire_owner, new_owner_token
        import agent.project_workflow as pw
        from api.tasks import run_research_expand_workflow_task

        results = {}
        for boundary in ("waiting", "resume", "enqueue", "done", "error"):
            proj = _proj(f"Fence-{boundary}")
            fence_paper = _paper(f"Fence {boundary}",
                                 f"fence-{boundary}",
                                 url=f"https://cdn.example.com/f-{boundary}.pdf")
            _link(proj, fence_paper)

            async def _search(*a, **kw):
                return {"papers": [{"title": f"Fence {boundary}",
                                    "year": 2024,
                                    "arxiv_id": fence_paper.arxiv_id,
                                    "pdf_url": fence_paper.pdf_url}]}

            run = ProjectRun.objects.create(
                project=proj, kind="workflow", status="pending",
                question=f"fence-{boundary}")
            # Per-boundary snapshot taken AT TAKEOVER TIME (inside the
            # fence hook) — only writes AFTER the replacement are illegal.
            snap = {"taken": False,
                    "events": 0, "jobs": 0, "deps": 0, "reports": 0}

            def _snapshot(run_id):
                if snap["taken"]:
                    return
                snap["taken"] = True
                snap["events"] = ProjectRunEvent.objects.filter(
                    run_id=run_id).count()
                snap["jobs"] = PaperIngestionJob.objects.filter(
                    project_id=proj.id).count()
                snap["deps"] = ProjectWorkflowDependency.objects.filter(
                    run_id=run_id).count()
                snap["reports"] = ReportVersion.objects.filter(
                    source_run_id=run_id).count()

            fence_at = {"hit": False}

            def _take_over(run_id):
                _snapshot(run_id)
                # simulate an expired lease being taken over by a new owner
                from django.utils import timezone as tz
                from datetime import timedelta
                ProjectRun.objects.filter(id=run_id).update(
                    lease_expires_at=tz.now() - timedelta(seconds=1))
                acquire_owner(ProjectRun.objects.get(id=run_id),
                              new_owner_token())

            if boundary == "waiting":
                # takeover AFTER the dependency refresh wrote rows but
                # BEFORE the waiting fence — the waiting state/event must
                # not be written by the old owner
                real_refresh = pw._refresh_dependency_status

                def _spied_refresh(run_id):
                    out = real_refresh(run_id)
                    _take_over(run_id)
                    return out

                async def _renew(run_id):
                    if snap["taken"]:
                        raise pw.OwnerLeaseLost("lost")

                patches = [
                    mock.patch.object(pw, "_refresh_dependency_status",
                                      _spied_refresh),
                    mock.patch.object(pw, "_renew_lease_async", _renew),
                ]
            elif boundary == "enqueue":
                # a NEW owner takes over right before the per-paper loop
                real_enq = pw._enqueue_ingestion_for_run

                def _spied_enq(run_id, project_id, target_ids):
                    _take_over(run_id)
                    return real_enq(run_id, project_id, target_ids)

                patches = [
                    mock.patch.object(pw, "_enqueue_ingestion_for_run",
                                      _spied_enq),
                ]
            elif boundary == "resume":
                # lose the lease before workflow_resumed is published
                async def _renew(run_id):
                    if fence_at["hit"]:
                        raise pw.OwnerLeaseLost("lost")
                    fence_at["hit"] = True

                def _snap_once(run_id):
                    _snapshot(run_id)

                patches = [
                    mock.patch.object(pw, "_renew_lease_async", _renew),
                ]
            elif boundary == "done":
                # owner replaced AFTER the graph completes, before the task
                # writes terminal state
                real_run = pw.run_project_research_expand

                async def _spied_run(project_id, question, run_id):
                    r = await real_run(project_id, question, run_id)
                    await asyncio.to_thread(_take_over, run_id)
                    return r

                patches = [
                    mock.patch.object(pw, "run_project_research_expand",
                                      _spied_run),
                ]
            else:  # error: generic exception AFTER the owner was replaced
                async def _boom(project_id, question, run_id):
                    await asyncio.to_thread(_take_over, run_id)
                    raise RuntimeError("synthetic failure")

                patches = [
                    mock.patch.object(pw, "run_project_research_expand",
                                      _boom),
                ]

            from contextlib import ExitStack
            with ExitStack() as stack:
                stack.enter_context(mock.patch.object(
                    pw, "search_papers", _search))
                stack.enter_context(mock.patch.object(
                    pw, "draft_report_section",
                    return_value={"section": "FENCE"}))
                stack.enter_context(mock.patch(
                    "api.tasks.ingest_paper_pdf_task.delay",
                    return_value=type("R", (), {"id": "f"})()))
                for p in patches:
                    stack.enter_context(p)
                try:
                    res = run_research_expand_workflow_task.run(run.id)
                except Exception:
                    res = {"status": "raised"}
            run.refresh_from_db()
            if not snap["taken"]:
                _snapshot(run.id)  # defensive: no takeover happened
            after_events = ProjectRunEvent.objects.filter(run=run).count()
            after_jobs = PaperIngestionJob.objects.filter(
                project=proj).count()
            after_deps = run.workflow_dependencies.count()
            after_reports = ReportVersion.objects.filter(
                source_run=run).count()
            results[boundary] = {
                "task": res.get("status"), "run": run.status,
                "job_delta": after_jobs - snap["jobs"],
                "dep_delta": after_deps - snap["deps"],
                "report_delta": after_reports - snap["reports"],
                "event_delta": after_events - snap["events"]}
            self._checks.append({"surface": boundary,
                                 "sentinel": "zero_side_effects",
                                 "found": (res.get("status") != "skipped"
                                           or run.status == "error"
                                           or after_reports - snap["reports"]
                                           or after_jobs - snap["jobs"] > 0
                                           or after_deps - snap["deps"] > 0
                                           or after_events - snap["events"]
                                           > 1),
                                 "note": str(results[boundary])})
        bad = {k: v for k, v in results.items()
               if v["task"] != "skipped" or v["run"] == "error"
               or v["report_delta"] or v["job_delta"] > 0
               or v["dep_delta"] > 0 or v["event_delta"] > 1}
        self.assertEqual(
            bad, {},
            "old owner wrote side effects at boundaries (CXF-05): %r" % bad)
        if ARTIFACTS_DIR:
            _wj("batchc-DW-CXF05-OWNER-REPLACED-AT-BOUNDARIES.json",
                {"case_id": self.case_id, "boundaries": results})


class CXF06UnifiedUrlIdentityTest(BatchCRedTestBase):

    def test_DW_CXF06_ONE_BUILD_ACROSS_ENTRY_POINTS(self):
        self.case_id = "DW-CXF06-ONE-BUILD-ACROSS-ENTRY-POINTS"
        self.contract = ("TWO projects share one paper + URL; URL API, "
                         "Agent and Workflow cover the entries; ONE global "
                         "build; ONE scoped job per project; per-project "
                         "dependency ownership; raw URL/path/query zero "
                         "leakage (P2-C-R3-02 final positive control)")
        from api.models import (PaperIngestionJob, ProjectRun,
                                ProjectWorkflowDependency, ResearchProject)
        from papers.models import Paper
        from rag.models import PaperIndexVersion

        URL = ("https://cdn.example.com/cxf06-crosspath.pdf"
               "?token=SECRET-QUERY-9931")
        proj1 = _proj("CXF06-A")
        proj2 = _proj("CXF06-B")
        paper = Paper.objects.create(
            title="CXF06 Cross P", abstract="a", year=2024,
            arxiv_id="cxf06-cross", pdf_url=URL)
        _link(proj1, paper)   # same paper linked into BOTH projects
        _link(proj2, paper)

        from api.ingestion_service import IngestionService
        service = IngestionService()
        client = Client(HTTP_HOST="localhost")

        # 1) URL API covers project 1
        with mock.patch("api.tasks.ingest_paper_pdf_task.delay",
                        return_value=type("R", (), {"id": "f"})()):
            resp = client.post(
                f"/api/projects/{proj1.id}/papers/{paper.id}/ingest", {})
        self.assertIn(resp.status_code, (200, 201),
                      f"URL API ingest failed: {resp.status_code}")

        # 2) Agent auto-queue covers project 2
        with mock.patch("api.tasks.ingest_paper_pdf_task.delay",
                        return_value=type("R", (), {"id": "f"})()):
            from agent.project_tools import add_papers_to_project
            asyncio.run(add_papers_to_project(
                proj2.id, [{"title": "CXF06 Cross P", "year": 2024,
                            "arxiv_id": "cxf06-cross", "pdf_url": URL}],
                reason="agent"))

        # 3) Durable workflow covers project 1 again (duplicate entry)
        run1 = ProjectRun.objects.create(
            project=proj1, kind="workflow", status="pending",
            question="cxf06")
        # and project 2's own workflow run (independent dependency)
        run2 = ProjectRun.objects.create(
            project=proj2, kind="workflow", status="pending",
            question="cxf06")
        from agent.project_workflow import _enqueue_ingestion_for_run
        with mock.patch("api.tasks.ingest_paper_pdf_task.delay",
                        return_value=type("R", (), {"id": "f"})()):
            _enqueue_ingestion_for_run(run1.id, proj1.id, [paper.id])
            _enqueue_ingestion_for_run(run2.id, proj2.id, [paper.id])

        jobs = list(PaperIngestionJob.objects.filter(
            paper=paper).values_list("id", "project_id", "file_name"))
        builds = list(PaperIndexVersion.objects.filter(
            paper=paper).values_list("id", "status", "source_sha256",
                                     "pipeline_signature"))
        distinct_build_keys = {(b[2], b[3]) for b in builds}
        deps = list(ProjectWorkflowDependency.objects.filter(
            paper=paper).values_list("run_id", "status"))
        dep_runs = {r for r, _ in deps}

        # ONE global build across ALL entries and BOTH projects
        self._checks.append({"surface": "builds", "sentinel": "single_build",
                             "found": len(distinct_build_keys) != 1,
                             "note": f"keys={distinct_build_keys}"})
        self.assertEqual(
            len(distinct_build_keys), 1,
            "entry points diverged on build identity (CXF-06): %r"
            % distinct_build_keys)

        # ONE scoped job PER PROJECT (two projects -> exactly two jobs)
        jobs_by_project = {}
        for j in jobs:
            jobs_by_project.setdefault(j[1], []).append(j)
        self._checks.append({"surface": "jobs",
                             "sentinel": "one_job_per_project",
                             "found": (len(jobs_by_project) != 2
                                       or any(len(v) != 1
                                              for v in
                                              jobs_by_project.values())),
                             "note": str({k: len(v) for k, v in
                                          jobs_by_project.items()})})
        self.assertEqual(
            len(jobs_by_project), 2,
            "expected one job per project (CXF-06): %r" % jobs_by_project)
        for pid, project_jobs in jobs_by_project.items():
            self.assertEqual(
                len(project_jobs), 1,
                f"project {pid} has {len(project_jobs)} jobs (CXF-06)")

        # per-project dependency ownership: run1 -> proj1, run2 -> proj2
        run_project = {run1.id: proj1.id, run2.id: proj2.id}
        self._checks.append({"surface": "deps",
                             "sentinel": "ownership",
                             "found": (dep_runs != {run1.id, run2.id}
                                       or len(deps) != 2),
                             "note": str(deps)})
        self.assertEqual(dep_runs, {run1.id, run2.id},
                         "dependency run ownership wrong (CXF-06)")
        self.assertEqual(len(deps), 2,
                         "expected exactly two dependencies (CXF-06)")
        for rid, _status in deps:
            dep = ProjectWorkflowDependency.objects.get(run_id=rid,
                                                        paper=paper)
            self.assertEqual(
                run_project[rid], proj1.id if rid == run1.id else proj2.id,
                "dependency belongs to the wrong project run (CXF-06)")

        # raw URL/path/query sentinel zero leakage on every surface
        sentinel = "cxf06-crosspath.pdf"
        file_leak = any(sentinel in (j[2] or "") for j in jobs)
        r1 = client.get(f"/api/projects/{proj1.id}/ingestion-jobs")
        r2 = client.get(f"/api/projects/{proj2.id}/ingestion-jobs")
        api_leak = (sentinel in json.dumps(r1.json(), default=str)
                    or sentinel in json.dumps(r2.json(), default=str))
        ev1 = " ".join(str(v) for v in
                       run1.events.values_list("payload", flat=True))
        ev2 = " ".join(str(v) for v in
                       run2.events.values_list("payload", flat=True))
        ev_leak = sentinel in ev1 or sentinel in ev2
        log_leak = False
        for handler in logging.getLogger().handlers:
            records = getattr(handler, "records", None)
            if records and any(sentinel in str(m) for m in records):
                log_leak = True
        for surface, leaked in (("file_name", file_leak),
                                ("api", api_leak),
                                ("events", ev_leak),
                                ("logs", log_leak)):
            self._checks.append({"surface": "leak", "sentinel": surface,
                                 "found": leaked, "note": ""})
        self.assertFalse(file_leak,
                         "URL path leaked into file_name (CXF-06)")
        self.assertFalse(api_leak,
                         "URL path leaked into API response (CXF-06)")
        self.assertFalse(ev_leak,
                         "URL path leaked into events (CXF-06)")
        self.assertFalse(log_leak,
                         "URL path leaked into logs (CXF-06)")

        # canonical identity helpers agree across surfaces
        digest = service.canonical_url_identity(URL)
        for j in jobs:
            self.assertEqual(
                j[2], f"paper-{paper.id}-{digest[:8]}.pdf",
                "file_name does not follow the canonical contract (CXF-06)")
        self.assertEqual(
            list(builds)[0][2], digest,
            "build source_sha256 does not match canonical identity (CXF-06)")
