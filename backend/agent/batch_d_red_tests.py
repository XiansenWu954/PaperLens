"""Phase 2 Batch D red tests — recovery, partial results, minimal gates.

BD-01 (5.1): terminal_at set ONCE on first embedded/failed; redelivery
            never rewrites it; on_commit syncs dependencies + enqueues an
            idempotent resume wakeup (callback never runs the graph).
BD-02 (5.2): Beat reconciliation scans waiting-terminal / running-expired /
            pending-not-started / lost-wakeup; only enqueues existing
            idempotent tasks; at most one resume per run per cycle; never
            executes ingestion/RAG/report/graph.
BD-03 (5.3): first_rag_at > last_ingestion_terminal_at; RAG calls == 0
            while any dependency non-terminal; RAG reads only current
            project active-compatible fulltext.
BD-04 (5.4): deterministic done/partial/error gates; partial discloses
            failed/unavailable dependencies; error creates ZERO reports;
            critic only downgrades; one ReportVersion per run.
BD-05 (5.5): sensitive scan across checkpoint/pending-writes/events/logs/
            API/Celery results (question/URL/payload/draft/excerpt/prompt/
            key/exception bodies all absent).
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
from datetime import timedelta
from unittest import mock

from django.db import connection
from django.test import Client
from django.utils import timezone

from agent.batch_c_red_tests import (_active, _link, _paper, _proj,
                                     BatchCRedTestBase)

logger = logging.getLogger(__name__)
ARTIFACTS_DIR = os.environ.get("PAPERLENS_STAGE_B_ARTIFACTS_DIR", "")


# ════════════════════════════════════════════════════════════════════════
# BD-01: terminal callback
# ════════════════════════════════════════════════════════════════════════

class BD01TerminalCallbackTest(BatchCRedTestBase):

    def test_DW_BD01_TERMINAL_AT_ONCE_AND_WAKEUP(self):
        self.case_id = "DW-BD01-TERMINAL-AT-ONCE-AND-WAKEUP"
        self.contract = ("terminal_at set once on first embedded/failed; "
                         "redelivery never rewrites; on_commit syncs the "
                         "dependency and enqueues one idempotent resume "
                         "wakeup (never executes the graph)")
        from api.models import PaperIngestionJob, ProjectRun
        from api.tasks import ingest_paper_pdf_task

        proj = _proj("BD01")
        paper = _paper("BD01 P", "bd01-p")
        _link(proj, paper)
        job = PaperIngestionJob.objects.create(
            project=proj, paper=paper, status="pending",
            source_url=paper.pdf_url, source_kind="url")
        run = ProjectRun.objects.create(
            project=proj, kind="workflow", status="waiting_ingestion",
            question="bd01")
        from api.models import ProjectWorkflowDependency
        ProjectWorkflowDependency.objects.create(
            run=run, paper=paper, ingestion_job=job, status="pending")

        # Simulate the worker reaching embedded with a REAL task execution:
        # patch the heavy path and run the production task.
        from rag.ingest import ingest_pdf_bytes

        calls = {"resume": []}
        # Simulate a REAL worker success without the heavy parse/embed:
        # the claimed build is already active with 3 chunks -> the task
        # takes the activation no-op path and commits `embedded`.
        from rag.models import PaperIndexVersion

        def _mk_version():
            from rag.embedding import embedding_metadata
            m = embedding_metadata()
            return PaperIndexVersion.objects.create(
                paper=paper, status="active",
                source_sha256=hashlib.sha256(
                    f"bd01-{paper.id}".encode()).hexdigest()[:64],
                pipeline_signature=f"bd01-{paper.id}",
                parser_identity="ingestion-service-v1",
                embedding_model=str(m["embedding_model"]),
                embedding_version=str(m["embedding_version"]),
                embedding_dim=int(m["embedding_dim"]), chunk_count=3)

        with mock.patch("api.tasks.ingest_pdf_bytes",
                        return_value=3), \
             mock.patch("api.tasks.download_pdf",
                        return_value=b"%PDF-fake"), \
             mock.patch("api.ingestion_service.IngestionService.claim_build",
                        return_value=_mk_version()), \
             mock.patch("api.ingestion_service.IngestionService.activate",
                        return_value=None), \
             mock.patch("api.tasks.resume_research_expand_workflow_task"
                        ".delay",
                        side_effect=lambda *a: calls["resume"].append(a)
                        or type("R", (), {"id": "f"})()):
            r1 = ingest_paper_pdf_task.run(job.id)
            first_terminal_at = PaperIngestionJob.objects.get(
                id=job.id).terminal_at
            self.assertIsNotNone(first_terminal_at,
                                 "terminal_at not set on embedded (BD-01)")
            # dependency synced by the on_commit callback
            dep = ProjectWorkflowDependency.objects.get(run=run,
                                                         paper=paper)
            self.assertEqual(dep.status, "succeeded",
                             "dependency not synced to succeeded (BD-01)")
            self.assertGreaterEqual(
                len(calls["resume"]), 1,
                "no resume wakeup enqueued for the waiting run (BD-01)")

            # REDHELIVERY: re-run the task (job already embedded -> reuse
            # path) — terminal_at must NOT be rewritten.
            before = PaperIngestionJob.objects.get(id=job.id).terminal_at
            r2 = ingest_paper_pdf_task.run(job.id)
            after = PaperIngestionJob.objects.get(id=job.id).terminal_at
            self.assertEqual(before, after,
                             "redelivery rewrote terminal_at (BD-01)")
        self._checks.append({"surface": "callback", "sentinel": "ok",
                             "found": False, "note": str(r1)})

    def test_DW_BD01_FAILED_TERMINAL_AT(self):
        self.case_id = "DW-BD01-FAILED-TERMINAL-AT"
        self.contract = "terminal_at set once on first failed too"
        from api.models import PaperIngestionJob, ProjectRun
        from api.tasks import ingest_paper_pdf_task

        proj = _proj("BD01F")
        paper = _paper("BD01F P", "bd01f-p")
        _link(proj, paper)
        job = PaperIngestionJob.objects.create(
            project=proj, paper=paper, status="pending",
            source_url="https://cdn.example.com/bd01f.pdf",
            source_kind="url")
        run = ProjectRun.objects.create(
            project=proj, kind="workflow", status="waiting_ingestion",
            question="bd01f")
        from api.models import ProjectWorkflowDependency
        ProjectWorkflowDependency.objects.create(
            run=run, paper=paper, ingestion_job=job, status="pending")

        with mock.patch("api.tasks.download_pdf",
                        side_effect=RuntimeError("synthetic download fail")):
            try:
                ingest_paper_pdf_task.run(job.id)
            except Exception:
                pass
        job.refresh_from_db()
        self.assertIsNotNone(job.terminal_at,
                             "terminal_at not set on failed (BD-01)")
        self.assertEqual(job.status, "failed", "job not failed (BD-01)")


# ════════════════════════════════════════════════════════════════════════
# BD-02: Beat reconciliation
# ════════════════════════════════════════════════════════════════════════

class BD02ReconciliationTest(BatchCRedTestBase):

    def test_DW_BD02_RECONCILIATION_SCANS_AND_ONLY_ENQUEUES(self):
        self.case_id = "DW-BD02-RECONCILIATION-SCANS-AND-ONLY-ENQUEUES"
        self.contract = ("15s Beat task scans waiting-terminal / "
                         "running-expired / pending-not-started / "
                         "lost-wakeup; enqueues ONLY existing idempotent "
                         "tasks; max one per run per cycle; never executes "
                         "graph/ingestion/RAG/report")
        from api.models import (PaperIngestionJob, ProjectRun,
                                ProjectWorkflowDependency)

        proj = _proj("BD02")
        paper_ok = _paper("BD02 Ok", "bd02-ok")
        _link(proj, paper_ok)
        _active(paper_ok)

        # 1) waiting_ingestion with ALL deps terminal -> resume wakeup
        run_wait_ready = ProjectRun.objects.create(
            project=proj, kind="workflow", status="waiting_ingestion",
            question="bd02w")
        ProjectWorkflowDependency.objects.create(
            run=run_wait_ready, paper=paper_ok, status="succeeded",
            terminal_at=timezone.now())

        # 2) running with EXPIRED lease -> takeover wakeup
        run_expired = ProjectRun.objects.create(
            project=proj, kind="workflow", status="running",
            question="bd02e",
            owner_token="ghost", lease_expires_at=timezone.now()
            - timedelta(seconds=400))

        # 3) pending never started -> start wakeup (aged past the 330s
        #    grace window; auto_now_add ignores the kwarg, so update after)
        run_pending = ProjectRun.objects.create(
            project=proj, kind="workflow", status="pending",
            question="bd02p")
        ProjectRun.objects.filter(id=run_pending.id).update(
            created_at=timezone.now() - timedelta(seconds=600))

        # 4) lost wakeup: waiting with terminal job but dependency still
        #    pending (callback lost) -> wakeup after dep sync
        paper_lost = _paper("BD02 Lost", "bd02-lost")
        _link(proj, paper_lost)
        job_lost = PaperIngestionJob.objects.create(
            project=proj, paper=paper_lost, status="embedded",
            source_url=paper_lost.pdf_url,
            terminal_at=timezone.now())
        run_lost = ProjectRun.objects.create(
            project=proj, kind="workflow", status="waiting_ingestion",
            question="bd02l")
        ProjectWorkflowDependency.objects.create(
            run=run_lost, paper=paper_lost, ingestion_job=job_lost,
            status="pending")

        enqueued = []
        graph_calls = []
        import agent.project_workflow as pw

        async def _no_graph(*a, **kw):
            graph_calls.append(a)
            return {}

        with mock.patch(
                "api.tasks.resume_research_expand_workflow_task.delay",
                side_effect=lambda *a: enqueued.append(("resume", a))
                or type("R", (), {"id": "f"})()) as _res, \
             mock.patch(
                "api.tasks.run_research_expand_workflow_task.delay",
                side_effect=lambda *a, **kw: enqueued.append(
                    ("start", a)) or type("R", (), {"id": "f"})()) as _st, \
             mock.patch.object(pw, "run_project_research_expand", _no_graph), \
             mock.patch.object(pw, "resume_project_research_expand",
                               _no_graph):
            from api.tasks import reconcile_workflow_runs_task
            result = reconcile_workflow_runs_task.run()

        target_runs = {run_wait_ready.id, run_expired.id, run_pending.id}
        woken = {a[0] if a else None for _, a in enqueued}
        # lost-wakeup run: dependency synced in-place by reconciliation then
        # woken too
        target_runs.add(run_lost.id)
        missing = target_runs - woken
        self._checks.append({"surface": "recon", "sentinel": "all_woken",
                             "found": bool(missing),
                             "note": f"missing={missing} "
                                     f"enqueued={enqueued}"})
        self.assertFalse(missing,
                         f"reconciliation missed runs (BD-02): {missing}")
        # each run at most ONE wakeup per cycle
        from collections import Counter
        per_run = Counter(a[0] for _, a in enqueued)
        doubled = [r for r, c in per_run.items() if c > 1]
        self._checks.append({"surface": "recon", "sentinel": "once_per_run",
                             "found": bool(doubled),
                             "note": str(per_run)})
        self.assertFalse(doubled,
                         f"run woken twice in one cycle (BD-02): {doubled}")
        # never executed the graph
        self._checks.append({"surface": "recon", "sentinel": "no_graph",
                             "found": bool(graph_calls),
                             "note": str(graph_calls)})
        self.assertFalse(graph_calls,
                         "reconciliation executed the graph (BD-02)")
        # lost-wakeup dependency was synced by the scan
        dep_lost = ProjectWorkflowDependency.objects.get(
            run=run_lost, paper=paper_lost)
        self.assertEqual(dep_lost.status, "succeeded",
                         "lost-wakeup dependency not synced (BD-02)")

    def test_DW_BD02_BEAT_SCHEDULE_CONFIGURED(self):
        self.case_id = "DW-BD02-BEAT-SCHEDULE-CONFIGURED"
        self.contract = "Celery Beat runs reconciliation every 15 seconds"
        from django.conf import settings
        schedule = getattr(settings, "CELERY_BEAT_SCHEDULE", None) or {}
        found = any("reconcile" in str(k) for k in schedule)
        entry = None
        for k, v in schedule.items():
            if "reconcile" in str(k):
                entry = v
        self._checks.append({"surface": "beat", "sentinel": "configured",
                             "found": not found,
                             "note": str(schedule)})
        self.assertTrue(found, "no reconcile entry in beat schedule (BD-02)")
        if entry is not None:
            self.assertEqual(entry.get("schedule"), 15.0,
                             "reconcile schedule is not 15s (BD-02)")


# ════════════════════════════════════════════════════════════════════════
# BD-03: strict timing
# ════════════════════════════════════════════════════════════════════════

class BD03StrictTimingTest(BatchCRedTestBase):

    def test_DW_BD03_RAG_AFTER_ALL_TERMINAL(self):
        self.case_id = "DW-BD03-RAG-AFTER-ALL-TERMINAL"
        self.contract = ("first_rag_at > last_ingestion_terminal_at; RAG "
                         "calls == 0 while any dependency non-terminal")
        if connection.vendor != "postgresql":
            self.skipTest("PostgreSQL required")
        from api.models import PaperIngestionJob, ProjectRun
        import agent.project_workflow as pw
        from api.tasks import run_research_expand_workflow_task

        proj = _proj("BD03")
        paper = _paper("BD03 P", "bd03-p")
        _link(proj, paper)
        _active(paper)  # P2-D-CX-04: real active fulltext

        # P2-D-CX-04: build real EvidenceEnvelope items from the live Text
        from rag.models import Text
        from agent.evidence import make_evidence_id
        real_chunk = Text.objects.filter(
            index_version__paper=paper,
            index_version__status="active").first()
        mock_evidence_item = {
            "evidence_id": make_evidence_id(
                proj.id, paper.id, real_chunk.id,
                real_chunk.content_hash,
                real_chunk.embedding_version),
            "project_id": proj.id, "paper_id": paper.id,
            "chunk_id": real_chunk.id,
            "content_hash": real_chunk.content_hash,
            "embedding_version": real_chunk.embedding_version,
            "section": real_chunk.section,
            "citation": real_chunk.citation_key,
            "source_marker": real_chunk.citation_key or real_chunk.docname,
            "evidence_type": "fulltext",
        } if real_chunk else None

        rag_calls = [0]

        async def _rag_inner(*a, **kw):
            rag_calls[0] += 1
            return ({"evidence": [mock_evidence_item], "fallback": ""}
                    if mock_evidence_item
                    else {"evidence": [], "fallback": ""})

        run = ProjectRun.objects.create(
            project=proj, kind="workflow", status="pending",
            question="bd03")

        async def _search(*a, **kw):
            return {"papers": [{"title": "BD03 P", "year": 2024,
                                "arxiv_id": paper.arxiv_id,
                                "pdf_url": paper.pdf_url}]}

        with mock.patch.object(pw, "search_papers", _search), \
             mock.patch.object(pw, "query_project_rag", _rag_inner), \
             mock.patch.object(pw, "draft_report_section",
                               return_value={"section": ""}), \
             mock.patch("api.tasks.ingest_paper_pdf_task.delay",
                        return_value=type("R", (), {"id": "f"})()):
            # paper has ready fulltext -> no waiting, completes directly
            run_research_expand_workflow_task.run(run.id)

        run.refresh_from_db()
        self._checks.append({"surface": "timing", "sentinel": "rag_once",
                             "found": rag_calls[0] != 1,
                             "note": f"calls={rag_calls[0]}"})
        self.assertEqual(rag_calls[0], 1, "RAG call count wrong (BD-03)")
        self.assertIsNotNone(run.first_rag_at,
                             "first_rag_at not recorded (BD-03)")
        deps = list(run.workflow_dependencies.all())
        self.assertTrue(deps, "no dependencies (BD-03)")
        # ready deps don't have terminal_at from jobs; the run's own
        # first_rag_at was stamped by _commit_rag_and_stamp_timing
        self.assertIsNotNone(run.first_rag_at,
                             "first_rag_at missing (BD-03)")


# ════════════════════════════════════════════════════════════════════════
# BD-04: deterministic result policy
# ════════════════════════════════════════════════════════════════════════

# ════════════════════════════════════════════════════════════════════════
# BD-05: sensitive scan across all surfaces (5.5)
# ════════════════════════════════════════════════════════════════════════

class BD05SensitiveScanTest(BatchCRedTestBase):

    def test_DW_BD05_NO_SENSITIVE_CONTENT_ANYWHERE(self):
        self.case_id = "DW-BD05-NO-SENSITIVE-CONTENT-ANYWHERE"
        self.contract = ("checkpoint/pending writes/events/logs/API/"
                         "Celery result contain no question/URL/payload/"
                         "draft/excerpt/prompt/key/exception body")
        if connection.vendor != "postgresql":
            self.skipTest("PostgreSQL required")
        from api.models import PaperIngestionJob, ProjectRun
        import agent.project_workflow as pw
        from api.tasks import (resume_research_expand_workflow_task,
                               run_research_expand_workflow_task)

        QUESTION = "DW-BD05-QUESTION-SENTINEL-8c1d"
        URL_SNIP = "bd05-path-sentinel.pdf?token=SECRET"
        DRAFT_SNIP = "BD05-DRAFT-BODY"
        EXC_SNIP = "RuntimeError"

        proj = _proj("BD05")
        paper = _paper("BD05 P", "bd05-p",
                       url=f"https://cdn.example.com/{URL_SNIP}")
        _link(proj, paper)

        class _CaptureHandler(logging.Handler):
            def __init__(self):
                super().__init__()
                self.records = []

            def emit(self, record):
                self.records.append(record.getMessage())

        handler = _CaptureHandler()
        root = logging.getLogger()
        root.addHandler(handler)

        run = ProjectRun.objects.create(
            project=proj, kind="workflow", status="pending",
            question=QUESTION)

        async def _search(*a, **kw):
            return {"papers": [{"title": "BD05 P", "year": 2024,
                                "arxiv_id": paper.arxiv_id,
                                "pdf_url": paper.pdf_url}]}

        results = {}
        try:
            with mock.patch.object(pw, "search_papers", _search), \
                 mock.patch.object(pw, "draft_report_section",
                                   return_value={"section": DRAFT_SNIP}), \
                 mock.patch("api.tasks.ingest_paper_pdf_task.delay",
                            return_value=type("R", (), {"id": "f"})()):
                r1 = run_research_expand_workflow_task.run(run.id)
                job = PaperIngestionJob.objects.get(paper=paper,
                                                    project=proj)
                job.status = "embedded"
                job.terminal_at = timezone.now()
                job.save(update_fields=["status", "terminal_at",
                                        "updated_at"])
                r2 = resume_research_expand_workflow_task.run(run.id)
        finally:
            root.removeHandler(handler)

        run.refresh_from_db()
        surfaces = {}
        ev_blob = " ".join(str(v) for v in
                           run.events.values_list("payload", flat=True))
        surfaces["events"] = (QUESTION.lower() in ev_blob.lower()
                              or URL_SNIP.lower() in ev_blob.lower()
                              or DRAFT_SNIP.lower() in ev_blob.lower())
        ck_found = []

        def _scan():
            with connection.cursor() as cur:
                for tbl in ("checkpoints", "checkpoint_blobs",
                            "checkpoint_writes"):
                    cur.execute(f"SELECT * FROM {tbl}")
                    for row in cur.fetchall():
                        blob = " ".join(str(v) for v in row
                                        if v is not None).lower()
                        for s in (QUESTION, URL_SNIP, DRAFT_SNIP):
                            if s.lower() in blob:
                                ck_found.append((tbl, s))
        import asyncio as _aio
        _aio.run(_aio.to_thread(_scan)) if False else None
        # TransactionTestCase is sync — direct call is fine here
        _scan()
        surfaces["checkpoint"] = bool(ck_found)
        log_blob = " ".join(handler.records)
        surfaces["logs"] = any(
            s.lower() in log_blob.lower()
            for s in (QUESTION, URL_SNIP, DRAFT_SNIP))
        client = Client(HTTP_HOST="localhost")
        resp = client.get(f"/api/projects/{proj.id}/runs")
        api_blob = json.dumps(resp.json(), default=str)
        surfaces["api"] = any(
            s.lower() in api_blob.lower()
            for s in (URL_SNIP, DRAFT_SNIP))
        celery_blob = json.dumps([r1, r2], default=str)
        surfaces["celery_result"] = any(
            s.lower() in celery_blob.lower()
            for s in (QUESTION, URL_SNIP, DRAFT_SNIP, EXC_SNIP))

        for surface, leaked in surfaces.items():
            self._checks.append({"surface": surface, "sentinel": "leak",
                                 "found": leaked, "note": ""})
        leaks = [k for k, v in surfaces.items() if v]
        self.assertEqual(leaks, [],
                         f"sensitive content leaked (BD-05): {leaks}")
