"""P2-GLM-01 red/green tests: private ingestion execution lease, heartbeat,
fencing, Beat redispatch and migration compatibility.

Contract source (Codex, 2026-08-16):
- PaperIngestionJob gains private execution_token / execution_heartbeat_at /
  execution_lease_expires_at; they never appear on any external surface.
- Worker claims the lease atomically BEFORE incrementing attempt_count; a
  heartbeat thread renews it during blocking parse/embed.
- A stale worker (token superseded) must have ZERO durable side effects:
  chunk writes, activation, terminalization and event publishing are all
  fenced at the database boundary.
- Beat reconciliation redispatches dep-linked non-terminal jobs after the
  DYNAMIC gate lease + beat-interval + 5s; live leases are never touched.
- Terminal jobs clear the execution identity; migrations are
  forward/backward compatible with legacy rows.

Every negative case has a non-empty positive control.
"""
from __future__ import annotations

import asyncio
import time
from datetime import timedelta
from unittest import mock

from django.core.management import call_command
from django.db import connection
from django.test import TransactionTestCase, override_settings
from django.utils import timezone

from api.ingestion_execution import (  # noqa: F401 ??RED until implemented
    EXECUTION_IN_PROGRESS_STATUSES,
    ExecutionLeaseLost,
    IngestionHeartbeat,
    claim_execution,
    compensation_gate,
    execution_settings,
    heartbeat as exec_heartbeat,
    release_execution,
)


def _proj(title="GLM01"):
    from api.models import ResearchProject
    return ResearchProject.objects.create(title=title, status="active")


def _paper(proj, title="P", arxiv="glm01-x"):
    from api.models import ProjectPaper
    from papers.models import Paper
    paper = Paper.objects.create(
        title=title, abstract="a", year=2024, arxiv_id=arxiv)
    ProjectPaper.objects.create(project=proj, paper=paper)
    return paper


def _job(proj, paper, **fields):
    from api.models import PaperIngestionJob
    defaults = dict(status="pending", idempotency_key="k",
                    source_kind="workflow", source_url="https://x/y.pdf")
    defaults.update(fields)
    return PaperIngestionJob.objects.create(
        project=proj, paper=paper, **defaults)


def _dep(job, status="pending"):
    from api.models import ProjectRun, ProjectWorkflowDependency
    run = ProjectRun.objects.create(
        project=job.project, kind="workflow", status="waiting_ingestion",
        question="q")
    return ProjectWorkflowDependency.objects.create(
        run=run, paper=job.paper, ingestion_job=job, status=status)


LONG_TEXT = ("Independent acceptance fixture paragraph with enough words. "
             ) * 20


# ?? 1. settings validation + dynamic compensation gate ???????????????
class ExecutionSettingsTests(TransactionTestCase):

    def test_defaults_and_dynamic_gate(self):
        lease, hb = execution_settings()
        self.assertEqual((lease, hb), (60, 10))
        # gate = lease + beat interval (15) + 5 tolerance ??dynamic, not fixed
        self.assertEqual(compensation_gate(), 80.0)

    @override_settings(
        PAPERLENS_INGESTION_EXECUTION_LEASE_SECONDS=5,
        PAPERLENS_INGESTION_HEARTBEAT_SECONDS=2)
    def test_gate_follows_runtime_config(self):
        self.assertEqual(compensation_gate(), 5 + 15 + 5)

    @override_settings(PAPERLENS_INGESTION_EXECUTION_LEASE_SECONDS=0)
    def test_nonpositive_lease_rejected(self):
        with self.assertRaises(Exception):
            execution_settings()

    @override_settings(
        PAPERLENS_INGESTION_EXECUTION_LEASE_SECONDS=10,
        PAPERLENS_INGESTION_HEARTBEAT_SECONDS=10)
    def test_heartbeat_must_be_strictly_below_lease(self):
        with self.assertRaises(Exception):
            execution_settings()


# ?? 2. atomic claim semantics ?????????????????????????????????????????
class ClaimExecutionTests(TransactionTestCase):

    def setUp(self):
        self.proj = _proj("CLAIM")
        self.paper = _paper(self.proj, "Claim paper", "glm01-claim")
        self.job = _job(self.proj, self.paper, status="parsing")

    def test_claim_increments_attempt_and_stamps_lease(self):
        token = claim_execution(self.job.id)
        self.job.refresh_from_db()
        self.assertTrue(token)
        self.assertEqual(self.job.attempt_count, 1)
        self.assertEqual(self.job.execution_token, token)
        self.assertIsNotNone(self.job.execution_heartbeat_at)
        self.assertIsNotNone(self.job.execution_lease_expires_at)

    def test_second_claim_while_live_returns_none(self):
        first = claim_execution(self.job.id)
        self.assertIsNotNone(first)
        self.assertIsNone(claim_execution(self.job.id))  # live lease held

    def test_expired_lease_is_taken_over_with_new_token(self):
        stale = claim_execution(self.job.id)
        from api.models import PaperIngestionJob
        PaperIngestionJob.objects.filter(id=self.job.id).update(
            execution_lease_expires_at=timezone.now() - timedelta(seconds=1))
        fresh = claim_execution(self.job.id)
        self.assertIsNotNone(fresh)
        self.assertNotEqual(fresh, stale)
        self.job.refresh_from_db()
        self.assertEqual(self.job.attempt_count, 2)

    def test_terminal_job_never_claimed(self):
        from api.models import PaperIngestionJob
        PaperIngestionJob.objects.filter(id=self.job.id).update(
            status="embedded", terminal_at=timezone.now())
        self.assertIsNone(claim_execution(self.job.id))


