"""P2-D-R4-01/03: production-integration gate + task tests.

R4-01: the workflow draft boundary (draft_report node → _draft_from_manifest
→ _store_draft) is NEVER mocked. Only the external retrieval engine
(query_project_rag) may be mocked to return envelopes from real chunks.
The stale case mutates the chunk BETWEEN draft and persist (never after
the report exists).

R4-03: mixed-terminal scenarios go through the REAL ingest_paper_pdf_task
callers (not the finalize helper).
"""
from __future__ import annotations

import asyncio
import logging
import threading
from unittest import mock

from django.db import connection
from django.utils import timezone

from agent.batch_c_red_tests import _active, _link, _paper, _proj, \
    BatchCRedTestBase
from agent.evidence import make_evidence_id


def _envelope_for(proj_id, chunk):
    return {
        "evidence_id": make_evidence_id(
            proj_id, chunk.paper_id, chunk.id, chunk.content_hash,
            chunk.embedding_version),
        "project_id": proj_id, "paper_id": chunk.paper_id,
        "chunk_id": chunk.id, "content_hash": chunk.content_hash,
        "embedding_version": chunk.embedding_version,
        "section": chunk.section, "citation": chunk.citation_key,
        "source_marker": chunk.citation_key or chunk.docname,
        "evidence_type": "fulltext",
    }


# ════════════════════════════════════════════════════════════════════════
# R4-01: production draft boundary tests (NOT mocked)
# ════════════════════════════════════════════════════════════════════════

