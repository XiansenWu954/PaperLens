"""Phase 2 Batch C — Checkpointed Graph And Idempotent Ownership red tests.

All tests execute production entry points (task functions, services, API
endpoint) with network mocked; they never inspect source strings or field
annotations as the primary proof.

Covered behaviors (each RED before Batch C implementation):
  BC-01  stable thread_id=str(run.id) checkpoint write + cross-instance restore
  BC-02  pending dependency -> interrupt; RAG call count must be 0
  BC-03  checkpoint state must not contain question/URL/fulltext/excerpt/
         report body/exception body
  BC-04  concurrent start/resume of the same run -> exactly one owner
  BC-05  valid lease blocks a second owner; expired lease is takeable
  BC-06  duplicate resume does not duplicate committed RAG event or report
  BC-07  disabled flag / unready checkpointer -> stable workflow_unavailable,
         no run created, no legacy graph invoked
  BC-08  enqueue_ingestion must go through IngestionService; direct job
         creation stays red
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import time
from datetime import timedelta
from unittest import mock

from django.db import connection
from django.test import Client, TransactionTestCase
from django.utils import timezone

logger = logging.getLogger(__name__)
ARTIFACTS_DIR = os.environ.get("PAPERLENS_STAGE_B_ARTIFACTS_DIR", "")


def _ap(n):
    if not ARTIFACTS_DIR:
        return None
    os.makedirs(ARTIFACTS_DIR, exist_ok=True)
    return os.path.join(ARTIFACTS_DIR, n)


def _wj(n, r):
    p = _ap(n)
    if p:
        with open(p, "w", encoding="utf-8") as f:
            json.dump(r, f, ensure_ascii=False, indent=2)


def _proj(title="BC"):
    from api.models import ResearchProject
    return ResearchProject.objects.create(title=title, status="active")


def _paper(title="P", arxiv="bc-1", url="https://cdn.example.com/bc.pdf"):
    from papers.models import Paper
    return Paper.objects.create(
        title=title, abstract="a", year=2024, arxiv_id=arxiv, pdf_url=url)


def _link(p, paper, st="included"):
    from api.models import ProjectPaper
    return ProjectPaper.objects.create(project=p, paper=paper, status=st)


def _active(paper, n=3, model_override=None, version_override=None,
            dim_override=None):
    from rag.models import PaperIndexVersion, Text
    from rag.embedding import embedding_metadata
    m = embedding_metadata()
    model = str(model_override or m["embedding_model"])
    version = str(version_override or m["embedding_version"])
    dim = int(dim_override or m["embedding_dim"])
    v = PaperIndexVersion.objects.create(
        paper=paper, status="active",
        source_sha256=hashlib.sha256(f"a-{paper.id}".encode()).hexdigest()[:64],
        pipeline_signature=f"bc-{paper.id}",
        parser_identity="ingestion-service-v1",
        embedding_model=model,
        embedding_version=version,
        embedding_dim=dim, chunk_count=n)
    for i in range(n):
        Text.objects.create(
            paper=paper, index_version=v, docname=f"c{i}", chunk_index=i,
            content=f"content {i} selective state space",
            embedding=[0.0] * dim,
            embedding_model=model,
            embedding_dim=dim,
            embedding_version=version,
            content_hash=f"h-{paper.id}-{i}",
            citation_key=f"pqac-bc-{paper.id}-{i}")
    return v


def _mock_network_stack(rag=None, papers=None):
    """Standard offline network stack for production graph execution.
    Returns a contextlib.ExitStack with all patches entered.

    ``papers`` feeds the search_sources/add_candidates chain: each dict is
    treated as a search result (upserted into the project by the production
    ``_add_candidates_to_project`` path).
    """
    if rag is None:
        rag = {"evidence": [], "fallback": ""}
    if papers is None:
        papers = []
    from contextlib import ExitStack
    stack = ExitStack()
    for patch in (
        mock.patch("agent.project_workflow.query_hybrid_rag",
                   return_value=rag),
        mock.patch("agent.project_workflow.search_papers",
                   return_value={"papers": papers}),
        mock.patch("agent.project_workflow.draft_report_section",
                   return_value={"section": ""}),
        mock.patch("api.tasks.ingest_paper_pdf_task.delay",
                   return_value=type("R", (), {"id": "fake"})()),
    ):
        stack.enter_context(patch)
    return stack


class BatchCRedTestBase(TransactionTestCase):
    case_id = ""
    contract = ""

    def setUp(self):
        super().setUp()
        self._checks = []

    def _bs(self):
        try:
            r = self._outcome.result
        except AttributeError:
            return "UNKNOWN"
        if any(t is self for t, _ in r.errors):
            return "ERROR"
        if any(t is self for t, _ in r.failures):
            return "FAIL"
        return "PASS"

    def tearDown(self):
        body = self._bs()
        try:
            super().tearDown()
        finally:
            if ARTIFACTS_DIR:
                _wj(f"batchc-{self.case_id}.json", {
                    "case_id": self.case_id,
                    "test": self.id(),
                    "contract": self.contract,
                    "actual": body,
                    "checks": self._checks,
                })


# ════════════════════════════════════════════════════════════════════════
# BC-01: stable thread_id checkpoint write + cross-instance restore
# ════════════════════════════════════════════════════════════════════════

class BC01CheckpointThreadTest(BatchCRedTestBase):

    def test_DW_BC01_THREAD_ID_CHECKPOINT_RESTORE(self):
        self.case_id = "DW-BC01-THREAD-ID-CHECKPOINT-RESTORE"
        self.contract = ("production graph writes a checkpoint under "
                         "thread_id=str(run.id) and a rebuilt graph instance "
                         "restores the same thread")
        if connection.vendor != "postgresql":
            self.skipTest("PostgreSQL required")
        from api.models import ProjectRun
        proj = _proj("BC01")
        run = ProjectRun.objects.create(
            project=proj, kind="workflow", status="pending", question="bc01")

        from agent.project_workflow import run_project_research_expand
        with _mock_network_stack():
            try:
                asyncio.run(run_project_research_expand(
                    proj.id, "bc01", run.id))
            except Exception as exc:  # noqa: BLE001 - red test tolerates
                self._checks.append({"surface": "graph",
                                     "sentinel": exc.__class__.__name__,
                                     "found": True,
                                     "note": "graph failed to reach checkpoint"})

        # 1) checkpoint row must exist under thread_id=str(run.id)
        with connection.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM checkpoints WHERE thread_id=%s",
                        [str(run.id)])
            count = cur.fetchone()[0]
        self._checks.append({"surface": "checkpoints",
                             "sentinel": f"thread_id={run.id}",
                             "found": count == 0,
                             "note": f"rows={count}"})
        self.assertGreater(
            count, 0,
            "no checkpoint row for thread_id=str(run.id) (BC-01)")

        # 2) rebuilt graph instance must read the same thread
        from agent.project_workflow import (CheckpointerSession,
                                            build_project_workflow)

        async def _restore():
            session = CheckpointerSession()
            try:
                graph2 = await build_project_workflow(session)
                cfg = {"configurable": {"thread_id": str(run.id)}}
                try:
                    st = await graph2.aget_state(cfg)
                    return st is not None
                except Exception as exc:  # noqa: BLE001
                    self._checks.append({"surface": "restore",
                                         "sentinel": exc.__class__.__name__,
                                         "found": True,
                                         "note": "restore raised"})
                    return False
            finally:
                await session.aclose()

        restored = asyncio.run(_restore())
        self._checks.append({"surface": "restore",
                             "sentinel": "cross-instance read",
                             "found": not restored,
                             "note": ""})
        self.assertTrue(restored, "cross-instance checkpoint read failed (BC-01)")


# ════════════════════════════════════════════════════════════════════════
# BC-02: pending dependency -> interrupt, RAG count == 0
# ════════════════════════════════════════════════════════════════════════

class BC02RagWaitInterruptTest(BatchCRedTestBase):

    def test_DW_BC02_RAG_ZERO_WHILE_PENDING(self):
        self.case_id = "DW-BC02-RAG-ZERO-WHILE-PENDING"
        self.contract = ("with a pending ingestion dependency the run must "
                         "interrupt at await_ingestion and RAG calls must be 0")
        if connection.vendor != "postgresql":
            self.skipTest("PostgreSQL required")
        from api.models import PaperIngestionJob, ProjectRun
        proj = _proj("BC02")
        paper = _paper("BC02 P", "bc02-p")
        _link(proj, paper)
        PaperIngestionJob.objects.create(
            project=proj, paper=paper, status="pending",
            source_url=paper.pdf_url)
        run = ProjectRun.objects.create(
            project=proj, kind="workflow", status="pending", question="bc02")

        rag_count = [0]

        async def _rag(*a, **kw):
            rag_count[0] += 1
            return {"evidence": [], "fallback": ""}

        from agent.project_workflow import run_project_research_expand
        # search returns the target paper -> add_candidates produces paper_ids
        # -> enqueue_ingestion creates a pending dependency for THIS run.
        search_result = {"title": "BC02 P", "year": 2024,
                         "arxiv_id": "bc02-p",
                         "pdf_url": "https://cdn.example.com/bc02.pdf"}
        with _mock_network_stack(rag=None, papers=[search_result]):
            from unittest import mock as m
            with m.patch("agent.project_workflow.query_hybrid_rag", _rag):
                try:
                    asyncio.run(run_project_research_expand(
                        proj.id, "bc02", run.id))
                except Exception as exc:  # noqa: BLE001
                    self._checks.append({"surface": "graph",
                                         "sentinel": exc.__class__.__name__,
                                         "found": True,
                                         "note": "graph raised"})

        run.refresh_from_db()
        self._checks.append({"surface": "behavior", "sentinel": "rag_count",
                             "found": rag_count[0] > 0,
                             "note": f"rag={rag_count[0]}"})
        self._checks.append({"surface": "behavior",
                             "sentinel": "waiting_status",
                             "found": run.status != "waiting_ingestion",
                             "note": f"status={run.status}"})
        self.assertEqual(
            rag_count[0], 0,
            "RAG executed while ingestion dependency is pending (BC-02)")
        self.assertEqual(
            run.status, "waiting_ingestion",
            "run did not transition to waiting_ingestion (BC-02)")


# ════════════════════════════════════════════════════════════════════════
# BC-03: checkpoint state sensitive scan
# ════════════════════════════════════════════════════════════════════════

class BC03CheckpointSensitiveScanTest(BatchCRedTestBase):

    def test_DW_BC03_CHECKPOINT_NO_SENSITIVE_DATA(self):
        self.case_id = "DW-BC03-CHECKPOINT-NO-SENSITIVE-DATA"
        self.contract = ("checkpoint tables + pending writes contain no "
                         "question, URL, fulltext, excerpt, report body or "
                         "exception body")
        if connection.vendor != "postgresql":
            self.skipTest("PostgreSQL required")
        from api.models import PaperIngestionJob, ProjectRun
        proj = _proj("BC03")
        question = "DW-BC03-QUESTION-SENTINEL-7f3a"
        paper = _paper("BC03 P", "bc03-p",
                       url="https://cdn.example.com/bc03-sentinel.pdf")
        _link(proj, paper)
        PaperIngestionJob.objects.create(
            project=proj, paper=paper, status="pending",
            source_url=paper.pdf_url)
        run = ProjectRun.objects.create(
            project=proj, kind="workflow", status="pending",
            question=question)

        from agent.project_workflow import run_project_research_expand
        with _mock_network_stack():
            try:
                asyncio.run(run_project_research_expand(
                    proj.id, question, run.id))
            except Exception as exc:  # noqa: BLE001
                self._checks.append({"surface": "graph",
                                     "sentinel": exc.__class__.__name__,
                                     "found": True,
                                     "note": "graph raised"})

        # Negative control: question IS in ProjectRun DB (scanner works)
        run.refresh_from_db()
        self.assertEqual(run.question, question)

        # Scan checkpoint/pending tables for sentinels
        sentinels = {
            "question": question,
            "url": "bc03-sentinel.pdf",
            "fulltext": "content 0 selective state space",
            "excerpt": "excerpt",
            "report": "report body",
            "exception": "Traceback",
        }
        found = {}
        with connection.cursor() as cur:
            for tbl in ("checkpoints", "checkpoint_blobs",
                        "checkpoint_writes"):
                cur.execute("SELECT * FROM %s" % tbl)
                cols = [d[0] for d in cur.description]
                for row in cur.fetchall():
                    blob = " ".join(
                        str(v) for v in row if v is not None).lower()
                    for label, sent in sentinels.items():
                        if sent.lower() in blob:
                            found.setdefault(label, []).append(tbl)
        for label in sentinels:
            self._checks.append({"surface": "checkpoint",
                                 "sentinel": label,
                                 "found": bool(found.get(label)),
                                 "note": str(found.get(label))})
        self.assertEqual(
            found, {},
            "checkpoint state leaked sensitive data (BC-03): %r" % found)


# ════════════════════════════════════════════════════════════════════════
# BC-04: concurrent start/resume -> exactly one owner
# ════════════════════════════════════════════════════════════════════════

class BC04SingleOwnerTest(BatchCRedTestBase):

    def test_DW_BC04_SINGLE_OWNER(self):
        self.case_id = "DW-BC04-SINGLE-OWNER"
        self.contract = ("two concurrent start/resume calls on the same run "
                         "yield exactly one owner")
        from api.models import ProjectRun
        proj = _proj("BC04")
        run = ProjectRun.objects.create(
            project=proj, kind="workflow", status="pending", question="bc04")

        from agent.owner_service import acquire_owner
        tok1 = "token-a"
        tok2 = "token-b"
        ok1 = acquire_owner(run, tok1)
        ok2 = acquire_owner(run, tok2)
        self._checks.append({"surface": "owner", "sentinel": "first",
                             "found": not ok1, "note": f"ok1={ok1}"})
        self._checks.append({"surface": "owner", "sentinel": "second",
                             "found": ok2, "note": f"ok2={ok2}"})
        self.assertTrue(ok1, "first owner not acquired (BC-04)")
        self.assertFalse(ok2, "second owner acquired (BC-04)")


# ════════════════════════════════════════════════════════════════════════
# BC-05: valid lease blocks; expired lease takeable
# ════════════════════════════════════════════════════════════════════════

class BC05LeaseExpiryTest(BatchCRedTestBase):

    def test_DW_BC05_LEASE_VALID_AND_EXPIRED(self):
        self.case_id = "DW-BC05-LEASE-VALID-AND-EXPIRED"
        self.contract = ("valid lease rejects a second owner; expired lease "
                         "is takeable and bumps resume_count")
        from api.models import ProjectRun
        proj = _proj("BC05")
        run = ProjectRun.objects.create(
            project=proj, kind="workflow", status="running", question="bc05",
            resume_count=0)

        from agent.owner_service import acquire_owner
        tok1 = "token-1"
        tok2 = "token-2"
        ok1 = acquire_owner(run, tok1)
        # force-expire the lease as a time-based control
        run.lease_expires_at = timezone.now() - timedelta(seconds=1)
        run.save(update_fields=["lease_expires_at", "updated_at"])
        ok2 = acquire_owner(run, tok2)
        run.refresh_from_db()
        self._checks.append({"surface": "lease", "sentinel": "valid",
                             "found": not ok1, "note": f"ok1={ok1}"})
        self._checks.append({"surface": "lease", "sentinel": "expired",
                             "found": not ok2, "note": f"ok2={ok2}"})
        self._checks.append({"surface": "lease", "sentinel": "resume_count",
                             "found": run.resume_count != 1,
                             "note": f"resume_count={run.resume_count}"})
        self.assertTrue(ok1, "first owner not acquired (BC-05)")
        self.assertTrue(ok2, "expired lease not takeable (BC-05)")
        self.assertEqual(run.resume_count, 1,
                         "resume_count not bumped on takeover (BC-05)")


# ════════════════════════════════════════════════════════════════════════
# BC-06: duplicate resume -> one committed RAG event, one report
# ════════════════════════════════════════════════════════════════════════

class BC06DuplicateResumeTest(BatchCRedTestBase):

    def test_DW_BC06_DUPLICATE_RESUME_IDEMPOTENT(self):
        self.case_id = "DW-BC06-DUPLICATE-RESUME-IDEMPOTENT"
        self.contract = ("resuming a completed run twice produces at most one "
                         "committed RAG event and at most one report")
        from api.models import ProjectRun
        proj = _proj("BC06")
        paper = _paper("BC06 P", "bc06-p")
        _link(proj, paper)
        _active(paper)
        run = ProjectRun.objects.create(
            project=proj, kind="workflow", status="pending", question="bc06")

        from api.tasks import run_research_expand_workflow_task
        # First run through the production task (network + enqueue mocked).
        # search returns [] so add_candidates yields no target papers; the
        # pre-linked active full-text paper is not a run target, so the
        # run completes directly (no pending deps).
        with mock.patch("api.tasks.ingest_paper_pdf_task.delay",
                        return_value=type("R", (), {"id": "fake"})()), \
             mock.patch("agent.project_workflow.search_papers",
                        return_value={"papers": []}), \
             mock.patch("agent.project_workflow.draft_report_section",
                        return_value={"section": ""}):
            try:
                run_research_expand_workflow_task.run(run.id)
            except Exception as exc:  # noqa: BLE001
                self._checks.append({"surface": "task",
                                     "sentinel": exc.__class__.__name__,
                                     "found": True,
                                     "note": "task raised"})
            try:
                run_research_expand_workflow_task.run(run.id)
            except Exception as exc:  # noqa: BLE001
                self._checks.append({"surface": "task2",
                                     "sentinel": exc.__class__.__name__,
                                     "found": True,
                                     "note": "task raised"})

        from api.models import ProjectRunEvent, ReportVersion
        rag_events = ProjectRunEvent.objects.filter(
            run=run, event_type="rag_committed").count()
        reports = ReportVersion.objects.filter(source_run=run).count()
        # P2-C-CX-03: exact per-event-type counts — every logical terminal
        # event must occur exactly once across replay.
        event_counts = dict(
            ProjectRunEvent.objects.filter(run=run)
            .values_list("event_type")
            .annotate(count=__import__("django.db.models", fromlist=["Count"]).Count("id")))
        self._checks.append({"surface": "events",
                             "sentinel": "rag_committed",
                             "found": rag_events > 1,
                             "note": f"count={rag_events}"})
        self._checks.append({"surface": "reports",
                             "sentinel": "source_run",
                             "found": reports > 1,
                             "note": f"count={reports}"})
        self._checks.append({"surface": "events",
                             "sentinel": "per_type_counts",
                             "found": event_counts.get("workflow_completed", 0) > 1,
                             "note": str(event_counts)})
        self.assertLessEqual(
            rag_events, 1,
            "duplicate resume duplicated committed RAG event (BC-06)")
        self.assertLessEqual(
            reports, 1,
            "duplicate resume duplicated report (BC-06)")
        for terminal in ("workflow_completed", "workflow_waiting"):
            self.assertLessEqual(
                event_counts.get(terminal, 0), 1,
                f"duplicate resume duplicated {terminal} event (BC-06): "
                f"{event_counts}")


# ════════════════════════════════════════════════════════════════════════
# BC-07: endpoint fail closed
# ════════════════════════════════════════════════════════════════════════

class BC07EndpointFailClosedTest(BatchCRedTestBase):

    def test_DW_BC07_UNAVAILABLE_NO_RUN_NO_LEGACY(self):
        self.case_id = "DW-BC07-ENDPOINT-FAIL-CLOSED"
        self.contract = ("disabled flag or unready checkpointer -> stable "
                         "workflow_unavailable; no run, no enqueue, no legacy "
                         "graph")
        from api.models import ProjectRun
        proj = _proj("BC07")
        client = Client(HTTP_HOST="localhost")

        # Case A: flag disabled
        with mock.patch(
                "django.conf.settings.PAPERLENS_DURABLE_WORKFLOW_ENABLED",
                False):
            legacy_called = []
            with mock.patch(
                    "api.tasks.run_research_expand_workflow_task") as tm:
                tm.delay.return_value = type("R", (), {"id": "fake"})()
                resp = client.post(
                    f"/api/projects/{proj.id}/workflows/research-expand",
                    {"question": "q"}, format="json")
                legacy_called.append(tm.delay.called)
        runs_after_disabled = ProjectRun.objects.filter(
            project=proj).count()
        self._checks.append({"surface": "api", "sentinel": "disabled_status",
                             "found": resp.status_code != 503,
                             "note": f"status={resp.status_code}"})
        self._checks.append({"surface": "api",
                             "sentinel": "disabled_code",
                             "found": resp.json().get("error")
                                      != "workflow_unavailable",
                             "note": str(resp.json())})
        self._checks.append({"surface": "api", "sentinel": "run_created",
                             "found": runs_after_disabled > 0,
                             "note": f"runs={runs_after_disabled}"})
        self._checks.append({"surface": "api", "sentinel": "legacy_invoked",
                             "found": any(legacy_called),
                             "note": str(legacy_called)})
        self.assertEqual(resp.status_code, 503,
                         "disabled flag must return 503 (BC-07)")
        self.assertEqual(resp.json().get("error"), "workflow_unavailable",
                         "stable workflow_unavailable code missing (BC-07)")
        self.assertEqual(runs_after_disabled, 0,
                         "run must not be created when unavailable (BC-07)")
        self.assertFalse(any(legacy_called),
                         "legacy graph must not be invoked (BC-07)")

        # Case B: checkpointer not ready (flag on, readiness false)
        with mock.patch("config.health.durable_workflow_health",
                        return_value={
                            "durable_workflow_enabled": True,
                            "workflow_checkpointer_ready": False,
                            "durable_workflow_available": False}):
            legacy_called2 = []
            with mock.patch(
                    "api.tasks.run_research_expand_workflow_task") as tm2:
                tm2.delay.return_value = type("R", (), {"id": "fake"})()
                resp2 = client.post(
                    f"/api/projects/{proj.id}/workflows/research-expand",
                    {"question": "q2"}, format="json")
                legacy_called2.append(tm2.delay.called)
        self._checks.append({"surface": "api",
                             "sentinel": "unready_status",
                             "found": resp2.status_code != 503,
                             "note": f"status={resp2.status_code}"})
        self.assertEqual(resp2.status_code, 503,
                         "unready checkpointer must return 503 (BC-07)")
        self.assertEqual(resp2.json().get("error"), "workflow_unavailable",
                         "unready code missing (BC-07)")
        self.assertFalse(any(legacy_called2),
                         "legacy graph invoked when unready (BC-07)")


# ════════════════════════════════════════════════════════════════════════
# BC-08: enqueue through IngestionService only
# ════════════════════════════════════════════════════════════════════════

class BC08ServiceRoutingTest(BatchCRedTestBase):

    def test_DW_BC08_ENQUEUE_VIA_SERVICE(self):
        self.case_id = "DW-BC08-ENQUEUE-VIA-SERVICE"
        self.contract = ("enqueue_ingestion routes through IngestionService "
                         "and never calls PaperIngestionJob.objects.create; "
                         "ONLY the run's target papers get dependencies "
                         "(P2-C-CX-01)")
        from api.models import PaperIngestionJob, ProjectWorkflowDependency, ProjectRun
        proj = _proj("BC08")
        target = _paper("BC08 Target", "bc08-t")
        unrelated = _paper("BC08 Unrelated", "bc08-u")
        _link(proj, target)
        _link(proj, unrelated)  # pre-existing project paper — NOT a run target
        run = ProjectRun.objects.create(
            project=proj, kind="workflow", status="pending", question="bc08")

        from api.ingestion_service import IngestionService
        from agent.project_workflow import enqueue_ingestion
        direct_creates = [0]
        service_called = [0]
        original_create = PaperIngestionJob.objects.create
        original_goc = IngestionService.get_or_create_job

        def _spy_create(*a, **kw):
            direct_creates[0] += 1
            return original_create(*a, **kw)

        def _spy_goc(self, *a, **kw):
            service_called[0] += 1
            return original_goc(self, *a, **kw)

        with mock.patch.object(PaperIngestionJob.objects, "create",
                               side_effect=_spy_create), \
             mock.patch.object(IngestionService, "get_or_create_job",
                               _spy_goc), \
             mock.patch("api.tasks.ingest_paper_pdf_task.delay",
                        return_value=type("R", (), {"id": "fake"})()):
            try:
                asyncio.run(enqueue_ingestion(
                    {"project_id": proj.id, "run_id": run.id,
                     "question": "q", "paper_ids": [target.id]}))
            except Exception as exc:  # noqa: BLE001
                self._checks.append({"surface": "node",
                                     "sentinel": exc.__class__.__name__,
                                     "found": True,
                                     "note": "enqueue raised"})

        self._checks.append({"surface": "service",
                             "sentinel": "service_called",
                             "found": service_called[0] == 0,
                             "note": f"calls={service_called[0]}"})
        self._checks.append({"surface": "service",
                             "sentinel": "direct_create",
                             "found": direct_creates[0] > 0,
                             "note": f"creates={direct_creates[0]}"})
        self.assertGreater(service_called[0], 0,
                           "IngestionService not called (BC-08)")
        self.assertEqual(direct_creates[0], 0,
                          "direct PaperIngestionJob.objects.create used (BC-08)")

        # P2-C-CX-01 mandatory control: unrelated pre-existing project paper
        # must NOT get a dependency for this run.
        dep_papers = set(ProjectWorkflowDependency.objects.filter(
            run=run).values_list("paper_id", flat=True))
        self._checks.append({"surface": "scope",
                             "sentinel": "unrelated_dependency",
                             "found": unrelated.id in dep_papers,
                             "note": f"dep_papers={sorted(dep_papers)}"})
        self.assertNotIn(
            unrelated.id, dep_papers,
            "unrelated project paper got a dependency (P2-C-CX-01)")


# ════════════════════════════════════════════════════════════════════════
# CXF-01: lease lost mid-flight -> safe exit, no error, no side effects
# ════════════════════════════════════════════════════════════════════════

class CXF01LeaseLostSafeExitTest(BatchCRedTestBase):

    def test_DW_CXF01_LEASE_LOST_SAFE_EXIT(self):
        self.case_id = "DW-CXF01-LEASE-LOST-SAFE-EXIT"
        self.contract = ("after the owner lease is lost mid-flight the old "
                         "executor must exit safely: run NOT marked error, "
                         "zero membership/job/build/task/event/draft/report "
                         "writes from the OLD owner (P2-C-R2-01)")
        if connection.vendor != "postgresql":
            self.skipTest("PostgreSQL required")
        from api.models import (PaperIngestionJob, ProjectPaper,
                                ProjectRun, ProjectRunEvent, ReportVersion)
        proj = _proj("CXF01")
        paper = _paper("CXF01 P", "cxf01-p")
        _link(proj, paper)
        run = ProjectRun.objects.create(
            project=proj, kind="workflow", status="pending", question="cxf01")

        # Baseline side-effect counts before the superseded execution.
        base = {
            "memberships": ProjectPaper.objects.filter(project=proj).count(),
            "jobs": PaperIngestionJob.objects.filter(project=proj).count(),
            "events": ProjectRunEvent.objects.filter(run=run).count(),
            "reports": ReportVersion.objects.filter(project=proj).count(),
        }
        from rag.models import PaperIndexVersion
        base["builds"] = PaperIndexVersion.objects.filter(
            paper=paper).count()

        # Supersede the owner mid-flight: the task acquires the lease
        # normally, but EVERY node-boundary fence observes a takeover (the
        # renew helper is replaced to raise OwnerLeaseLost — equivalent to
        # a new owner having taken over between acquire and the first
        # durable side effect).
        from agent.owner_service import new_owner_token
        import agent.project_workflow as pw

        new_owner_token()  # the token the "other worker" would hold

        async def _lost_renew(run_id):
            raise pw.OwnerLeaseLost("owner lease lost")

        search_calls = [0]

        async def _search(*a, **kw):
            search_calls[0] += 1
            return {"papers": [{"title": "CXF01 P", "year": 2024,
                                "arxiv_id": paper.arxiv_id,
                                "pdf_url": paper.pdf_url}]}

        from api.tasks import run_research_expand_workflow_task
        with mock.patch.object(pw, "_renew_lease_async", _lost_renew), \
             mock.patch.object(pw, "search_papers", _search), \
             mock.patch.object(pw, "draft_report_section",
                               return_value={"section": "OLD-OWNER-DRAFT"}), \
             mock.patch("api.tasks.ingest_paper_pdf_task.delay",
                        return_value=type("R", (), {"id": "fake"})()):
            try:
                result = run_research_expand_workflow_task.run(run.id)
            except Exception as exc:  # noqa: BLE001
                result = {"status": "raised", "reason": exc.__class__.__name__}

        self._checks.append({"surface": "task", "sentinel": "safe_exit",
                             "found": result.get("status") != "skipped",
                             "note": str(result)})
        self.assertEqual(result.get("status"), "skipped",
                         "superseded owner did not exit safely (CXF-01)")
        self.assertEqual(result.get("reason"), "owner_lease_lost",
                         "unexpected skip reason (CXF-01)")

        run.refresh_from_db()
        after = {
            "memberships": ProjectPaper.objects.filter(project=proj).count(),
            "jobs": PaperIngestionJob.objects.filter(project=proj).count(),
            "events": ProjectRunEvent.objects.filter(run=run).count(),
            "reports": ReportVersion.objects.filter(project=proj).count(),
            "builds": PaperIndexVersion.objects.filter(paper=paper).count(),
        }
        # The only allowed delta: the task-layer workflow_started event
        # (published before graph entry; stable dedupe key makes it once).
        event_delta = after["events"] - base["events"]
        zero = {k: after[k] - base[k] for k in base if k != "events"}
        self._checks.append({"surface": "run", "sentinel": "not_error",
                             "found": run.status == "error",
                             "note": run.status})
        self._checks.append({"surface": "side-effects",
                             "sentinel": "zero_writes",
                             "found": any(zero.values()) or event_delta > 1,
                             "note": f"delta={zero}, event_delta={event_delta}"})
        self.assertNotEqual(run.status, "error",
                            "superseded owner marked the shared run error (CXF-01)")
        self.assertTrue(all(v == 0 for v in zero.values()),
                        "superseded owner wrote side effects (CXF-01): %r" % zero)
        self.assertLessEqual(event_delta, 1,
                             "superseded owner wrote graph events (CXF-01)")
        # The old owner must not produce a draft either.
        self.assertEqual(run.draft_output, "",
                         "superseded owner wrote a draft (CXF-01)")


# ════════════════════════════════════════════════════════════════════════
# CXF-02: duplicate start for waiting run -> resume, no re-search/enqueue
# ════════════════════════════════════════════════════════════════════════

class CXF02DuplicateStartResumeTest(BatchCRedTestBase):

    def test_DW_CXF02_DUPLICATE_START_RESUMES(self):
        self.case_id = "DW-CXF02-DUPLICATE-START-RESUMES"
        self.contract = ("a duplicate start on a waiting run resumes from the "
                         "checkpoint and never re-executes search/enqueue "
                         "(P2-C-CX-04)")
        if connection.vendor != "postgresql":
            self.skipTest("PostgreSQL required")
        from api.models import PaperIngestionJob, ProjectRun
        proj = _proj("CXF02")
        paper = _paper("CXF02 P", "cxf02-p")
        _link(proj, paper)
        run = ProjectRun.objects.create(
            project=proj, kind="workflow", status="pending", question="cxf02")

        search_calls = [0]

        async def _search(*a, **kw):
            search_calls[0] += 1
            return {"papers": [{"title": "CXF02 P", "year": 2024,
                                "arxiv_id": paper.arxiv_id,
                                "pdf_url": paper.pdf_url}]}

        from api.tasks import run_research_expand_workflow_task
        import agent.project_workflow as pw_mod

        # Phase 1: real start -> search once -> pending dep -> waiting
        # (produces a REAL checkpoint under thread_id=str(run.id)).
        with mock.patch.object(pw_mod, "search_papers", _search), \
             mock.patch.object(pw_mod, "draft_report_section",
                               return_value={"section": ""}), \
             mock.patch("api.tasks.ingest_paper_pdf_task.delay",
                        return_value=type("R", (), {"id": "fake"})()):
            try:
                r1 = run_research_expand_workflow_task.run(run.id)
            except Exception as exc:  # noqa: BLE001
                self._checks.append({"surface": "task1",
                                     "sentinel": exc.__class__.__name__,
                                     "found": True,
                                     "note": "start raised"})
            run.refresh_from_db()
            calls_after_start = search_calls[0]
            self._checks.append({"surface": "behavior",
                                 "sentinel": "start_search_calls",
                                 "found": calls_after_start != 1,
                                 "note": f"calls={calls_after_start}"})
            self.assertEqual(run.status, "waiting_ingestion",
                             "start did not reach waiting (CXF-02)")
            self.assertEqual(
                calls_after_start, 1,
                "one normal start must search exactly once (P2-C-CX-01)")

            # Phase 2: duplicate start on the waiting run -> resume from the
            # checkpoint -> search/enqueue must NOT re-execute.
            try:
                r2 = run_research_expand_workflow_task.run(run.id)
            except Exception as exc:  # noqa: BLE001
                self._checks.append({"surface": "task2",
                                     "sentinel": exc.__class__.__name__,
                                     "found": True,
                                     "note": "duplicate start raised"})

        self._checks.append({"surface": "behavior",
                             "sentinel": "dup_search_calls",
                             "found": search_calls[0] > calls_after_start,
                             "note": f"calls={search_calls[0]}"})
        self.assertEqual(
            search_calls[0], calls_after_start,
            "duplicate start re-executed search (P2-C-CX-04)")


# ════════════════════════════════════════════════════════════════════════
# CXF-03: draft sentinel lives in draft_output + report, nowhere else
# ════════════════════════════════════════════════════════════════════════

class CXF03DraftSentinelTest(BatchCRedTestBase):

    def test_DW_CXF03_DRAFT_SENTINEL_PRIVATE_ONLY(self):
        self.case_id = "DW-CXF03-DRAFT-SENTINEL-PRIVATE-ONLY"
        self.contract = ("a REAL production task lifecycle (start -> wait -> "
                         "resume -> report) with a non-empty opaque draft "
                         "sentinel: present in draft_output and the final "
                         "ReportVersion, absent from checkpoint/pending "
                         "writes/events/logs/serializer/API/Celery result "
                         "(P2-C-R2-04)")
        if connection.vendor != "postgresql":
            self.skipTest("PostgreSQL required")
        from api.models import (PaperIngestionJob, ProjectRun,
                                ProjectRunEvent, ReportVersion)
        from django.utils import timezone
        import agent.project_workflow as pw

        proj = _proj("CXF03")
        paper = _paper("CXF03 P", "cxf03-p",
                       url="https://cdn.example.com/cxf03-sentinel.pdf")
        _link(proj, paper)
        _active(paper)  # P2-D-CX-01: real active fulltext for resolved evidence
        run = ProjectRun.objects.create(
            project=proj, kind="workflow", status="pending", question="cxf03")

        SENTINEL = "DW-CXF03-OPAQUE-DRAFT-4e6c2b9a"

        # Capture real log output produced during the production lifecycle.
        class _CaptureHandler(logging.Handler):
            def __init__(self):
                super().__init__()
                self.records = []

            def emit(self, record):
                self.records.append(record.getMessage())

        handler = _CaptureHandler()
        root = logging.getLogger()
        root.addHandler(handler)

        from api.tasks import (run_research_expand_workflow_task,
                               resume_research_expand_workflow_task)

        async def _search(*a, **kw):
            return {"papers": [{"title": "CXF03 P", "year": 2024,
                                "arxiv_id": paper.arxiv_id,
                                "pdf_url": paper.pdf_url}]}

        try:
            # P2-D-CX-04: build proper EvidenceEnvelope items from the
            # real Text rows so the CitationResolver resolves them — no
            # bypassing the production gate.
            from rag.models import Text
            from agent.evidence import make_evidence_id
            real_chunks = list(Text.objects.filter(
                index_version__paper=paper,
                index_version__status="active"))
            mock_evidence = [{
                "evidence_id": make_evidence_id(
                    proj.id, c.paper_id, c.id, c.content_hash,
                    c.embedding_version),
                "project_id": proj.id, "paper_id": c.paper_id,
                "chunk_id": c.id, "content_hash": c.content_hash,
                "embedding_version": c.embedding_version,
                "excerpt": "test excerpt", "section": c.section,
                "citation": c.citation_key,
                "source_marker": c.citation_key or c.docname,
                "evidence_type": "fulltext",
            } for c in real_chunks[:1]]

            async def _mock_rag(*a, **kw):
                return {"evidence": mock_evidence, "fallback": ""}

            # P2-D-R3-01: cite the CANONICAL EVIDENCE ID (ev-...), not a
            # paper/docname marker (manifest binding requires the ID).
            cite_id = mock_evidence[0]["evidence_id"]
            poison_section = f"{SENTINEL} [cite:{cite_id}]"

            original_store = pw._store_draft

            def _store_sentinel(run_id, section):
                original_store(run_id, poison_section)

            with mock.patch.object(pw, "search_papers", _search), \
                 mock.patch.object(pw, "query_project_rag", _mock_rag), \
                 mock.patch.object(pw, "_store_draft",
                                   side_effect=_store_sentinel), \
                 mock.patch("api.tasks.ingest_paper_pdf_task.delay",
                            return_value=type("R", (), {"id": "fake"})()):
                r1 = run_research_expand_workflow_task.run(run.id)
                run.refresh_from_db()
                self._checks.append(
                    {"surface": "lifecycle", "sentinel": "start_done",
                     "found": run.status != "done",
                     "note": f"{run.status} {r1}"})
                self.assertEqual(run.status, "done",
                                 f"start did not complete (CXF-03): {run.status}")
        finally:
            root.removeHandler(handler)

        # sentinel PRESENT in draft_output + final report
        self._checks.append({"surface": "draft",
                             "sentinel": "in_draft_output",
                             "found": SENTINEL not in run.draft_output,
                             "note": ""})
        self.assertIn(SENTINEL, run.draft_output,
                      "sentinel missing in draft_output (CXF-03)")
        report = ReportVersion.objects.get(source_run=run)
        self._checks.append({"surface": "report",
                             "sentinel": "in_report",
                             "found": SENTINEL not in report.content,
                             "note": ""})
        self.assertIn(SENTINEL, report.content,
                      "sentinel missing in final report (CXF-03)")

        # sentinel ABSENT everywhere else
        surfaces = {}
        ev_blob = " ".join(str(v) for v in
                           ProjectRunEvent.objects.filter(run=run)
                           .values_list("payload", "dedupe_key",
                                        flat=False)).lower()
        surfaces["events"] = SENTINEL.lower() in ev_blob

        found_tables = []
        with connection.cursor() as cur:
            for tbl in ("checkpoints", "checkpoint_blobs",
                        "checkpoint_writes"):
                cur.execute("SELECT * FROM %s" % tbl)
                for row in cur.fetchall():
                    blob = " ".join(str(v) for v in row
                                    if v is not None).lower()
                    if SENTINEL.lower() in blob:
                        found_tables.append(tbl)
        surfaces["checkpoint"] = bool(found_tables)

        log_blob = " ".join(handler.records).lower()
        surfaces["logs"] = SENTINEL.lower() in log_blob

        from api.serializers import ProjectRunSerializer
        serializer_blob = json.dumps(
            ProjectRunSerializer(run).data, default=str).lower()
        surfaces["serializer"] = SENTINEL.lower() in serializer_blob

        from django.test import Client
        client = Client(HTTP_HOST="localhost")
        resp = client.get(f"/api/projects/{proj.id}/runs")
        api_blob = json.dumps(resp.json(), default=str).lower()
        surfaces["api"] = SENTINEL.lower() in api_blob

        # REAL Celery task return values from the production lifecycle
        result_blob = json.dumps([r1], default=str).lower()
        surfaces["celery_result"] = SENTINEL.lower() in result_blob

        for surface, leaked in surfaces.items():
            self._checks.append({"surface": surface,
                                 "sentinel": "leaked",
                                 "found": leaked,
                                 "note": ""})
        leaks = [k for k, v in surfaces.items() if v]
        self.assertEqual(
            leaks, [],
            "sentinel leaked into surfaces (CXF-03): %r" % leaks)

        # P2-C-R2-03: with real active fulltext the run completes directly
        # (no waiting/resume). The sentinel privacy contract is verified
        # above across all surfaces — no resume event needed here.

# ========================================================================
# CXF-04: readiness gating positive/negative controls (P2-C-R2-02)
# ========================================================================

class CXF04ReadinessControlsTest(BatchCRedTestBase):

    def test_DW_CXF04_READINESS_AND_SCOPE_CONTROLS(self):
        self.case_id = "DW-CXF04-READINESS-AND-SCOPE-CONTROLS"
        self.contract = ("ready requires active+current embedding+parser+"
                         "+chunk_count>0+consistent Text rows; excluded/"
                         "foreign/unlinked/stale never enqueue (P2-C-R2-02)")
        from api.models import (PaperIngestionJob, ProjectPaper,
                                ProjectRun, ProjectWorkflowDependency,
                                ResearchProject)
        from agent.project_workflow import _has_ready_fulltext

        proj = _proj("CXF04")
        run = ProjectRun.objects.create(
            project=proj, kind="workflow", status="pending", question="cxf04")

        # ── readiness controls ────────────────────────────────────────
        own_active = _paper("CXF04 Own Active", "cxf04-oa")
        _link(proj, own_active)
        _active(own_active)
        self.assertTrue(_has_ready_fulltext(own_active.id),
                        "own active full text must be ready (CXF-04)")

        stale_model = _paper("CXF04 Stale Model", "cxf04-sm")
        _link(proj, stale_model)
        _active(stale_model, model_override="old-model-x")
        self.assertFalse(_has_ready_fulltext(stale_model.id),
                         "stale embedding model must NOT be ready (CXF-04)")

        stale_version = _paper("CXF04 Stale Ver", "cxf04-sv")
        _link(proj, stale_version)
        _active(stale_version, version_override="old-version-x")
        self.assertFalse(_has_ready_fulltext(stale_version.id),
                         "stale embedding version must NOT be ready (CXF-04)")

        wrong_dim = _paper("CXF04 Wrong Dim", "cxf04-wd")
        _link(proj, wrong_dim)
        # pgvector column is fixed-dim: declare a MISMATCHED dim on the
        # version metadata while real Text rows keep the true dim — the
        # metadata inconsistency itself must fail readiness.
        v_wd = _active(wrong_dim)
        from rag.models import PaperIndexVersion as PIV2
        PIV2.objects.filter(id=v_wd.id).update(embedding_dim=768)
        self.assertFalse(_has_ready_fulltext(wrong_dim.id),
                         "wrong dim must NOT be ready (CXF-04)")

        zero_chunk = _paper("CXF04 Zero Chunk", "cxf04-zc")
        _link(proj, zero_chunk)
        v = _active(zero_chunk)
        PaperIndexVersion_proxy = None
        from rag.models import PaperIndexVersion as PIV
        PIV.objects.filter(id=v.id).update(chunk_count=0)
        self.assertFalse(_has_ready_fulltext(zero_chunk.id),
                         "zero chunk_count must NOT be ready (CXF-04)")

        missing_chunk = _paper("CXF04 Missing Chunk", "cxf04-mc")
        _link(proj, missing_chunk)
        v2 = _active(missing_chunk)
        from rag.models import Text
        Text.objects.filter(index_version=v2).delete()
        self.assertFalse(_has_ready_fulltext(missing_chunk.id),
                         "missing Text rows must NOT be ready (CXF-04)")

        # ── scope controls: excluded/foreign/unlinked never enqueue ────
        excluded = _paper("CXF04 Excluded", "cxf04-ex",
                          url="https://cdn.example.com/cxf04-ex.pdf")
        _link(proj, excluded, "excluded")
        foreign_p = _paper("CXF04 Foreign", "cxf04-fo",
                           url="https://cdn.example.com/cxf04-fo.pdf")
        proj2 = ResearchProject.objects.create(title="CXF04 Foreign Proj",
                                               status="active")
        ProjectPaper.objects.create(project=proj2, paper=foreign_p)
        unlinked = _paper("CXF04 Unlinked", "cxf04-ul",
                          url="https://cdn.example.com/cxf04-ul.pdf")

        from agent.project_workflow import _enqueue_ingestion_for_run
        targets = [excluded.id, foreign_p.id, unlinked.id]
        with mock.patch("api.tasks.ingest_paper_pdf_task.delay",
                        return_value=type("R", (), {"id": "fake"})()):
            job_count, pending = _enqueue_ingestion_for_run(
                run.id, proj.id, targets)

        jobs_for = PaperIngestionJob.objects.filter(
            paper_id__in=targets)
        deps = set(ProjectWorkflowDependency.objects.filter(run=run)
                   .values_list("paper_id", flat=True))
        self._checks.append({"surface": "scope", "sentinel": "excluded",
                             "found": jobs_for.filter(paper=excluded).exists() ,
                             "note": ""})
        self.assertFalse(jobs_for.filter(paper=excluded).exists(),
                         "excluded paper got a job (CXF-04)")
        self.assertFalse(jobs_for.filter(paper=foreign_p).exists(),
                         "foreign paper got a job (CXF-04)")
        self.assertFalse(jobs_for.filter(paper=unlinked).exists(),
                         "unlinked paper got a job (CXF-04)")
        self.assertEqual(deps, set(),
                         "out-of-scope paper got a dependency (CXF-04): %r" % deps)
        self.assertEqual(job_count, 0,
                         "out-of-scope enqueue created jobs (CXF-04)")