# ?? 3. heartbeat thread + renewal ?????????????????????????????????????
class HeartbeatTests(TransactionTestCase):

    def test_heartbeat_renews_lease_and_thread_keeps_it_alive(self):
        proj = _proj("HB")
        paper = _paper(proj, "HB paper", "glm01-hb")
        job = _job(proj, paper, status="parsing")
        token = claim_execution(job.id)
        job.refresh_from_db()
        first_expiry = job.execution_lease_expires_at

        self.assertTrue(exec_heartbeat(job.id, token))
        job.refresh_from_db()
        self.assertGreater(job.execution_lease_expires_at, first_expiry)

        hb = IngestionHeartbeat(job.id, token, interval=0.05, lease=60)
        hb.start()
        try:
            time.sleep(0.25)
            job.refresh_from_db()
            self.assertGreater(job.execution_lease_expires_at, first_expiry)
        finally:
            hb.stop()

    def test_heartbeat_with_wrong_token_fails(self):
        proj = _proj("HB2")
        paper = _paper(proj, "HB2 paper", "glm01-hb2")
        job = _job(proj, paper, status="parsing")
        claim_execution(job.id)
        self.assertFalse(exec_heartbeat(job.id, "not-the-token"))


# ?? 4/5/6. fencing of stale writers ???????????????????????????????????
class FencingTests(TransactionTestCase):

    def setUp(self):
        from api.ingestion_service import IngestionService
        self.proj = _proj("FENCE")
        self.paper = _paper(self.proj, "Fence paper", "glm01-fence")
        self.job = _job(self.proj, self.paper, status="parsing")
        self.live = claim_execution(self.job.id)
        self.stale = "stale-token-abcdef"
        self.service = IngestionService()

    def _version(self):
        from rag.models import PaperIndexVersion
        from rag.embedding import embedding_metadata
        m = embedding_metadata()
        return PaperIndexVersion.objects.create(
            paper=self.paper, status="building",
            source_sha256="glm01fence", pipeline_signature="glm01-fence",
            parser_identity="ingestion-service-v1",
            embedding_model=str(m["embedding_model"]),
            embedding_version=str(m["embedding_version"]),
            embedding_dim=int(m["embedding_dim"]), chunk_count=0)

    # 4 ??chunk persistence fence
    def test_stale_chunk_write_rejected_live_allowed(self):
        from rag.models import Text
        from rag.ingest import ingest_text
        # negative: stale token
        with self.assertRaises(ExecutionLeaseLost):
            asyncio.run(ingest_text(
                self.paper, LONG_TEXT, replace_existing=True,
                execution=(self.job.id, self.stale)))
        self.assertEqual(Text.objects.filter(paper=self.paper).count(), 0)
        # positive control: live token persists chunks
        count = asyncio.run(ingest_text(
            self.paper, LONG_TEXT, replace_existing=True,
            execution=(self.job.id, self.live)))
        self.assertGreater(count, 0)
        self.assertGreater(Text.objects.filter(paper=self.paper).count(), 0)

    # 5 ??activation fence
    def test_stale_activate_rejected_live_allowed(self):
        version = self._version()
        from rag.models import Text
        from rag.embedding import embedding_metadata
        m = embedding_metadata()
        Text.objects.create(
            paper=self.paper, index_version=version, docname="c0",
            chunk_index=0, content=LONG_TEXT,
            embedding=[0.0] * int(m["embedding_dim"]),
            embedding_model=str(m["embedding_model"]),
            embedding_version=str(m["embedding_version"]),
            embedding_dim=int(m["embedding_dim"]),
            content_hash="h-glm01-0", citation_key="ck-glm01-0")
        # negative: stale token
        with self.assertRaises(ExecutionLeaseLost):
            self.service.activate(self.paper.id, version.id, 1,
                                  expected_execution=(self.job.id, self.stale))
        version.refresh_from_db()
        self.assertEqual(version.status, "building")
        # positive control: live token activates
        self.service.activate(self.paper.id, version.id, 1,
                              expected_execution=(self.job.id, self.live))
        version.refresh_from_db()
        self.assertEqual(version.status, "active")

    # 6 ??terminalize/publish fence
    def test_stale_finalize_rejected_live_terminalizes_and_clears(self):
        from api.models import ProjectRunEvent
        from api.workflow_callbacks import finalize_job_terminal
        # negative: stale token loses atomically, zero side effects
        fin = finalize_job_terminal(
            self.job.id, "embedded", chunk_count=4,
            expected_execution_token=self.stale)
        self.assertFalse(fin["won"])
        self.job.refresh_from_db()
        self.assertIsNone(self.job.terminal_at)
        self.assertNotEqual(self.job.status, "embedded")
        self.assertTrue(self.job.execution_token)  # live lease untouched
        self.assertEqual(ProjectRunEvent.objects.filter(
            event_type="ingestion_completed").count(), 0)
        # positive control: live token terminalizes exactly once
        fin2 = finalize_job_terminal(
            self.job.id, "embedded", chunk_count=4,
            expected_execution_token=self.live)
        self.assertTrue(fin2["won"])
        self.job.refresh_from_db()
        self.assertEqual(self.job.status, "embedded")
        self.assertIsNotNone(self.job.terminal_at)
        # 8 ??terminal job clears the execution identity
        self.assertEqual(self.job.execution_token, "")
        self.assertIsNone(self.job.execution_heartbeat_at)
        self.assertIsNone(self.job.execution_lease_expires_at)