class R4ProductionDraftGateTest(BatchCRedTestBase):

    def _run_production_positive(self, proj, paper, envelopes):
        """Positive path: full production run, only the external retrieval
        engine mocked. The draft boundary is UNMOCKED."""
        from api.models import ProjectRun
        import agent.project_workflow as pw
        from api.tasks import (resume_research_expand_workflow_task,
                               run_research_expand_workflow_task)

        run = ProjectRun.objects.create(
            project=proj, kind="workflow", status="pending",
            question=f"r4-{proj.id}")

        async def _search(*a, **kw):
            return {"papers": [{"title": paper.title, "year": 2024,
                                "arxiv_id": paper.arxiv_id,
                                "pdf_url": paper.pdf_url}]}

        async def _rag_engine(*a, **kw):
            return {"evidence": envelopes, "fallback": ""}

        with mock.patch.object(pw, "search_papers", _search), \
             mock.patch.object(pw, "query_project_rag", _rag_engine), \
             mock.patch("api.tasks.ingest_paper_pdf_task.delay",
                        return_value=type("R", (), {"id": "f"})()):
            r1 = run_research_expand_workflow_task.run(run.id)
            run.refresh_from_db()
        return run, r1

    def _run_persist_gate(self, proj, paper, envelopes, poison_draft):
        """Negative gate path: full production run; after the UNMOCKED
        draft node stores the draft, poison it (or mutate the chunk) —
        the UNMOCKED persist_report then re-verifies and REJECTS."""
        from api.models import ProjectRun
        import agent.project_workflow as pw
        from api.tasks import run_research_expand_workflow_task

        run = ProjectRun.objects.create(
            project=proj, kind="workflow", status="pending",
            question=f"r4-{proj.id}")

        async def _search(*a, **kw):
            return {"papers": [{"title": paper.title, "year": 2024,
                                "arxiv_id": paper.arxiv_id,
                                "pdf_url": paper.pdf_url}]}

        async def _rag_engine(*a, **kw):
            return {"evidence": envelopes, "fallback": ""}

        original_store = pw._store_draft

        def _store_and_poison(run_id, section):
            original_store(run_id, section)
            poison_draft(run_id)

        with mock.patch.object(pw, "search_papers", _search), \
             mock.patch.object(pw, "query_project_rag", _rag_engine), \
             mock.patch.object(pw, "_store_draft",
                               side_effect=_store_and_poison), \
             mock.patch("api.tasks.ingest_paper_pdf_task.delay",
                        return_value=type("R", (), {"id": "f"})()):
            r1 = run_research_expand_workflow_task.run(run.id)
        return run, r1

    def test_DW_R4_POSITIVE_MANIFEST_DRAFT_REPORT(self):
        """Production path: recalled canonical chunk → real draft cites
        its canonical evidence ID → exactly ONE report."""
        self.case_id = "DW-R4-POSITIVE-MANIFEST-DRAFT-REPORT"
        if connection.vendor != "postgresql":
            self.skipTest("PostgreSQL required")
        from api.models import ReportVersion
        from rag.models import Text

        proj = _proj("R4P")
        paper = _paper("R4 Pos", "r4-pos")
        _link(proj, paper)
        _active(paper)
        chunk = Text.objects.filter(
            index_version__paper=paper,
            index_version__status="active").first()
        env = _envelope_for(proj.id, chunk)

        run, result = self._run_production_positive(proj, paper, [env])
        run.refresh_from_db()
        reports = ReportVersion.objects.filter(source_run=run).count()
        self._checks.append({"surface": "gate", "sentinel": "done_1_report",
                             "found": run.status != "done" or reports != 1,
                             "note": f"{run.status} reports={reports}"})
        self.assertEqual(run.status, "done",
                         "production draft+manifest must be done")
        self.assertEqual(reports, 1,
                         "production draft+manifest: exactly 1 report")

    def test_DW_R4_NEG_UNRECALLED_CHUNK(self):
        """Same paper 2 chunks; manifest recalls A; the stored draft is
        poisoned to cite B (the un-recalled canonical ID); the UNMOCKED
        persist_report gate rejects → error + ZERO reports."""
        self.case_id = "DW-R4-NEG-UNRECALLED-CHUNK"
        if connection.vendor != "postgresql":
            self.skipTest("PostgreSQL required")
        from api.models import ProjectRun, ReportVersion
        from rag.models import Text

        proj = _proj("R4U")
        paper = _paper("R4 Unrec", "r4-unrec")
        _link(proj, paper)
        version = _active(paper, n=2)
        chunks = list(Text.objects.filter(index_version=version)
                      .order_by("chunk_index"))
        recalled_env = _envelope_for(proj.id, chunks[0])
        unrecalled_env = _envelope_for(proj.id, chunks[1])

        def _poison(run_id):
            ProjectRun.objects.filter(id=run_id).update(
                draft_output=f"x [cite:{unrecalled_env['evidence_id']}]")

        run, result = self._run_persist_gate(
            proj, paper, [recalled_env], _poison)
        run.refresh_from_db()
        reports = ReportVersion.objects.filter(source_run=run).count()
        self.assertEqual(run.status, "error",
                         "un-recalled chunk cite must be error")
        self.assertEqual(reports, 0,
                         "un-recalled chunk cite: ZERO reports")

    def test_DW_R4_NEG_STALE_BETWEEN_DRAFT_AND_PERSIST(self):
        """Stale evidence ID: the chunk hash is mutated BETWEEN the draft
        store and the persist_report execution → zero reports."""
        self.case_id = "DW-R4-NEG-STALE-BETWEEN-DRAFT-AND-PERSIST"
        if connection.vendor != "postgresql":
            self.skipTest("PostgreSQL required")
        from api.models import ReportVersion
        from rag.models import Text

        proj = _proj("R4S")
        paper = _paper("R4 Stale", "r4-stale")
        _link(proj, paper)
        _active(paper)
        chunk = Text.objects.filter(
            index_version__paper=paper,
            index_version__status="active").first()
        env = _envelope_for(proj.id, chunk)

        def _poison(run_id):
            """Mutate the chunk hash between draft and persist — the
            manifest ID becomes stale (recomputed ID no longer matches)."""
            Text.objects.filter(id=chunk.id).update(
                content_hash="mutated-between-draft-and-persist")

        run, result = self._run_persist_gate(proj, paper, [env], _poison)
        run.refresh_from_db()
        reports = ReportVersion.objects.filter(source_run=run).count()
        self.assertEqual(run.status, "error",
                         "stale (mutated between draft/persist) must be error")
        self.assertEqual(reports, 0,
                         "stale evidence ID: ZERO reports")

    def test_DW_R4_NEG_MARKER_TEXT_COLLISION(self):
        """Same citation_key text on two chunks; manifest recalls A; the
        stored draft cites B's canonical ID (not in the manifest); the
        UNMOCKED persist gate rejects → ZERO reports."""
        self.case_id = "DW-R4-NEG-MARKER-TEXT-COLLISION"
        if connection.vendor != "postgresql":
            self.skipTest("PostgreSQL required")
        from api.models import ProjectRun, ReportVersion
        from rag.models import Text

        proj = _proj("R4C")
        paper = _paper("R4 Coll", "r4-coll")
        _link(proj, paper)
        version = _active(paper, n=2)
        chunks = list(Text.objects.filter(index_version=version)
                      .order_by("chunk_index"))
        Text.objects.filter(id=chunks[0].id).update(citation_key="pqac-same")
        Text.objects.filter(id=chunks[1].id).update(citation_key="pqac-same")
        recalled_env = _envelope_for(proj.id, chunks[0])
        unrecalled_env = _envelope_for(proj.id, chunks[1])

        def _poison(run_id):
            ProjectRun.objects.filter(id=run_id).update(
                draft_output=f"x [cite:{unrecalled_env['evidence_id']}]")

        run, result = self._run_persist_gate(
            proj, paper, [recalled_env], _poison)
        run.refresh_from_db()
        reports = ReportVersion.objects.filter(source_run=run).count()
        self.assertEqual(run.status, "error",
                         "colliding marker non-manifest ID must be error")
        self.assertEqual(reports, 0,
                         "colliding marker non-manifest ID: ZERO reports")

    def test_DW_R4_NEG_FAKE_MARKER(self):
        """Fabricated canonical evidence ID in the draft → zero reports."""
        self.case_id = "DW-R4-NEG-FAKE-MARKER"
        if connection.vendor != "postgresql":
            self.skipTest("PostgreSQL required")
        from api.models import ProjectRun, ReportVersion
        from rag.models import Text

        proj = _proj("R4F")
        paper = _paper("R4 Fake", "r4-fake")
        _link(proj, paper)
        _active(paper)
        chunk = Text.objects.filter(
            index_version__paper=paper,
            index_version__status="active").first()
        env = _envelope_for(proj.id, chunk)

        def _poison(run_id):
            ProjectRun.objects.filter(id=run_id).update(
                draft_output="x [cite:ev-fabricated9999999999]")

        run, result = self._run_persist_gate(proj, paper, [env], _poison)
        run.refresh_from_db()
        reports = ReportVersion.objects.filter(source_run=run).count()
        self.assertEqual(run.status, "error", "fake marker must be error")
        self.assertEqual(reports, 0, "fake marker: ZERO reports")