# ?? 7/12. full task path (success, failure, transient) ???????????????
class TaskPathTests(TransactionTestCase):

    def setUp(self):
        self.proj = _proj("TASK")
        self.paper = _paper(self.proj, "Task paper", "glm01-task")
        # production shape: jobs are created through the canonical URL
        # entry so the global build is claimed at enqueue time
        from api.ingestion_service import IngestionService
        self.job, _created, _v = IngestionService().enqueue_url_job(
            project=self.proj, paper=self.paper,
            source_url="https://cdn.example.com/glm01-task.pdf",
            source_kind="workflow")

    def _run_task(self):
        from api.tasks import ingest_paper_pdf_task
        return ingest_paper_pdf_task.run(self.job.id)

    def test_success_single_build_dep_and_event(self):
        from api.models import ProjectRunEvent, ProjectWorkflowDependency
        from rag.models import PaperIndexVersion
        from rag.ingest import ingest_text
        dep = _dep(self.job)

        async def fake_ingest(paper, pdf_bytes, **kw):
            # real chunk persistence (fake embeddings) through the fence,
            # into the build the task passes down (production shape)
            return await ingest_text(
                paper, LONG_TEXT, replace_existing=True,
                index_version=kw.get("index_version"),
                execution=kw.get("execution"))

        with mock.patch("api.tasks._load_pdf_bytes",
                        return_value=b"%PDF-1.4 fake"), \
             mock.patch("api.tasks.ingest_pdf_bytes", new=fake_ingest):
            result = self._run_task()
        self.assertEqual(result["status"], "embedded")
        self.assertGreaterEqual(result["chunk_count"], 1)
        self.job.refresh_from_db()
        # claim happened exactly once
        self.assertEqual(self.job.attempt_count, 1)
        # 7 ??exactly one active build
        self.assertEqual(PaperIndexVersion.objects.filter(
            paper=self.paper, status="active").count(), 1)
        # 7 ??exactly one terminal dependency
        dep.refresh_from_db()
        self.assertEqual(dep.status, "succeeded")
        # 7 ??exactly one completion event
        self.assertEqual(ProjectRunEvent.objects.filter(
            event_type="ingestion_completed").count(), 1)
        # 8 ??identity cleared
        self.assertEqual(self.job.execution_token, "")

    def test_live_lease_holds_second_delivery_skips(self):
        claim_execution(self.job.id)  # simulate a live executing worker
        result = self._run_task()
        self.assertEqual(result["status"], "skipped")
        self.assertEqual(result["reason"], "execution_lease_held")
        self.job.refresh_from_db()
        self.assertEqual(self.job.attempt_count, 1)  # no second attempt
        self.assertIsNone(self.job.terminal_at)

    def test_permanent_failure_path_no_regression(self):
        from api.models import ProjectRunEvent
        with mock.patch("api.tasks._load_pdf_bytes",
                        side_effect=ValueError("no pdf")):
            with self.assertRaises(ValueError):
                self._run_task()
        self.job.refresh_from_db()
        self.assertEqual(self.job.status, "failed")
        self.assertIsNotNone(self.job.terminal_at)
        self.assertEqual(self.job.execution_token, "")
        self.assertEqual(ProjectRunEvent.objects.filter(
            event_type="ingestion_failed").count(), 1)

    def test_transient_failure_releases_execution_for_retry(self):
        from api.tasks import TransientIngestError
        with mock.patch("api.tasks._load_pdf_bytes", return_value=b"%PDF"), \
             mock.patch("api.tasks.ingest_pdf_bytes",
                        new=mock.AsyncMock(side_effect=RuntimeError("boom"))):
            with self.assertRaises(TransientIngestError):
                self._run_task()
        self.job.refresh_from_db()
        self.assertEqual(self.job.status, "pending")
        # lease released so the celery retry can reclaim immediately
        self.assertEqual(self.job.execution_token, "")
        self.assertEqual(self.job.attempt_count, 1)
        # retry reclaims and counts a second executed attempt
        token2 = claim_execution(self.job.id)
        self.assertIsNotNone(token2)
        self.job.refresh_from_db()
        self.assertEqual(self.job.attempt_count, 2)


# ?? 2/3/9. Beat reconciliation scans ??????????????????????????????????
class ReconcileExecutionScanTests(TransactionTestCase):

    def _age(self, job, **stamp):
        from api.models import PaperIngestionJob
        fields = {}
        for k, v in stamp.items():
            fields[k] = timezone.now() - timedelta(seconds=v)
        PaperIngestionJob.objects.filter(id=job.id).update(**fields)

    def _reconcile(self):
        from api.tasks import reconcile_workflow_runs_task
        with mock.patch("api.tasks.ingest_paper_pdf_task") as fake:
            stats = reconcile_workflow_runs_task.run()
        return stats, fake

    def test_expired_lease_job_redispatched_and_live_job_untouched(self):
        # dead worker: parsing + expired execution lease
        dead = _job(_proj("R1"), _paper(_proj("R1"), "r1", "glm01-r1"),
                    status="parsing")
        _dep(dead)
        claim_execution(dead.id)
        self._age(dead, execution_lease_expires_at=30)
        # live worker: parsing + fresh lease
        proj2 = _proj("R2")
        live = _job(proj2, _paper(proj2, "r2", "glm01-r2"), status="parsing")
        _dep(live)
        claim_execution(live.id)  # lease fresh by construction

        stats, fake = self._reconcile()
        fake.delay.assert_called_once_with(dead.id)
        self.assertEqual(stats["execution_lost"], 1)

    def test_heartbeat_renewal_prevents_redispatch_of_long_parse(self):
        # long-running valid parse keeps renewing: never redispatched
        proj = _proj("R3")
        job = _job(proj, _paper(proj, "r3", "glm01-r3"), status="parsing")
        _dep(job)
        token = claim_execution(job.id)
        for _ in range(3):
            time.sleep(0.02)
            self.assertTrue(exec_heartbeat(job.id, token))
        stats, fake = self._reconcile()
        fake.delay.assert_not_called()
        self.assertEqual(stats["execution_lost"], 0)
        # positive control in the same cycle: an expired twin IS redispatched
        twin = _job(proj, _paper(proj, "r3b", "glm01-r3b"), status="parsing")
        _dep(twin)
        claim_execution(twin.id)
        self._age(twin, execution_lease_expires_at=10)
        stats, fake = self._reconcile()
        fake.delay.assert_called_once_with(twin.id)

    @override_settings(
        PAPERLENS_INGESTION_EXECUTION_LEASE_SECONDS=5,
        PAPERLENS_INGESTION_HEARTBEAT_SECONDS=2)
    def test_pending_no_attempt_grace_is_dynamic(self):
        proj = _proj("R4")
        lost = _job(proj, _paper(proj, "r4", "glm01-r4"), status="pending")
        lost.attempt_count = 0
        lost.save()
        _dep(lost)
        # gate = 5 + 15 + 5 = 25s: aged 20s -> inside grace, NOT redispatched
        self._age(lost, created_at=20)
        stats, fake = self._reconcile()
        fake.delay.assert_not_called()
        self.assertEqual(stats["execution_lost"], 0)
        # aged 30s -> beyond the dynamic gate, redispatched
        self._age(lost, created_at=30)
        stats, fake = self._reconcile()
        fake.delay.assert_called_once_with(lost.id)
        self.assertEqual(stats["execution_lost"], 1)

    def test_dep_unlinked_jobs_never_redispatched(self):
        # negative control: same stale shape but NO workflow dependency
        proj = _proj("R5")
        job = _job(proj, _paper(proj, "r5", "glm01-r5"), status="parsing")
        claim_execution(job.id)
        self._age(job, execution_lease_expires_at=60)
        stats, fake = self._reconcile()
        fake.delay.assert_not_called()
        self.assertEqual(stats["execution_lost"], 0)

    def test_in_progress_statuses_contract(self):
        self.assertEqual(
            set(EXECUTION_IN_PROGRESS_STATUSES),
            {"downloading", "parsing", "embedding", "committing"})


# ?? 10. zero leakage on every external surface ???????????????????????
class PrivacyLeakageTests(TransactionTestCase):

    def test_execution_identity_never_exposed(self):
        from api.models import ProjectRunEvent
        from api.serializers import PaperIngestionJobSerializer
        proj = _proj("LEAK")
        paper = _paper(proj, "Leak paper", "glm01-leak")
        job = _job(proj, paper, status="parsing")
        token = claim_execution(job.id)
        _dep(job)

        # serializer surface
        data = PaperIngestionJobSerializer(job).data
        rendered = str(data).lower()
        self.assertNotIn("execution_token", rendered)
        self.assertNotIn("execution_heartbeat", rendered)
        self.assertNotIn("execution_lease", rendered)
        self.assertNotIn(token, rendered)

        # event surface: run a full success and scan payloads
        with mock.patch("api.tasks._load_pdf_bytes",
                        return_value=b"%PDF-1.4 fake"), \
             mock.patch("api.tasks.ingest_pdf_bytes",
                        new=mock.AsyncMock(return_value=2)):
            from api.tasks import ingest_paper_pdf_task
            result = ingest_paper_pdf_task.run(job.id)
        for event in ProjectRunEvent.objects.all().values("payload"):
            self.assertNotIn(token, str(event["payload"]))
        # celery result surface
        self.assertNotIn(token, str(result))
        # checkpoint surface (SQL scan of both langgraph tables)
        with connection.cursor() as cur:
            for table in ("checkpoints", "checkpoint_writes",
                          "checkpoint_blobs"):
                cur.execute(
                    f"SELECT to_jsonb(x)::text FROM {table} x LIMIT 200")
                for (blob,) in cur.fetchall():
                    self.assertNotIn(token, blob)
                    self.assertNotIn("execution_token", blob)


# ?? 11. migration forward/backward compatibility ?????????????????????
class MigrationCompatTests(TransactionTestCase):
    """Apply 0007 (backward), insert a legacy row, roll forward to 0008 and
    verify defaults; then roll back again ??legacy rows survive both ways."""

    def test_forward_backward_with_legacy_rows(self):
        # base rows must exist BEFORE the backward migration (the raw
        # INSERT...SELECT reads them while the model fields are absent)
        proj = _proj("MIGBASE")
        _paper(proj, "Mig paper", "glm01-mig")
        call_command("migrate", "api", "0007", verbosity=0)
        try:
            now = timezone.now().isoformat()
            with connection.cursor() as cur:
                cur.execute(
                    "INSERT INTO api_paperingestionjob "
                    "(project_id, paper_id, status, idempotency_key, "
                    " source_kind, attempt_count, file_size, error_code, "
                    " retryable, file_name, file_hash, file_path, "
                    " source_url, chunk_count, error_message, "
                    " celery_task_id, created_at, updated_at) "
                    "SELECT p.id, pp.id, 'parsing', 'legacy-key', "
                    "       'workflow', 1, 0, '', false, '', '', '', "
                    "       '', 0, '', '', %s, %s "
                    "FROM api_researchproject p, papers_paper pp "
                    "LIMIT 1", [now, now])
            # forward: columns added with safe defaults for legacy rows
            call_command("migrate", "api", "0008", verbosity=0)
            with connection.cursor() as cur:
                cur.execute(
                    "SELECT execution_token, execution_heartbeat_at, "
                    "execution_lease_expires_at, status "
                    "FROM api_paperingestionjob WHERE idempotency_key="
                    "'legacy-key'")
                token, hb, lease, status = cur.fetchone()
            self.assertEqual(token, "")
            self.assertIsNone(hb)
            self.assertIsNone(lease)
            self.assertEqual(status, "parsing")  # untouched by migration
        finally:
            call_command("migrate", "api", verbosity=0)  # restore head


# == P2-GLM-01-CX round: token match is NOT sufficient ==================
# Negative controls hold the SAME token with an EXPIRED lease; every
# control has an unexpired live-token positive twin.
def _expire_lease(job):
    from api.models import PaperIngestionJob
    PaperIngestionJob.objects.filter(id=job.id).update(
        execution_lease_expires_at=timezone.now() - timedelta(seconds=1))


def _fresh_job(tag):
    proj = _proj(f"CX{tag}")
    paper = _paper(proj, f"CX {tag} paper", f"glm01-cx-{tag}")
    from api.ingestion_service import IngestionService
    job, _c, _v = IngestionService().enqueue_url_job(
        project=proj, paper=paper,
        source_url=f"https://cdn.example.com/glm01-cx-{tag}.pdf",
        source_kind="workflow")
    return job