# ════════════════════════════════════════════════════════════════════════
# R4-03: production task-level mixed-terminal tests (REAL task callers)
# ════════════════════════════════════════════════════════════════════════

class R4TaskLevelTerminalTest(BatchCRedTestBase):

    def _mk_job(self, label):
        from api.models import (PaperIngestionJob, ProjectRun,
                                ProjectWorkflowDependency)
        proj = _proj(f"R4T-{label}")
        paper = _paper(f"R4T {label}", f"r4t-{label}".lower())
        _link(proj, paper)
        job = PaperIngestionJob.objects.create(
            project=proj, paper=paper, status="pending",
            source_url=paper.pdf_url, source_kind="url")
        dep_run = ProjectRun.objects.create(
            project=proj, kind="workflow", status="waiting_ingestion",
            question=label)
        dep = ProjectWorkflowDependency.objects.create(
            run=dep_run, paper=paper, ingestion_job=job, status="pending")
        return proj, paper, job, dep_run, dep

    def _mk_active_version(self, paper):
        import hashlib
        from rag.embedding import embedding_metadata
        from rag.models import PaperIndexVersion, Text
        m = embedding_metadata()
        v = PaperIndexVersion.objects.create(
            paper=paper, status="active",
            source_sha256=hashlib.sha256(
                f"r4t-{paper.id}".encode()).hexdigest()[:64],
            pipeline_signature=f"r4t-{paper.id}",
            parser_identity="ingestion-service-v1",
            embedding_model=str(m["embedding_model"]),
            embedding_version=str(m["embedding_version"]),
            embedding_dim=int(m["embedding_dim"]), chunk_count=3)
        for i in range(3):
            Text.objects.create(
                paper=paper, index_version=v, docname=f"c{i}",
                chunk_index=i, content=f"content {i}",
                embedding=[0.0]*int(m["embedding_dim"]),
                embedding_model=str(m["embedding_model"]),
                embedding_version=str(m["embedding_version"]),
                embedding_dim=int(m["embedding_dim"]),
                content_hash=f"h-{paper.id}-{i}",
                citation_key=f"pqac-{paper.id}-{i}")
        return v

    def test_DW_R4_EMBEDDED_WINNER_FAILED_LOSER_TASK(self):
        """REAL task path: successful ingestion first (embedded winner),
        then a failing re-delivery of the same job — the failed loser
        must safely return embedded WITHOUT raising, no error surfaces."""
        self.case_id = "DW-R4-EMBEDDED-WINNER-FAILED-LOSER"
        from api.tasks import ingest_paper_pdf_task
        proj, paper, job, dep_run, dep = self._mk_job("EF")
        version = self._mk_active_version(paper)

        # winner: successful real task execution
        r1 = None
        with mock.patch("api.tasks.ingest_pdf_bytes", return_value=3), \
             mock.patch("api.tasks.download_pdf", return_value=b"%PDF-x"), \
             mock.patch("api.ingestion_service.IngestionService.claim_build",
                        return_value=version), \
             mock.patch("api.ingestion_service.IngestionService.activate",
                        return_value=None):
            r1 = ingest_paper_pdf_task.run(job.id)

        job.refresh_from_db()
        winner_terminal_at = job.terminal_at
        winner_status = job.status

        # loser: failing re-delivery (download blows up)
        r2 = None
        raised = None
        with mock.patch("api.tasks.download_pdf",
                        side_effect=RuntimeError("synthetic download fail")):
            try:
                r2 = ingest_paper_pdf_task.run(job.id)
            except Exception as exc:
                raised = exc.__class__.__name__

        job.refresh_from_db()
        dep_run.refresh_from_db()
        dep.refresh_from_db()
        from api.models import ProjectRunEvent
        events = list(ProjectRunEvent.objects.filter(
            run=dep_run).values_list("event_type", flat=True))
        # ingestion run events: winner published completed, loser published nothing
        ing_events = list(ProjectRunEvent.objects.filter(
            run__kind="ingestion",
            run__project=proj).values_list("event_type", flat=True))

        self._checks.append({
            "surface": "task", "sentinel": "embedded_authoritative",
            "found": winner_status != "embedded" or job.status != "embedded",
            "note": f"winner={winner_status} final={job.status}"})
        self.assertEqual(job.status, "embedded",
                         "embedded winner must stay authoritative")
        self.assertEqual(job.terminal_at, winner_terminal_at,
                         "terminal_at must not change on loser re-delivery")
        # failed loser must NOT raise (safe return)
        self._checks.append({
            "surface": "task", "sentinel": "loser_safe_return",
            "found": raised is not None,
            "note": f"raised={raised} result={r2}"})
        self.assertIsNone(raised,
                          "failed loser must NOT raise when embedded won")
        self.assertIsNotNone(r2, "failed loser must return a result")
        self.assertEqual(r2.get("status"), "embedded",
                         "loser Celery result must be authoritative embedded")
        # no contradictory events
        self._checks.append({
            "surface": "events", "sentinel": "no_contradiction",
            "found": ("ingestion_failed" in ing_events),
            "note": str(ing_events)})
        self.assertNotIn("ingestion_failed", ing_events,
                         "failed loser must not publish ingestion_failed")
        self.assertIn("ingestion_completed", ing_events,
                      "winner must publish ingestion_completed")

    def test_DW_R4_FAILED_WINNER_SUCCESSFUL_LOSER_TASK(self):
        """REAL task path: failing ingestion first (failed winner), then a
        successful re-delivery — the success loser must return failed
        WITHOUT publishing completed."""
        self.case_id = "DW-R4-FAILED-WINNER-SUCCESSFUL-LOSER"
        from api.tasks import ingest_paper_pdf_task
        proj, paper, job, dep_run, dep = self._mk_job("FS")
        version = self._mk_active_version(paper)

        # winner: failing real task execution (download blows up)
        raised1 = None
        with mock.patch("api.tasks.download_pdf",
                        side_effect=RuntimeError("first synthetic fail")):
            try:
                ingest_paper_pdf_task.run(job.id)
            except Exception as exc:
                raised1 = exc.__class__.__name__

        job.refresh_from_db()
        winner_terminal_at = job.terminal_at
        winner_status = job.status
        self.assertIsNotNone(raised1,
                              "authoritative failure must raise")

        # loser: successful re-delivery
        r2 = None
        with mock.patch("api.tasks.ingest_pdf_bytes", return_value=3), \
             mock.patch("api.tasks.download_pdf", return_value=b"%PDF-x"), \
             mock.patch("api.ingestion_service.IngestionService.claim_build",
                        return_value=version), \
             mock.patch("api.ingestion_service.IngestionService.activate",
                        return_value=None):
            r2 = ingest_paper_pdf_task.run(job.id)

        job.refresh_from_db()
        dep_run.refresh_from_db()
        dep.refresh_from_db()
        from api.models import ProjectRunEvent
        ing_events = list(ProjectRunEvent.objects.filter(
            run__kind="ingestion", run__project=proj)
            .values_list("event_type", flat=True))

        self.assertEqual(job.status, "failed",
                         "failed winner must stay authoritative")
        self.assertEqual(job.terminal_at, winner_terminal_at,
                         "terminal_at must not change")
        self._checks.append({
            "surface": "task", "sentinel": "loser_no_completed",
            "found": r2 is not None and r2.get("status") == "embedded",
            "note": str(r2)})
        self.assertNotIn("ingestion_completed", ing_events,
                         "successful loser must not publish completed")
        self.assertIn("ingestion_failed", ing_events,
                      "winner must publish ingestion_failed")
        if r2 is not None:
            self.assertEqual(r2.get("status"), "failed",
                             "loser Celery result must be authoritative failed")