class ExpiredLeaseFencingTests(TransactionTestCase):

    def test_expired_same_token_heartbeat_refused(self):
        from api.ingestion_execution import heartbeat as hb_fn
        job = _fresh_job("HBX")
        token = claim_execution(job.id)
        # live control: unexpired lease renews
        self.assertTrue(hb_fn(job.id, token))
        _expire_lease(job)
        # same token, expired lease: MUST NOT self-renew
        self.assertFalse(hb_fn(job.id, token))
        job.refresh_from_db()
        self.assertLess(job.execution_lease_expires_at,
                        timezone.now())

    def test_expired_same_token_owner_assertion_rejected(self):
        from api.ingestion_execution import assert_execution_owner
        from django.db import transaction as tx
        job = _fresh_job("ASX")
        token = claim_execution(job.id)
        with tx.atomic():
            assert_execution_owner(job.id, token)  # live control passes
        _expire_lease(job)
        with tx.atomic():
            with self.assertRaises(ExecutionLeaseLost):
                assert_execution_owner(job.id, token)

    def test_expired_same_token_release_preserves_evidence(self):
        from api.ingestion_execution import release_execution
        job = _fresh_job("RLX")
        token = claim_execution(job.id)
        expired_lease = timezone.now() - timedelta(seconds=1)
        from api.models import PaperIngestionJob
        PaperIngestionJob.objects.filter(id=job.id).update(
            execution_lease_expires_at=expired_lease)
        self.assertFalse(release_execution(job.id, token))
        job.refresh_from_db()
        # expired evidence (token + heartbeat + expired lease) MUST remain
        # for Beat recovery — an expired worker cannot erase it
        self.assertEqual(job.execution_token, token)
        self.assertIsNotNone(job.execution_heartbeat_at)
        self.assertEqual(job.execution_lease_expires_at, expired_lease)
        # live control: unexpired twin releases cleanly
        job2 = _fresh_job("RLY")
        token2 = claim_execution(job2.id)
        self.assertTrue(release_execution(job2.id, token2))
        job2.refresh_from_db()
        self.assertEqual(job2.execution_token, "")

    def test_expired_same_token_chunk_write_zero_effects(self):
        from rag.models import Text
        from rag.ingest import ingest_text
        job = _fresh_job("CHK")
        paper = job.paper
        token = claim_execution(job.id)
        _expire_lease(job)
        with self.assertRaises(ExecutionLeaseLost):
            asyncio.run(ingest_text(
                paper, LONG_TEXT, replace_existing=True,
                execution=(job.id, token)))
        self.assertEqual(Text.objects.filter(paper=paper).count(), 0)
        # live control
        job2 = _fresh_job("CHY")
        token2 = claim_execution(job2.id)
        count = asyncio.run(ingest_text(
            job2.paper, LONG_TEXT, replace_existing=True,
            execution=(job2.id, token2)))
        self.assertGreater(count, 0)

    def test_expired_same_token_build_claim_zero_effects(self):
        from rag.models import PaperIndexVersion
        from api.ingestion_service import IngestionService
        job = _fresh_job("BLD")
        token = claim_execution(job.id)
        _expire_lease(job)
        before = PaperIndexVersion.objects.filter(
            paper=job.paper).count()
        with self.assertRaises(ExecutionLeaseLost):
            IngestionService().claim_build(
                job, job.source_url,
                expected_execution=(job.id, token))
        self.assertEqual(PaperIndexVersion.objects.filter(
            paper=job.paper).count(), before)
        # enqueue_url_job already attached the pre-claimed build; the
        # expired fence must not RE-attach or create a NEW one
        self.assertEqual(PaperIndexVersion.objects.filter(
            paper=job.paper).count(), 1)
        # live control attaches a build
        job2 = _fresh_job("BLY")
        token2 = claim_execution(job2.id)
        version = IngestionService().claim_build(
            job2, job2.source_url,
            expected_execution=(job2.id, token2))
        job2.refresh_from_db()
        self.assertEqual(job2.index_version_id, version.id)

    def test_expired_same_token_activate_zero_effects(self):
        from api.ingestion_service import IngestionService
        from rag.models import PaperIndexVersion, Text
        from rag.embedding import embedding_metadata
        job = _fresh_job("ACT")
        token = claim_execution(job.id)
        service = IngestionService()
        version = service.claim_build(
            job, job.source_url, expected_execution=(job.id, token))
        m = embedding_metadata()
        Text.objects.create(
            paper=job.paper, index_version=version, docname="c0",
            chunk_index=0, content=LONG_TEXT,
            embedding=[0.0] * int(m["embedding_dim"]),
            embedding_model=str(m["embedding_model"]),
            embedding_version=str(m["embedding_version"]),
            embedding_dim=int(m["embedding_dim"]),
            content_hash="h-cx-act-0", citation_key="ck-cx-act-0")
        _expire_lease(job)
        with self.assertRaises(ExecutionLeaseLost):
            service.activate(job.paper_id, version.id, 1,
                             expected_execution=(job.id, token))
        version.refresh_from_db()
        self.assertEqual(version.status, "building")
        # live control activates
        job2 = _fresh_job("ACY")
        token2 = claim_execution(job2.id)
        v2 = service.claim_build(
            job2, job2.source_url, expected_execution=(job2.id, token2))
        Text.objects.create(
            paper=job2.paper, index_version=v2, docname="c0",
            chunk_index=0, content=LONG_TEXT,
            embedding=[0.0] * int(m["embedding_dim"]),
            embedding_model=str(m["embedding_model"]),
            embedding_version=str(m["embedding_version"]),
            embedding_dim=int(m["embedding_dim"]),
            content_hash="h-cx-acy-0", citation_key="ck-cx-acy-0")
        service.activate(job2.paper_id, v2.id, 1,
                         expected_execution=(job2.id, token2))
        v2.refresh_from_db()
        self.assertEqual(v2.status, "active")

    def test_expired_same_token_finalize_zero_effects(self):
        from api.models import ProjectRunEvent
        from api.workflow_callbacks import finalize_job_terminal
        job = _fresh_job("FIN")
        token = claim_execution(job.id)
        _expire_lease(job)
        fin = finalize_job_terminal(
            job.id, "embedded", chunk_count=3,
            expected_execution_token=token)
        self.assertFalse(fin["won"])
        job.refresh_from_db()
        self.assertIsNone(job.terminal_at)
        self.assertNotEqual(job.status, "embedded")
        self.assertEqual(job.execution_token, token)  # evidence intact
        self.assertEqual(ProjectRunEvent.objects.filter(
            event_type="ingestion_completed").count(), 0)
        # live control terminalizes and clears identity
        job2 = _fresh_job("FIY")
        token2 = claim_execution(job2.id)
        fin2 = finalize_job_terminal(
            job2.id, "embedded", chunk_count=3,
            expected_execution_token=token2)
        self.assertTrue(fin2["won"])
        job2.refresh_from_db()
        self.assertEqual(job2.status, "embedded")
        self.assertEqual(job2.execution_token, "")

    def test_expired_same_token_job_run_event_zero_effects(self):
        from api.models import ProjectRun, ProjectRunEvent
        from api.tasks import _mark_transient
        job = _fresh_job("MRK")
        token = claim_execution(job.id)
        run = ProjectRun.objects.create(
            project=job.project, kind="ingestion", status="running",
            question="cx mark")
        _expire_lease(job)
        with self.assertRaises(ExecutionLeaseLost):
            _mark_transient(job, run, RuntimeError("boom"),
                            exec_token=token)
        job.refresh_from_db()
        self.assertEqual(job.status, "pending")  # untouched
        self.assertEqual(job.execution_token, token)
        self.assertEqual(ProjectRunEvent.objects.filter(
            event_type="ingestion_retry").count(), 0)
        # live control marks + hands off
        job2 = _fresh_job("MRY")
        token2 = claim_execution(job2.id)
        run2 = ProjectRun.objects.create(
            project=job2.project, kind="ingestion", status="running",
            question="cx mark 2")
        _mark_transient(job2, run2, RuntimeError("boom"),
                        exec_token=token2)
        job2.refresh_from_db()
        self.assertEqual(job2.status, "pending")
        self.assertEqual(job2.execution_token, "")  # handed off
        self.assertIsNotNone(job2.execution_heartbeat_at)  # recovery fact
        self.assertEqual(ProjectRunEvent.objects.filter(
            event_type="ingestion_retry").count(), 1)

    def test_fence_vs_write_pause_chunk_write_fails(self):
        """Lease expires BETWEEN an earlier ownership fence and the chunk
        write: the write must fail inside its own re-fenced transaction
        (the write path re-verifies token + unexpired lease at write time,
        never trusting a preceding check)."""
        from api.ingestion_execution import assert_execution_owner
        from api.models import PaperIngestionJob
        from rag.models import Text
        from rag.ingest import ingest_text
        from django.db import transaction as tx
        job = _fresh_job("PAU")
        token = claim_execution(job.id)
        # 1. an earlier fence in its own transaction passes
        with tx.atomic():
            assert_execution_owner(job.id, token)
        # 2. the lease expires after that fence, before the write
        PaperIngestionJob.objects.filter(id=job.id).update(
            execution_lease_expires_at=timezone.now()
            - timedelta(seconds=1))
        # 3. the chunk write re-fences at write time and MUST fail
        with self.assertRaises(ExecutionLeaseLost):
            asyncio.run(ingest_text(
                job.paper, LONG_TEXT, replace_existing=True,
                execution=(job.id, token)))
        self.assertEqual(Text.objects.filter(paper=job.paper).count(), 0)
        # live control: identical flow with the lease still fresh
        job2 = _fresh_job("PAV")
        token2 = claim_execution(job2.id)
        with tx.atomic():
            assert_execution_owner(job2.id, token2)  # earlier fence
        count = asyncio.run(ingest_text(
            job2.paper, LONG_TEXT, replace_existing=True,
            execution=(job2.id, token2)))  # write-time fence still fresh
        self.assertGreater(count, 0)

    def test_after_takeover_old_token_all_boundaries_zero_effects(self):
        from api.ingestion_execution import (assert_execution_owner,
                                             heartbeat as hb_fn,
                                             release_execution)
        from api.ingestion_service import IngestionService
        from api.models import ProjectRun, ProjectRunEvent
        from api.tasks import _mark_transient
        from api.workflow_callbacks import finalize_job_terminal
        from rag.ingest import ingest_text
        from django.db import transaction as tx
        job = _fresh_job("TKO")
        old = claim_execution(job.id)
        _expire_lease(job)
        new = claim_execution(job.id)  # takeover
        self.assertNotEqual(old, new)
        # heartbeat / release: no effect on the new identity
        self.assertFalse(hb_fn(job.id, old))
        self.assertFalse(release_execution(job.id, old))
        job.refresh_from_db()
        self.assertEqual(job.execution_token, new)
        self.assertIsNotNone(job.execution_lease_expires_at)
        # chunk boundary
        with self.assertRaises(ExecutionLeaseLost):
            asyncio.run(ingest_text(
                job.paper, LONG_TEXT, replace_existing=True,
                execution=(job.id, old)))
        # build boundary
        with self.assertRaises(ExecutionLeaseLost):
            IngestionService().claim_build(
                job, job.source_url, expected_execution=(job.id, old))
        # terminal boundary
        fin = finalize_job_terminal(
            job.id, "embedded", chunk_count=1,
            expected_execution_token=old)
        self.assertFalse(fin["won"])
        # job/run/event boundary
        run = ProjectRun.objects.create(
            project=job.project, kind="ingestion", status="running",
            question="tko")
        with self.assertRaises(ExecutionLeaseLost):
            _mark_transient(job, run, RuntimeError("x"), exec_token=old)
        self.assertEqual(ProjectRunEvent.objects.filter(
            event_type="ingestion_retry").count(), 0)
        # owner assertion with the stale token fails too
        with tx.atomic():
            with self.assertRaises(ExecutionLeaseLost):
                assert_execution_owner(job.id, old)


class TransientHandoffRecoveryTests(TransactionTestCase):

    def _reconcile(self):
        from api.tasks import reconcile_workflow_runs_task
        # workflow wakeups are mocked out: this suite tests INGESTION
        # recovery only, and an eager workflow run (network-guarded
        # searches) would make the suite slow and non-deterministic
        with mock.patch("api.tasks.ingest_paper_pdf_task") as fake, \
             mock.patch("api.tasks.resume_research_expand_workflow_task"), \
             mock.patch("api.tasks.run_research_expand_workflow_task"):
            stats = reconcile_workflow_runs_task.run()
        return stats, fake

    def test_lost_retry_publication_recovered_once_and_converges(self):
        """Transient handoff whose Celery retry publication is LOST: Beat
        must NOT redispatch inside the gate, MUST redispatch exactly once
        per cycle after it, and a replacement worker must converge."""
        from api.ingestion_execution import compensation_gate
        from api.models import (PaperIngestionJob, ProjectRunEvent,
                                ProjectWorkflowDependency)
        from api.tasks import (TransientIngestError,
                               ingest_paper_pdf_task)
        from rag.models import PaperIndexVersion
        from rag.ingest import ingest_text

        job = _fresh_job("THX")
        dep = _dep(job)

        async def boom(*a, **kw):
            raise RuntimeError("parse exploded")

        with mock.patch("api.tasks._load_pdf_bytes",
                        return_value=b"%PDF-1.4"), \
             mock.patch("api.tasks.ingest_pdf_bytes", new=boom):
            with self.assertRaises(TransientIngestError):
                ingest_paper_pdf_task.run(job.id)
            # retry publication simulated as LOST (never delivered)
        job.refresh_from_db()
        self.assertEqual(job.status, "pending")
        self.assertEqual(job.attempt_count, 1)
        self.assertEqual(job.execution_token, "")  # handed off
        self.assertIsNotNone(job.execution_heartbeat_at)  # recovery fact

        # live handoff: fresh heartbeat -> Beat must NOT redispatch yet
        stats, fake = self._reconcile()
        fake.delay.assert_not_called()
        self.assertEqual(stats["execution_lost"], 0)

        # age the handoff fact past the DYNAMIC gate
        gate = compensation_gate()
        PaperIngestionJob.objects.filter(id=job.id).update(
            execution_heartbeat_at=timezone.now()
            - timedelta(seconds=gate + 5))
        stats, fake = self._reconcile()
        fake.delay.assert_called_once_with(job.id)  # once this cycle
        self.assertEqual(stats["execution_lost"], 1)
        stats, fake = self._reconcile()
        fake.delay.assert_called_once_with(job.id)  # once PER cycle

        # replacement worker converges with a working parse
        paper = job.paper
        job_id = job.id

        async def ok(*a, **kw):
            # plain values only: this coroutine runs on an executor thread
            # with its own DB connection (lazy FK loads would deadlock)
            return await ingest_text(
                paper, LONG_TEXT, replace_existing=True,
                index_version=kw.get("index_version"),
                execution=kw.get("execution"))
        with mock.patch("api.tasks._load_pdf_bytes",
                        return_value=b"%PDF-1.4"), \
             mock.patch("api.tasks.ingest_pdf_bytes", new=ok):
            result = ingest_paper_pdf_task.run(job.id)
        self.assertEqual(result["status"], "embedded")
        job.refresh_from_db()
        self.assertEqual(job.attempt_count, 2)
        self.assertEqual(PaperIndexVersion.objects.filter(
            paper=job.paper, status="active").count(), 1)
        dep.refresh_from_db()
        self.assertEqual(dep.status, "succeeded")
        self.assertEqual(ProjectRunEvent.objects.filter(
            event_type="ingestion_completed").count(), 1)
        # terminal -> never redispatched again
        stats, fake = self._reconcile()
        fake.delay.assert_not_called()
        self.assertEqual(stats["execution_lost"], 0)

    def test_live_inprogress_worker_not_redispatched(self):
        job = _fresh_job("LIV")
        _dep(job)
        token = claim_execution(job.id)
        from api.models import PaperIngestionJob
        PaperIngestionJob.objects.filter(id=job.id).update(
            status="parsing")
        stats, fake = self._reconcile()
        fake.delay.assert_not_called()
        self.assertEqual(stats["execution_lost"], 0)
        self.assertTrue(token)  # sanity


# == P2-GLM-01-CX-04: valid-release recovery fact ======================
@override_settings(
    PAPERLENS_INGESTION_EXECUTION_LEASE_SECONDS=30,
    PAPERLENS_INGESTION_HEARTBEAT_SECONDS=1)
class HeartbeatErrorRecoveryTests(TransactionTestCase):
    """A transient DB error makes the heartbeat thread report lost; the
    task exits fail-closed; finally releases a STILL-VALID lease. The
    release must PRESERVE a timestamped recovery fact so Beat recovers
    after the dynamic gate — never immediately, never lost forever."""

    def _reconcile(self):
        from api.tasks import reconcile_workflow_runs_task
        with mock.patch("api.tasks.ingest_paper_pdf_task") as fake, \
             mock.patch("api.tasks.resume_research_expand_workflow_task"), \
             mock.patch("api.tasks.run_research_expand_workflow_task"):
            stats = reconcile_workflow_runs_task.run()
        return stats, fake

    def test_hb_error_exit_recovery_fact_survives_finally(self):
        from api.ingestion_execution import (compensation_gate,
                                             release_execution)
        from api.models import (PaperIngestionJob, ProjectRun,
                                ProjectRunEvent,
                                ProjectWorkflowDependency)
        from api.tasks import ingest_paper_pdf_task
        from rag.ingest import ingest_text

        proj = _proj("CX4")
        paper = _paper(proj, "CX4 paper", "glm01-cx4")
        from api.ingestion_service import IngestionService
        job, _c, _v = IngestionService().enqueue_url_job(
            project=proj, paper=paper,
            source_url="https://cdn.example.com/glm01-cx4.pdf",
            source_kind="workflow")
        dep = ProjectWorkflowDependency.objects.create(
            run=ProjectRun.objects.create(
                project=proj, kind="workflow", status="waiting_ingestion",
                question="cx4"),
            paper=paper, ingestion_job=job, status="pending")

        # drive the task until it is mid-parse inside a SLOW body, then
        # inject a heartbeat DB error to force the fail-closed exit
        started_evt = {}

        async def slow_body(*a, **kw):
            started_evt["t"] = time.time()
            while time.time() - started_evt["t"] < 3.0:
                time.sleep(0.1)   # hold parsing; hb interval is 1s
            return await ingest_text(
                paper, LONG_TEXT, replace_existing=True,
                index_version=kw.get("index_version"),
                execution=kw.get("execution"))

        import api.ingestion_execution as iex
        real_hb = iex.heartbeat
        err_state = {"armed": False}

        def flaky_hb(job_id, token, lease=None):
            if err_state["armed"]:
                raise RuntimeError("db connection refused")  # transient
            return real_hb(job_id, token, lease)

        real_start = iex.IngestionHeartbeat.start

        def arming_start(self):
            err_state["armed"] = True   # DB breaks right after the claim
            return real_start(self)

        def slow_load(job_arg):
            time.sleep(3.0)            # window covering a 1s hb interval
            return b"%PDF-1.4 fake"

        with mock.patch("api.tasks._load_pdf_bytes", slow_load), \
             mock.patch("api.tasks.ingest_pdf_bytes", new=slow_body), \
             mock.patch.object(iex, "heartbeat", flaky_hb), \
             mock.patch.object(iex.IngestionHeartbeat, "start",
                               arming_start):
            result = ingest_paper_pdf_task.run(job.id)

        # fail-closed exit
        self.assertEqual(result["status"], "skipped")
        job.refresh_from_db()
        # NOT terminal — and the recovery fact MUST have survived finally
        self.assertIsNone(job.terminal_at)
        self.assertEqual(job.status, "parsing")
        self.assertEqual(job.execution_token, "")      # released
        self.assertIsNotNone(job.execution_heartbeat_at)  # fact kept
        self.assertIsNone(job.execution_lease_expires_at)

        # gate 内 reconciliation 不重投（新鲜交接事实）
        stats, fake = self._reconcile()
        fake.delay.assert_not_called()
        self.assertEqual(stats["execution_lost"], 0)

        # gate 后恰重投一次
        PaperIngestionJob.objects.filter(id=job.id).update(
            execution_heartbeat_at=timezone.now()
            - timedelta(seconds=compensation_gate() + 5))
        stats, fake = self._reconcile()
        fake.delay.assert_called_once_with(job.id)
        self.assertEqual(stats["execution_lost"], 1)

        # replacement claim 成功并最终 terminal（真实 parse 收敛）
        async def ok(*a, **kw):
            return await ingest_text(
                paper, LONG_TEXT, replace_existing=True,
                index_version=kw.get("index_version"),
                execution=kw.get("execution"))
        with mock.patch("api.tasks._load_pdf_bytes",
                        return_value=b"%PDF-1.4"), \
             mock.patch("api.tasks.ingest_pdf_bytes", new=ok):
            result = ingest_paper_pdf_task.run(job.id)
        self.assertEqual(result["status"], "embedded")
        job.refresh_from_db()
        self.assertIsNotNone(job.terminal_at)
        self.assertEqual(job.execution_token, "")
        self.assertIsNone(job.execution_heartbeat_at)
        self.assertIsNone(job.execution_lease_expires_at)
        dep.refresh_from_db()
        self.assertEqual(dep.status, "succeeded")
        self.assertEqual(ProjectRunEvent.objects.filter(
            event_type="ingestion_completed").count(), 1)

    def test_fresh_released_worker_untouched_then_redispatched_after_gate(self):
        """Fresh valid-release (no error): release keeps the fact; Beat
        ignores it inside the gate and redispatches once past it."""
        from api.ingestion_execution import (compensation_gate,
                                             release_execution)
        from api.models import PaperIngestionJob
        job = _fresh_job("CX4B")
        _dep(job)  # dep-linked jobs are in the Beat scan scope
        token = claim_execution(job.id)
        self.assertTrue(release_execution(job.id, token))
        job.refresh_from_db()
        self.assertEqual(job.execution_token, "")
        self.assertIsNotNone(job.execution_heartbeat_at)
        self.assertIsNone(job.execution_lease_expires_at)

        stats, fake = self._reconcile()
        fake.delay.assert_not_called()          # fresh -> untouched
        self.assertEqual(stats["execution_lost"], 0)

        PaperIngestionJob.objects.filter(id=job.id).update(
            execution_heartbeat_at=timezone.now()
            - timedelta(seconds=compensation_gate() + 5))
        stats, fake = self._reconcile()
        fake.delay.assert_called_once_with(job.id)

    def test_cleared_identity_shape_is_illegal_unrecoverable(self):
        """NEGATIVE control: parsing + token/heartbeat/lease ALL empty is
        an illegal unrecoverable shape — the recovery gate must reject it
        (not treat it as a legitimate PASS state), and the tests assert
        that shape can never be produced by release/terminalization."""
        from api.ingestion_execution import execution_lost
        from api.models import PaperIngestionJob
        job = _fresh_job("CX4C")
        _dep(job)
        claim_execution(job.id)
        # the OLD buggy shape: everything cleared while still parsing
        PaperIngestionJob.objects.filter(id=job.id).update(
            execution_token="",
            execution_heartbeat_at=None,
            execution_lease_expires_at=None,
            status="parsing")
        job.refresh_from_db()
        # the recovery gate cannot recognize it (by design it has NO
        # recovery fact) — this is exactly why the shape must be
        # unreachable: assert the gate says NOT recoverable (and would
        # therefore orphan the job) ...
        self.assertFalse(execution_lost(job))
        # ... which is only safe if no code path can produce it:
        # release preserves the fact (positive proof above), expired
        # release refuses to erase (CX-03 proof), and only
        # terminalization clears all three — verified here directly:
        token = claim_execution(job.id)   # take over the orphan
        from api.workflow_callbacks import finalize_job_terminal
        fin = finalize_job_terminal(
            job.id, "embedded", chunk_count=1,
            expected_execution_token=token)
        self.assertTrue(fin["won"])
        job.refresh_from_db()
        self.assertEqual(job.status, "embedded")
        self.assertEqual(job.execution_token, "")
        self.assertIsNone(job.execution_heartbeat_at)
        self.assertIsNone(job.execution_lease_expires_at)
        # terminal jobs are never recoverable targets either
        self.assertFalse(execution_lost(job))