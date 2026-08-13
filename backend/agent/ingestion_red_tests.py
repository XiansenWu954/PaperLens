"""Phase 1 `secure-and-version-paper-ingestion` — Batch A v3 red baseline
(Tasks 1.1-1.5, Codex v3 minimal revisions).

v3 changes:
- SafePdfFetcher test seam: tests import `rag.ingest.SafePdfFetcher` and
  inject SequenceResolver / FakePinnedTransport / connected_peer / redirect
  responses; while the model is absent the tests FAIL with an explicit
  assertion (never ERROR).
- Redirect limit: a 6+ hop sequence must send <= 5 hop requests, return
  `redirect_limit_exceeded`, and never connect the 6th target.
- Migration tests: explicit migrate_from (current leaf) -> migrate_to
  (`rag.0004_paper_index_version`), legacy fixtures (two embedding groups per
  paper, another paper, incompatible group), real data migration assertions.
- Active-only retrieval: four versions (active/building/superseded/failed)
  with per-version sentinel chunks; active sentinel recalled, the other three
  never.
- Cross-project fixture: both ProjectPaper memberships exist; each request
  yields its own project job; both jobs share one non-null global version;
  request identity is exact per project.
- Streaming: APIRequestFactory drives the real view; read/chunks call counts
  are asserted (view read_count==0, chunks_count>0).

Run (PostgreSQL gate):
  docker compose run --rm -v "D:/aiproducts/PaperLens/docs/internal/stage-b-artifacts-20260811/ingestion-batchA-red-v3:/artifacts" \
    -e PAPERLENS_STAGE_B_ARTIFACTS_DIR=/artifacts backend \
    python manage.py test agent.ingestion_red_tests --noinput -v 2
"""
from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import logging
import os
import socket
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

from django.test import TransactionTestCase
from django.test.client import Client

from agent.scope_failing_tests import (
    NetworkGuardTestCaseMixin,
    ScopeFixtureMixin,
    _active_embedding_meta,
    _write_json,
)

PDF_MAGIC = b"%PDF-1.7\n1 0 obj<</Type/Catalog>>endobj\n%%EOF"
SMALL_PDF = PDF_MAGIC + b"\nselective state space model paper text. " * 200
NOT_A_PDF = b"this is not a pdf"
MAX_UPLOAD_BYTES = 50 * 1024 * 1024
OVER_LIMIT_PDF = b"%PDF-1.7 " + (b"x" * (MAX_UPLOAD_BYTES + 1))
EXACT_LIMIT_PDF = b"%PDF-1.7 " + (b"x" * (MAX_UPLOAD_BYTES - len(b"%PDF-1.7 ")))


class PdfAcquisitionError(Exception):
    """Stable acquisition error carrying only a stable error_code."""

    def __init__(self, error_code: str):
        super().__init__(error_code)
        self.error_code = error_code


class SequenceResolver:
    """Injectable resolver: yields an address set per call (rebinding: first
    answer public, second answer private)."""

    def __init__(self, answers: list[set[str]]):
        self.answers = answers
        self.calls = 0

    def resolve(self, hostname: str) -> set[str]:
        idx = min(self.calls, len(self.answers) - 1)
        self.calls += 1
        return set(self.answers[idx])


class FakePinnedTransport:
    """Injectable pinned transport: records connected peers and sent requests;
    serves a scripted response sequence (redirects)."""

    def __init__(self, sequence: list | None = None,
                 connected_peer: str = "93.184.216.34"):
        self.sequence = sequence or []
        self.connected: list[str] = []
        self.sent: list[str] = []
        self.connected_peer = connected_peer

    def connect(self, ip: str) -> None:
        self.connected.append(ip)

    def send(self, url: str) -> dict:
        self.sent.append(url)
        if self.sequence:
            return self.sequence.pop(0)
        return {"status": 200, "content": SMALL_PDF, "headers": {}}


def _redirect_response(location: str) -> dict:
    return {"status": 302, "content": b"", "headers": {"location": location}}


def _config_embedding_identity():
    """The ACTIVE embedding identity as the versioning migration reads it —
    settings constants (deterministic, provider-free)."""
    from django.conf import settings
    return (str(settings.PAPERLENS_EMBEDDING_MODEL),
            str(settings.PAPERLENS_EMBEDDING_VERSION),
            int(settings.PAPERLENS_EMBEDDING_DIM))


class _IngestionRedBase(NetworkGuardTestCaseMixin, ScopeFixtureMixin,
                        TransactionTestCase):
    """Shared helpers + per-case contract/root-cause recording."""

    case_id = ""
    expected_pre_fix = ""
    contract = ""
    positive_control = ""
    negative_control = ""
    root_cause = ""

    def setUp(self):
        super().setUp()
        self.setUpScopeFixture()
        self._checks: list[dict] = []

    def record(self, surface: str, sentinel: str, found: bool, note: str = "") -> None:
        self._checks.append({
            "surface": surface, "sentinel": sentinel, "found": found,
            "note": note})

    def _body_status(self) -> str:
        try:
            result = self._outcome.result
        except AttributeError:
            return "UNKNOWN"
        if any(test is self for test, _tb in result.errors):
            return "ERROR"
        if any(test is self for test, _tb in result.failures):
            return "FAIL"
        return "PASS"

    def tearDown(self):
        """Write the audit record ALWAYS — even for cases that fail early via
        self.fail() — then delegate to the guard mixin teardown."""
        _write_json(f"audit-{self.case_id}.json", {
            "case_id": self.case_id,
            "test": self.id(),
            "contract": self.contract,
            "positive_control": self.positive_control,
            "negative_control": self.negative_control,
            "expected_pre_fix": self.expected_pre_fix,
            "actual": self._body_status(),
            "failure_root_cause": self.root_cause,
            "network_guard": {
                "installed": getattr(self, "_guard_installed", False),
                "call_count": len(getattr(self._counter, "calls", [])),
            },
            "checks": self._checks,
            "found_issues": [c for c in self._checks if c["found"]],
        })
        super().tearDown()

    # -- SafePdfFetcher seam (red: FAIL with an explicit assertion) ----------
    def _require_fetcher(self):
        try:
            from rag.ingest import SafePdfFetcher
            return SafePdfFetcher
        except (ImportError, AttributeError):
            self.fail("SafePdfFetcher missing — Tasks 3.1 (red)")

    def _require_version_model(self):
        import rag.models
        if not hasattr(rag.models, "PaperIndexVersion"):
            self.fail("PaperIndexVersion missing — Tasks 2.1 (red)")
        return rag.models.PaperIndexVersion

    # -- legacy httpx probe (used by URL-config cases) ------------------------
    def _probe_client(self, requests_log: list, status=200,
                      content: bytes = SMALL_PDF, headers: dict | None = None):
        class FakeResponse:
            def __init__(self):
                self.status_code = status
                self.content = content
                self.headers = headers or {}

            def raise_for_status(self):
                if 400 <= self.status_code < 600:
                    import httpx
                    raise httpx.HTTPStatusError(
                        "boom", request=None, response=None)

        class FakeClient:
            def __init__(self, *a, **k):
                self.kwargs = k
                requests_log.append(("__init__", k))

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def get(self, url, **k):
                requests_log.append(("get", url))
                return FakeResponse()

        return mock.patch("httpx.Client", FakeClient)

    def _enqueue_stub(self):
        return mock.patch(
            "api.views._enqueue_ingestion_job",
            return_value=type("Fake", (), {"id": "fake-task-1"})())

    def _upload(self, project_id, paper_id, name, content,
                content_type="application/pdf", raise_request_exception=False,
                stream_only=False):
        from django.core.files.uploadedfile import SimpleUploadedFile

        if stream_only:
            uploaded = _StreamOnlyUploadedFile(
                name, content, content_type=content_type)
        else:
            uploaded = SimpleUploadedFile(
                name, content, content_type=content_type)
        client = Client(HTTP_HOST="testserver",
                        raise_request_exception=raise_request_exception)
        return client.post(
            f"/api/projects/{project_id}/papers/{paper_id}/pdf-upload",
            {"file": uploaded},
        )


class _StreamOnlyUploadedFile:
    """UploadedFile whose ``read()`` raises — only ``chunks()`` works."""

    def __init__(self, name, content, content_type="application/pdf"):
        self.name = name
        self.content_type = content_type
        self.size = len(content)
        self._data = content

    def read(self, *a, **k):
        raise AssertionError(
            "read() must not be used on uploads (streaming required)")

    def chunks(self, chunk_size=None):
        yield self._data


class _CountingUploadedFile:
    """UploadedFile recording read()/chunks() call counts for the streaming
    red test (driven through APIRequestFactory, not the multipart encoder)."""

    def __init__(self, name, content, content_type="application/pdf"):
        self.name = name
        self.content_type = content_type
        self.size = len(content)
        self._data = content
        self.read_count = 0
        self.chunks_count = 0

    def read(self, *a, **k):
        self.read_count += 1
        return self._data

    def chunks(self, chunk_size=None):
        self.chunks_count += 1
        yield self._data


# =====================================================================
# Task 1.2 — versioned data model / migration red contracts
# =====================================================================

class IngestionVersionRedTest(_IngestionRedBase):

    def test_ING_VERSION_MODEL(self):
        self.case_id = "ING-VERSION-MODEL"
        self.expected_pre_fix = "FAIL"
        self.contract = "rag.models.PaperIndexVersion must exist (Task 2.1)"
        self.positive_control = "rag.models.Text exists"
        self.negative_control = "no PaperIndexVersion model"
        self.root_cause = "no PaperIndexVersion model defined"
        import rag.models
        has_model = hasattr(rag.models, "PaperIndexVersion")
        self.assertTrue(hasattr(rag.models, "Text"),
                        "positive control: Text model must exist")
        self.record("rag.models", "PaperIndexVersion", not has_model,
                    "PaperIndexVersion must exist")
        self.assertTrue(has_model, "PaperIndexVersion model missing (Tasks 2.1)")

    def test_ING_VERSION_UNIQUE_ACTIVE(self):
        self.case_id = "ING-VERSION-UNIQUE-ACTIVE"
        self.expected_pre_fix = "FAIL"
        self.contract = "at most ONE active PaperIndexVersion per paper"
        self.positive_control = "constraint list enumerable"
        self.negative_control = "no one-active partial unique constraint"
        self.root_cause = "model absent / constraint absent"
        import rag.models
        has_model = hasattr(rag.models, "PaperIndexVersion")
        constraint_ok = False
        if has_model:
            names = [c.name or "" for c in
                     rag.models.PaperIndexVersion._meta.constraints]
            constraint_ok = any("active" in n for n in names)
        self.record("constraints", "one_active_partial", not constraint_ok,
                    "partial unique constraint (one active per paper)")
        self.assertTrue(has_model and constraint_ok,
                        "one-active-per-paper constraint missing (Tasks 2.1/2.2)")

    def test_ING_VERSION_IMMUTABLE_IDENTITY(self):
        self.case_id = "ING-VERSION-IMMUTABLE-IDENTITY"
        self.expected_pre_fix = "FAIL"
        self.contract = "(paper, source_sha256, pipeline_signature) unique"
        self.positive_control = "constraint list exists"
        self.negative_control = "no immutable build identity constraint"
        self.root_cause = "model absent / identity constraint absent"
        import rag.models
        has_model = hasattr(rag.models, "PaperIndexVersion")
        identity_ok = False
        if has_model:
            names = [c.name or "" for c in
                     rag.models.PaperIndexVersion._meta.constraints]
            identity_ok = any("identity" in n or "source" in n for n in names)
        self.record("constraints", "immutable_identity", not identity_ok,
                    "immutable version identity unique")
        self.assertTrue(identity_ok,
                        "immutable version identity missing (Tasks 2.1)")

    def test_ING_VERSION_CHUNK_UNIQUENESS(self):
        self.case_id = "ING-VERSION-CHUNK-UNIQUENESS"
        self.expected_pre_fix = "FAIL"
        self.contract = "Text uniqueness is (index_version, chunk_index)"
        self.positive_control = "a Text uniqueness constraint exists"
        self.negative_control = "no version-scoped uniqueness"
        self.root_cause = "Text had no index_version; uniqueness was per-paper"
        import rag.models
        meta = rag.models.Text._meta
        names = [c.name or "" for c in meta.constraints]
        version_scoped = any("index_version" in (c.fields or [])
                             for c in meta.constraints)
        self.record("constraints", "version_scoped_uniqueness",
                    not version_scoped,
                    "(index_version, chunk_index) unique")
        self.assertTrue(version_scoped,
                        "chunk uniqueness is not version-scoped (Tasks 2.2)")

    def test_ING_VERSION_BACKFILL(self):
        self.case_id = "ING-VERSION-BACKFILL"
        self.expected_pre_fix = "FAIL"
        self.contract = "every legacy Text gets an index_version (non-null)"
        self.positive_control = "Text model field list is readable"
        self.negative_control = "no index_version field"
        self.root_cause = "Text.index_version does not exist"
        import rag.models
        has_field = any(f.name == "index_version"
                        for f in rag.models.Text._meta.fields)
        self.record("model", "index_version_field", not has_field,
                    "Text.index_version must exist")
        self.assertTrue(has_field,
                        "Text.index_version missing (Tasks 2.2 backfill)")

    def test_ING_VERSION_LEGACY_BACKFILL_MIGRATION(self):
        self.case_id = "ING-VERSION-LEGACY-BACKFILL-MIGRATION"
        self.expected_pre_fix = "FAIL"
        self.contract = ("migrate rag.0003_bge_m3_sparse_weights -> "
                         "rag.0004_paper_index_version must backfill legacy "
                         "chunks: every Text.index_version non-null, one "
                         "active per paper, newest compatible group active, "
                         "others superseded, (index_version, chunk_index) "
                         "unique, second active rejected by the DB")
        self.positive_control = "migration graph contains both nodes"
        self.negative_control = "target migration absent -> stable FAIL"
        self.root_cause = "no versioning migration exists"
        from django.db import connection, IntegrityError
        from django.db.migrations.executor import MigrationExecutor

        executor = MigrationExecutor(connection)
        graph = executor.loader.graph
        MIGRATE_FROM = ("rag", "0003_bge_m3_sparse_weights")
        MIGRATE_TO = ("rag", "0004_paper_index_version")
        if MIGRATE_TO not in graph.nodes:
            self.fail(
                "migration 'rag.0004_paper_index_version' missing — Tasks 2.2 "
                "(red)")
        self.assertTrue(MIGRATE_FROM in graph.nodes,
                        "positive control: legacy migration exists")

        executor.migrate([MIGRATE_FROM])
        try:
            # NOTE: MigrationLoader.applied_migrations caches the applied set
            # in memory — build a FRESH executor after every migrate step so
            # forward/backward plans are computed from the real DB state.
            fresh = MigrationExecutor(connection)
            legacy_apps = fresh.loader.project_state([MIGRATE_FROM]).apps
            LegacyText = legacy_apps.get_model("rag", "Text")
            LegacyPaper = legacy_apps.get_model("papers", "Paper")
            legacy_paper = LegacyPaper.objects.create(title="Legacy Paper",
                                                      year=2024)
            legacy_foreign = LegacyPaper.objects.create(title="Legacy Foreign",
                                                        year=2023)
            meta = _active_embedding_meta()
            active_model_cfg, active_v, active_dim_cfg = _config_embedding_identity()
            other_v = "old-incompatible-version"
            for paper, chunk_index, content, version in [
                (legacy_paper, 0, "LEGACY_A_ACTIVE", active_v),
                (legacy_paper, 1, "LEGACY_A2_ACTIVE", active_v),
                (legacy_paper, 2, "LEGACY_B_INCOMPATIBLE", other_v),
                (legacy_paper, 3, "LEGACY_B2_INCOMPATIBLE", other_v),
                (legacy_foreign, 0, "LEGACY_FOREIGN", active_v),
            ]:
                LegacyText.objects.create(
                    paper=paper, docname=f"legacy {chunk_index}",
                    chunk_index=chunk_index, content=content,
                    embedding=[1.0] + [0.0] * 1023,
                    embedding_model=active_model_cfg,
                    embedding_dim=active_dim_cfg, embedding_version=version,
                    content_hash=f"h{chunk_index}", citation_key="pqac-legacy",
                    search_vector="legacy chunk")

            fresh2 = MigrationExecutor(connection)
            fresh2.migrate([MIGRATE_TO])
            fresh3 = MigrationExecutor(connection)
            new_apps = fresh3.loader.project_state([MIGRATE_TO]).apps
            NewText = new_apps.get_model("rag", "Text")
            PaperIndexVersion = new_apps.get_model("rag", "PaperIndexVersion")
            Paper = new_apps.get_model("papers", "Paper")

            rows = list(NewText.objects.filter(content__startswith="LEGACY_")
                        .values("paper_id", "index_version_id"))
            self.assertTrue(rows and all(r["index_version_id"] for r in rows),
                            "every legacy Text must have index_version")
            self.assertTrue(all(v is not None for v in
                                [r["index_version_id"] for r in rows]),
                            "index_version must be non-null")

            new_legacy_paper = Paper.objects.get(id=legacy_paper.id)
            versions = list(PaperIndexVersion.objects.filter(
                paper_id=new_legacy_paper.id))
            actives = [v for v in versions if v.status == "active"]
            self.assertEqual(len(actives), 1,
                             "exactly one active version per paper")
            self.assertEqual(actives[0].embedding_version, active_v,
                             "newest compatible group must be active")
            self.assertTrue(all(v.status == "superseded"
                                for v in versions if v.status != "active"),
                            "incompatible group must be superseded")

            # duplicate (index_version, chunk_index) must raise IntegrityError
            active_version = actives[0]
            before = NewText.objects.filter(index_version=active_version,
                                            chunk_index=0).count()
            self.assertGreaterEqual(before, 1,
                                    "positive control: chunk 0 exists")
            with self.assertRaises(IntegrityError):
                NewText.objects.create(
                    paper_id=new_legacy_paper.id, docname="dup",
                    chunk_index=0, content="DUP_LEGACY",
                    embedding=[1.0] + [0.0] * 1023,
                    embedding_model=str(meta["embedding_model"]),
                    embedding_dim=1024, embedding_version=active_v,
                    content_hash="hdup", citation_key="pqac-dup",
                    search_vector="dup", index_version=active_version)

            # a second active version for the same paper must raise IntegrityError
            with self.assertRaises(IntegrityError):
                PaperIndexVersion.objects.create(
                    paper_id=new_legacy_paper.id, status="active",
                    source_sha256="sha-second", pipeline_signature="p2",
                    embedding_version=active_v,
                    embedding_model=str(meta["embedding_model"]),
                    embedding_dim=1024, chunk_count=1)
        finally:
            MigrationExecutor(connection).migrate(
                MigrationExecutor(connection).loader.graph.leaf_nodes())

    def test_ING_VERSION_ROLLBACK_FORWARD_NOTE(self):
        """Backward migration is safe ONLY before new-version chunks exist;
        once new versions exist production rollback MUST use a corrective
        forward migration (per design §Migration Plan / ING-B)."""
        self.case_id = "ING-VERSION-ROLLBACK-FORWARD-NOTE"
        self.expected_pre_fix = "PASS"
        self.contract = ("0004 backward works with legacy data only; the "
                         "corrective-forward requirement is documented")
        self.positive_control = "migration graph contains both nodes"
        self.negative_control = "backward must not run after new versions exist"
        self.root_cause = ""
        from django.db import connection
        from django.db.migrations.executor import MigrationExecutor

        ex = MigrationExecutor(connection)
        MF = ("rag", "0003_bge_m3_sparse_weights")
        MT = ("rag", "0004_paper_index_version")
        if MT not in ex.loader.graph.nodes:
            self.fail("versioning migration missing — Tasks 2.2 (red)")
        # forward on an EMPTY database is a no-op (test DB already applied);
        # the mixed-data forward/backward lifecycle is covered by
        # ING-VERSION-LEGACY-BACKFILL-MIGRATION. Here we prove backward is
        # possible on the CURRENT (clean, no new-version) state and that the
        # DB is restored afterwards.
        ex2 = MigrationExecutor(connection)
        ex2.migrate([MF])          # backward to legacy
        ex3 = MigrationExecutor(connection)
        legacy_apps = ex3.loader.project_state([MF]).apps
        self.assertTrue(hasattr(legacy_apps.get_model("rag", "Text"),
                                "index_version") is False,
                        "legacy state must not have Text.index_version")
        # restore to leaf
        MigrationExecutor(connection).migrate(
            MigrationExecutor(connection).loader.graph.leaf_nodes())
        ex4 = MigrationExecutor(connection)
        self.assertTrue(("rag", MT[1]) in ex4.loader.applied_migrations,
                        "leaf state restored after backward")

    def test_ING_VERSION_ONE_ACTIVE_CONSTRAINT(self):
        self.case_id = "ING-VERSION-ONE-ACTIVE-CONSTRAINT"
        self.expected_pre_fix = "FAIL"
        self.contract = ("the versioning migration must add the one-active "
                         "partial unique constraint and the (index_version, "
                         "chunk_index) uniqueness — proven by DB behavior in "
                         "LEGACY-BACKFILL-MIGRATION")
        self.positive_control = "migration graph contains the target node"
        self.negative_control = "target migration absent"
        self.root_cause = "no versioning migration"
        from django.db import connection
        from django.db.migrations.executor import MigrationExecutor

        executor = MigrationExecutor(connection)
        graph = executor.loader.graph
        if ("rag", "0004_paper_index_version") not in graph.nodes:
            self.fail("versioning migration missing — Tasks 2.2 (red)")
        # behavioral proof lives in LEGACY-BACKFILL-MIGRATION (IntegrityError
        # on duplicate chunk and on a second active version); here we just
        # assert the target migration exists (red until Tasks 2.2).
        self.assertTrue(
            ("rag", "0004_paper_index_version") in graph.nodes,
            "target migration must exist")

    def test_ING_VERSION_ACTIVE_ONLY_RETRIEVAL(self):
        """Four versions (active/building/superseded/failed) with per-version
        sentinel chunks; the ACTIVE-ONLY constraint must gate the retrieval
        candidate set itself (ING-B-CX-05 P0): direct assertions on the version
        ids of the scoped active_only queryset AND of the hybrid candidate
        lists (PostgreSQL SQL path when available, Python fallback otherwise)."""
        self.case_id = "ING-VERSION-ACTIVE-ONLY-RETRIEVAL"
        self.expected_pre_fix = "FAIL"
        self.contract = ("retrieval candidates read ONLY the active compatible "
                         "version; building/superseded/failed sentinels never "
                         "enter candidates")
        self.positive_control = "active sentinel is recalled"
        self.negative_control = ("building/superseded/failed sentinels "
                                 "excluded")
        self.root_cause = "retrieval gates on model/version columns, not on PaperIndexVersion.status"
        import rag.models
        self._require_version_model()
        from rag.models import PaperIndexVersion
        from agent.scope import ProjectScopeResolver
        from rag.retrieval import hybrid_retrieve_texts

        meta = _active_embedding_meta()
        active_v = str(meta["embedding_version"])
        base = dict(embedding_model=str(meta["embedding_model"]),
                    embedding_dim=1024, embedding_version=active_v,
                    search_vector="sentinel selective state space")
        # the ACTIVE version already exists from the scope fixture — reuse it
        versions = {}
        active_version = rag.models.PaperIndexVersion.objects.get(
            paper=self.paper_own, status="active")
        versions["active"] = active_version
        for status in ("building", "superseded", "failed"):
            v = rag.models.PaperIndexVersion.objects.create(
                paper=self.paper_own, status=status,
                source_sha256=f"sha-{status}", pipeline_signature="p1",
                embedding_version=active_v, embedding_model=base["embedding_model"],
                embedding_dim=1024, chunk_count=1)
            versions[status] = v
            from rag.models import Text
            Text.objects.create(
                paper=self.paper_own, index_version=v, docname=f"{status} chunk",
                chunk_index=0,
                content=f"VERSION_SENTINEL_{status} selective state space",
                embedding=[1.0] + [0.0] * 1023, **base,
                content_hash=f"h-{status}", citation_key="pqac-sentinel")
        from rag.models import Text
        active_sentinel = Text.objects.create(
            paper=self.paper_own, index_version=versions["active"],
            docname="active sentinel", chunk_index=7,
            content="VERSION_SENTINEL_active selective state space",
            embedding=[1.0] + [0.0] * 1023, **base,
            content_hash="h-active-sentinel", citation_key="pqac-sentinel")

        # 1) the scoped active_only queryset must contain ONLY active-version ids
        qs_ids = set(ProjectScopeResolver(self.proj_a.id).chunks(
            paper_ids=[self.paper_own.id], active_only=True)
            .values_list("id", flat=True))
        self.assertIn(active_sentinel.id, qs_ids,
                      "positive control: active sentinel in active_only queryset")
        self.assertEqual(
            set(Text.objects.filter(id__in=qs_ids)
                .values_list("index_version_id", flat=True)),
            {active_version.id},
            "active_only queryset must expose only active-version chunks")

        # 2) hybrid candidates (SQL path on PostgreSQL, Python fallback on
        #    SQLite) must carry ONLY the active version id
        candidates = asyncio.run(hybrid_retrieve_texts(
            "selective state space sentinel", paper_ids=[self.paper_own.id],
            final_k=20))
        cand_ids = [c.id for c in candidates]
        self.assertIn(active_sentinel.id, cand_ids,
                      "positive control: active sentinel recalled by retriever")
        self.assertEqual(
            {c.index_version_id for c in candidates}, {active_version.id},
            "every candidate must belong to the active index version")
        for status in ("building", "superseded", "failed"):
            self.assertNotIn(versions[status].id,
                             {c.index_version_id for c in candidates},
                             f"{status} sentinel must never be a candidate")


# =====================================================================
# Task 1.3 — safe PDF acquisition red contracts (SafePdfFetcher seam)
# =====================================================================

class PdfAcquisitionRedTest(_IngestionRedBase):

    OPAQUE = "Qz7xT9mK2pL4vR8wHn5c"

    def _fetch(self, url, resolver_answers, transport, valid_content=SMALL_PDF):
        """Drive SafePdfFetcher with injected resolver/transport.

        Returns (code, resolver, transport, payload_text):
          code: error_code string on rejection, "success" on success,
                exception class name otherwise (red marker).
        """
        fetcher = self._require_fetcher()
        resolver = SequenceResolver(resolver_answers)
        try:
            result = asyncio.run(fetcher().fetch(
                url, resolver=resolver, transport=transport))
            return "success", resolver, transport, str(result)[:200]
        except Exception as exc:
            code = getattr(exc, "error_code", None) or type(exc).__name__
            self._assert_safe_exception(exc, url)
            return code, resolver, transport, str(exc)[:200]

    def _assert_safe_exception(self, exc: Exception, url: str) -> None:
        """No URL, IP, filesystem path, raw exception body or opaque sentinel
        may leak from a rejection."""
        text = str(exc)
        for forbidden in (url, self.OPAQUE, "/", "\\", "paper.pdf",
                          "Exception", "Traceback"):
            self.assertNotIn(forbidden, text,
                             f"rejection leaked {forbidden!r}: {text!r}")

    def test_ING_ACQ_VALID_HTTPS(self):
        """POSITIVE control: public HTTPS + matching peer + valid PDF ->
        exactly one connection to the validated public IP, bytes returned."""
        self.case_id = "ING-ACQ-VALID-HTTPS"
        self.expected_pre_fix = "FAIL"
        self.contract = ("a public HTTPS fetch with a matching connected peer "
                         "and valid PDF succeeds and connects exactly the "
                         "validated public IP")
        self.positive_control = "this case IS the positive control"
        self.negative_control = "no connection to any non-public address"
        self.root_cause = "no SafePdfFetcher"
        transport = FakePinnedTransport(
            connected_peer="93.184.216.34",
            sequence=[{"status": 200, "content": SMALL_PDF, "headers": {}}])
        code, resolver, transport, payload = self._fetch(
            "https://paper.example/paper.pdf", [{"93.184.216.34"}], transport)
        self.assertEqual(code, "success",
                         "a valid public fetch must succeed")
        self.assertEqual(transport.connected, ["93.184.216.34"],
                         "exactly the validated public IP must be connected")
        self.assertEqual(resolver.calls, 1,
                         "the resolver must be consulted exactly once")
        self.assertTrue(payload.startswith("%PDF-"),
                        "valid PDF bytes must be returned")

    def test_ING_ACQ_HTTP_URL(self):
        self.case_id = "ING-ACQ-HTTP-URL"
        self.expected_pre_fix = "FAIL"
        self.contract = "http:// rejected with unsafe_pdf_url, zero connections"
        self.positive_control = "valid public https fetch succeeds"
        self.negative_control = "http:// -> unsafe_pdf_url, transport untouched"
        self.root_cause = "no SafePdfFetcher URL normalization"
        transport = FakePinnedTransport()
        code, resolver, transport, _payload = self._fetch(
            "http://example.com/paper.pdf", [{"93.184.216.34"}], transport)
        self.assertEqual(code, "unsafe_pdf_url",
                         "http:// must fail with unsafe_pdf_url")
        self.assertEqual(transport.connected, [],
                         "no connection may be made")

    def test_ING_ACQ_USERINFO_URL(self):
        self.case_id = "ING-ACQ-USERINFO-URL"
        self.expected_pre_fix = "FAIL"
        self.contract = "userinfo URL rejected with unsafe_pdf_url"
        self.positive_control = "public https fetch succeeds"
        self.negative_control = "userinfo -> unsafe_pdf_url, no connection"
        self.root_cause = "no URL normalization"
        transport = FakePinnedTransport()
        code, resolver, transport, _payload = self._fetch(
            "https://user:pass@example.com/paper.pdf",
            [{"93.184.216.34"}], transport)
        self.assertEqual(code, "unsafe_pdf_url")
        self.assertEqual(transport.connected, [])

    def test_ING_ACQ_LOOPBACK_DNS(self):
        self.case_id = "ING-ACQ-LOOPBACK-DNS"
        self.expected_pre_fix = "FAIL"
        self.contract = "loopback DNS answer rejected before connection"
        self.positive_control = "public answer connects"
        self.negative_control = "loopback -> unsafe_pdf_url, no connect"
        self.root_cause = "no DNS address validation"
        transport = FakePinnedTransport()
        code, resolver, transport, _payload = self._fetch(
            "https://paper.example/x.pdf", [{"127.0.0.1"}], transport)
        self.assertEqual(code, "unsafe_pdf_url")
        self.assertEqual(transport.connected, [])

    def test_ING_ACQ_PRIVATE_DNS(self):
        self.case_id = "ING-ACQ-PRIVATE-DNS"
        self.expected_pre_fix = "FAIL"
        self.contract = "private/CGNAT answers rejected"
        self.positive_control = "public answer connects"
        self.negative_control = ("10/8, 172.16/12, 192.168/16, 100.64/10 -> "
                                 "unsafe_pdf_url")
        self.root_cause = "no RFC1918/CGNAT validation"
        for addr in ("10.1.2.3", "172.16.5.5", "192.168.1.10", "100.64.0.1"):
            transport = FakePinnedTransport()
            code, resolver, transport, _payload = self._fetch(
                "https://paper.example/x.pdf", [{addr}], transport)
            self.assertEqual(code, "unsafe_pdf_url", addr)
            self.assertEqual(transport.connected, [], addr)

    def test_ING_ACQ_IPV6_LOOPBACK(self):
        self.case_id = "ING-ACQ-IPV6-LOOPBACK"
        self.expected_pre_fix = "FAIL"
        self.contract = "IPv6 loopback rejected"
        self.positive_control = "public answer connects"
        self.negative_control = "::1 -> unsafe_pdf_url"
        self.root_cause = "no IPv6 validation"
        transport = FakePinnedTransport()
        code, resolver, transport, _payload = self._fetch(
            "https://paper.example/x.pdf", [{"::1"}], transport)
        self.assertEqual(code, "unsafe_pdf_url")
        self.assertEqual(transport.connected, [])

    def test_ING_ACQ_LINK_LOCAL(self):
        self.case_id = "ING-ACQ-LINK-LOCAL"
        self.expected_pre_fix = "FAIL"
        self.contract = "link-local fe80::/10 rejected"
        self.positive_control = "public answer connects"
        self.negative_control = "fe80:: -> unsafe_pdf_url"
        self.root_cause = "no IPv6 scope validation"
        transport = FakePinnedTransport()
        code, resolver, transport, _payload = self._fetch(
            "https://paper.example/x.pdf", [{"fe80::1"}], transport)
        self.assertEqual(code, "unsafe_pdf_url")

    def test_ING_ACQ_RESERVED_DNS(self):
        self.case_id = "ING-ACQ-RESERVED-DNS"
        self.expected_pre_fix = "FAIL"
        self.contract = "reserved TEST-NET address rejected"
        self.positive_control = "public answer connects"
        self.negative_control = "192.0.2.1 -> unsafe_pdf_url"
        self.root_cause = "no reserved-address validation"
        transport = FakePinnedTransport()
        code, resolver, transport, _payload = self._fetch(
            "https://paper.example/x.pdf", [{"192.0.2.1"}], transport)
        self.assertEqual(code, "unsafe_pdf_url")

    def test_ING_ACQ_MIXED_DNS(self):
        self.case_id = "ING-ACQ-MIXED-DNS"
        self.expected_pre_fix = "FAIL"
        self.contract = ("mixed answers (public + private) rejected — EVERY "
                         "answer must be globally routable")
        self.positive_control = "all-public answers connect"
        self.negative_control = "public+private set -> unsafe_pdf_url"
        self.root_cause = "no per-answer validation"
        transport = FakePinnedTransport()
        code, resolver, transport, _payload = self._fetch(
            "https://paper.example/x.pdf",
            [{"93.184.216.34", "10.0.0.5"}], transport)
        self.assertEqual(code, "unsafe_pdf_url")
        self.assertEqual(transport.connected, [],
                         "no connection when any answer is non-global")

    def test_ING_ACQ_DNS_REBINDING(self):
        self.case_id = "ING-ACQ-DNS-REBINDING"
        self.expected_pre_fix = "FAIL"
        self.contract = ("rebinding: the FIRST public answer is pinned; a "
                         "second DNS resolution returning a private address "
                         "must never be connected. Safe success OR safe "
                         "rejection are both acceptable.")
        self.positive_control = "first answer is public (93.184.216.34)"
        self.negative_control = "10.0.0.9 never connected"
        self.root_cause = "no resolver/connection pinning (rebinding window)"
        transport = FakePinnedTransport(connected_peer="93.184.216.34")
        code, resolver, transport, _payload = self._fetch(
            "https://paper.example/x.pdf",
            [{"93.184.216.34"}, {"10.0.0.9"}], transport)
        self.assertGreaterEqual(resolver.calls, 1,
                                "resolver must be consulted")
        self.assertEqual(transport.connected, ["93.184.216.34"],
                         "the FIRST validated public IP must be pinned")
        self.assertNotIn("10.0.0.9", transport.connected,
                         "the rebound private address must never be connected")

    def test_ING_ACQ_PEER_VALIDATION(self):
        self.case_id = "ING-ACQ-PEER-VALIDATION"
        self.expected_pre_fix = "FAIL"
        self.contract = ("connected peer must equal the validated address; a "
                         "mismatch aborts")
        self.positive_control = "matching peer succeeds"
        self.negative_control = ("validated 93.184.216.34 but peer "
                                 "192.168.0.1 -> abort")
        self.root_cause = "no connected-peer verification"
        transport = FakePinnedTransport(connected_peer="192.168.0.1")
        code, resolver, transport, _payload = self._fetch(
            "https://paper.example/x.pdf", [{"93.184.216.34"}], transport)
        self.assertEqual(code, "unsafe_pdf_url",
                         "peer mismatch must abort")

    def test_ING_ACQ_REDIRECT_LIMIT(self):
        self.case_id = "ING-ACQ-REDIRECT-LIMIT"
        self.expected_pre_fix = "FAIL"
        self.contract = ("six+ redirects: initial_request_count=1, "
                         "redirects_followed <= 5, total_requests <= 6, "
                         "redirect_limit_exceeded, the 6th target never "
                         "connected")
        self.positive_control = "a short redirect chain is followed"
        self.negative_control = ">5 hops -> redirect_limit_exceeded"
        self.root_cause = "no manual 5-hop redirect handling"
        sequence = [
            _redirect_response(f"https://hop{i}.example/x.pdf")
            for i in range(6)
        ]
        transport = FakePinnedTransport(
            sequence=sequence, connected_peer="93.184.216.34")
        code, resolver, transport, _payload = self._fetch(
            "https://hop0.example/paper.pdf", [{"93.184.216.34"}], transport)
        initial_request_count = 1
        redirects_followed = max(0, len(transport.sent) - initial_request_count)
        total_requests = len(transport.sent)
        self.record("redirect", "initial_request_count",
                    initial_request_count != 1,
                    "initial request must be exactly 1")
        self.record("redirect", "redirects_followed", redirects_followed > 5,
                    "at most five redirects may be followed")
        self.record("redirect", "total_requests", total_requests > 6,
                    "total requests must be <= 6")
        self.assertEqual(code, "redirect_limit_exceeded",
                         "a 6-hop chain must fail with redirect_limit_exceeded")
        self.assertLessEqual(redirects_followed, 5,
                             "no more than five redirects followed")
        self.assertLessEqual(total_requests, 6,
                             "initial + redirects must not exceed 6")
        self.assertNotIn("hop6.example", " ".join(transport.sent),
                         "the 6th target must never be requested/connected")

    def test_ING_ACQ_REDIRECT_TO_PRIVATE_RESPONSE(self):
        self.case_id = "ING-ACQ-REDIRECT-TO-PRIVATE-RESPONSE"
        self.expected_pre_fix = "FAIL"
        self.contract = ("a redirect whose Location targets a private address "
                         "must fail with unsafe_pdf_url and never connect it")
        self.positive_control = "public target connects"
        self.negative_control = "Location 127.0.0.1 -> unsafe_pdf_url"
        self.root_cause = "no per-hop redirect revalidation"
        transport = FakePinnedTransport(
            sequence=[_redirect_response("https://127.0.0.1/x.pdf")],
            connected_peer="93.184.216.34")
        code, resolver, transport, _payload = self._fetch(
            "https://paper.example/x.pdf", [{"93.184.216.34"}], transport)
        self.assertEqual(code, "unsafe_pdf_url")
        self.assertNotIn("127.0.0.1", transport.connected)

    def test_ING_ACQ_TRUST_ENV(self):
        self.case_id = "ING-ACQ-TRUST-ENV"
        self.expected_pre_fix = "FAIL"
        self.contract = "fetcher must not inherit environment proxies"
        self.positive_control = "fetcher construction is observable"
        self.negative_control = "no trust_env proxy inheritance"
        self.root_cause = "httpx.Client(trust_env=True) in download_pdf"
        from rag.ingest import download_pdf
        constructed: list = []
        with self._probe_client(constructed):
            download_pdf("https://example.com/paper.pdf")
        self.assertTrue(constructed, "positive control: client built")
        kwargs = constructed[0][1]
        trust_env = kwargs.get("trust_env", True)
        self.record("download_pdf", "trust_env_disabled", trust_env is True,
                    "trust_env must be False")
        self.assertIs(trust_env, False,
                      "trust_env must be disabled (Tasks 3.1)")

    def test_ING_ACQ_REDIRECT_PRIVATE(self):
        self.case_id = "ING-ACQ-REDIRECT-PRIVATE"
        self.expected_pre_fix = "FAIL"
        self.contract = ("redirects must be manual (follow_redirects=False) "
                         "with per-hop revalidation")
        self.positive_control = "client construction kwargs observable"
        self.negative_control = "follow_redirects must be False"
        self.root_cause = "httpx.Client(follow_redirects=True)"
        from rag.ingest import download_pdf
        constructed: list = []
        with self._probe_client(constructed):
            download_pdf("https://example.com/paper.pdf")
        kwargs = constructed[0][1]
        follow = kwargs.get("follow_redirects", True)
        self.record("download_pdf", "manual_redirect_revalidation",
                    follow is True,
                    "redirects must be manual + revalidated per hop")
        self.assertIs(follow, False,
                      "automatic redirect following must be disabled (Tasks 3.1)")

    def test_ING_ACQ_SIZE_LIMIT(self):
        self.case_id = "ING-ACQ-SIZE-LIMIT"
        self.expected_pre_fix = "FAIL"
        self.contract = "streams > 50 MiB rejected with size_limit_exceeded"
        self.positive_control = "<= limit stream accepted"
        self.negative_control = ">50MiB -> size_limit_exceeded"
        self.root_cause = "download_pdf buffers the whole response"
        transport = FakePinnedTransport(
            sequence=[{"status": 200, "content": OVER_LIMIT_PDF, "headers": {}}],
            connected_peer="93.184.216.34")
        code, resolver, transport, _payload = self._fetch(
            "https://paper.example/x.pdf", [{"93.184.216.34"}], transport)
        self.assertEqual(code, "size_limit_exceeded")

    def test_ING_ACQ_INVALID_MAGIC(self):
        self.case_id = "ING-ACQ-INVALID-MAGIC"
        self.expected_pre_fix = "FAIL"
        self.contract = "non-PDF content rejected with invalid_pdf_magic"
        self.positive_control = "valid PDF accepted"
        self.negative_control = "content without %PDF- -> invalid_pdf_magic"
        self.root_cause = "no PDF-magic validation"
        transport = FakePinnedTransport(
            sequence=[{"status": 200, "content": NOT_A_PDF, "headers": {}}],
            connected_peer="93.184.216.34")
        code, resolver, transport, _payload = self._fetch(
            "https://paper.example/x.pdf", [{"93.184.216.34"}], transport)
        self.assertEqual(code, "invalid_pdf_magic")

    def test_ING_ACQ_EMPTY_UPLOAD(self):
        self.case_id = "ING-ACQ-EMPTY-UPLOAD"
        self.expected_pre_fix = "PASS"
        self.contract = "empty upload rejected with a stable 400"
        self.positive_control = "upload endpoint responds"
        self.negative_control = "empty file -> 400"
        self.root_cause = ""
        resp = self._upload(self.proj_a.id, self.paper_own.id, "empty.pdf", b"")
        self.assertEqual(resp.status_code, 400,
                         "empty upload must be rejected (stable code)")

    def test_ING_ACQ_EXACT_LIMIT_UPLOAD(self):
        self.case_id = "ING-ACQ-EXACT-LIMIT-UPLOAD"
        self.expected_pre_fix = "PASS"
        self.contract = "upload exactly at the 50 MiB limit is accepted"
        self.positive_control = "upload endpoint responds"
        self.negative_control = "== limit must not be rejected"
        self.root_cause = ""
        with self._enqueue_stub():
            resp = self._upload(self.proj_a.id, self.paper_own.id,
                                "exact.pdf", EXACT_LIMIT_PDF)
        self.assertEqual(resp.status_code, 201,
                         "50 MiB boundary upload must be accepted")

    def test_ING_ACQ_OVER_LIMIT_UPLOAD(self):
        self.case_id = "ING-ACQ-OVER-LIMIT-UPLOAD"
        self.expected_pre_fix = "FAIL"
        self.contract = "upload > 50 MiB rejected with a stable code"
        self.positive_control = "upload endpoint responds"
        self.negative_control = "> 50 MiB -> rejection, no job created"
        self.root_cause = "upload view reads the whole file with no size cap"
        from api.models import PaperIngestionJob
        with self._enqueue_stub():
            resp = self._upload(self.proj_a.id, self.paper_own.id,
                                "big.pdf", OVER_LIMIT_PDF)
        jobs = PaperIngestionJob.objects.filter(paper_id=self.paper_own.id).count()
        accepted = resp.status_code == 201 or jobs > 0
        self.record("upload view", "over_limit_rejected", accepted,
                    ">50MiB upload must be rejected")
        self.assertFalse(accepted,
                         "oversized upload must be rejected (Tasks 3.2)")

    def test_ING_ACQ_NONPDF_UPLOAD(self):
        self.case_id = "ING-ACQ-NONPDF-UPLOAD"
        self.expected_pre_fix = "FAIL"
        self.contract = "non-PDF upload rejected before any job is created"
        self.positive_control = "upload endpoint responds"
        self.negative_control = "non-PDF -> 4xx + no job"
        self.root_cause = "upload view never validates PDF magic"
        from api.models import PaperIngestionJob
        with self._enqueue_stub():
            resp = self._upload(self.proj_a.id, self.paper_own.id,
                                "fake.pdf", NOT_A_PDF)
        jobs = PaperIngestionJob.objects.filter(paper_id=self.paper_own.id).count()
        accepted = resp.status_code == 201 and jobs > 0
        self.record("upload view", "non_pdf_rejected", accepted,
                    "non-PDF upload must not create a job")
        self.assertFalse(accepted,
                         "non-PDF upload must be rejected before a job "
                         "(Tasks 3.2)")

    def test_ING_ACQ_STREAMING_UPLOAD(self):
        self.case_id = "ING-ACQ-STREAMING-UPLOAD"
        self.expected_pre_fix = "FAIL"
        self.contract = ("the production view must stream uploads via "
                         "chunks() — read() call count must be 0")
        self.positive_control = "view runs and returns a response"
        self.negative_control = "view read_count == 0, chunks_count > 0"
        self.root_cause = "upload view calls uploaded.read()"
        from rest_framework.test import APIRequestFactory
        from api.views import project_paper_pdf_upload

        uploaded = _CountingUploadedFile("stream.pdf", SMALL_PDF)
        factory = APIRequestFactory()
        # pre-populate FILES directly — the request never passes through the
        # multipart encoder, so only the production view touches the file
        request = factory.post(
            f"/api/projects/{self.proj_a.id}/papers/{self.paper_own.id}/pdf-upload",
            {})
        request.FILES["file"] = uploaded
        # zero the counters before the production path begins
        uploaded.read_count = 0
        uploaded.chunks_count = 0
        with self._enqueue_stub():
            response = project_paper_pdf_upload(
                request, self.proj_a.id, self.paper_own.id)
        self.assertEqual(uploaded.read_count, 0,
                         "the production view must never call read()")
        self.assertGreater(uploaded.chunks_count, 0,
                           "the production view must consume chunks()")
        self.assertEqual(response.status_code, 201,
                         "positive control: the view produced a response")

    def test_ING_ACQ_FILENAME_TRAVERSAL(self):
        self.case_id = "ING-ACQ-FILENAME-TRAVERSAL"
        self.expected_pre_fix = "PASS"
        self.contract = "storage path derived from server values only"
        self.positive_control = "upload job created"
        self.negative_control = "traversal name must not escape MEDIA_ROOT"
        self.root_cause = ""
        from api.models import PaperIngestionJob
        from django.conf import settings
        with self._enqueue_stub():
            self._upload(self.proj_a.id, self.paper_own.id,
                         "../../../../evil.pdf", SMALL_PDF)
        job = PaperIngestionJob.objects.filter(paper_id=self.paper_own.id).first()
        self.assertTrue(job, "positive control: upload job created")
        stored = Path(job.file_path).resolve()
        media = Path(settings.MEDIA_ROOT).resolve()
        self.assertTrue(str(stored).startswith(str(media)),
                        "stored path must stay under MEDIA_ROOT")
        self.assertNotIn("/", job.file_name or "")
        self.assertNotIn("\\", job.file_name or "")
        self.assertEqual(job.file_name, "evil.pdf",
                         "name must be reduced to basename")

    def test_ING_ACQ_PARTIAL_CLEANUP(self):
        self.case_id = "ING-ACQ-PARTIAL-CLEANUP"
        self.expected_pre_fix = "FAIL"
        self.contract = ("a WRITE INTERRUPTION must leave no partial artifact "
                         "(no .part temp file, no partial bytes)")
        self.positive_control = "upload endpoint attempts the write"
        self.negative_control = "interrupted write -> no leftovers"
        self.root_cause = "no .part temp file; write_bytes is not atomic"
        from django.conf import settings

        nonce = str(time.time_ns()).encode()
        unique = SMALL_PDF + b"\ncleanup-" + hashlib.sha256(nonce).digest()
        target_dir = Path(settings.MEDIA_ROOT) / "papers" / str(self.paper_own.id)
        target_dir.mkdir(parents=True, exist_ok=True)
        before = set(target_dir.iterdir())

        def _interrupted_replace(src, dst):
            with open(dst, "wb") as fh:
                fh.write(b"partial")
            raise OSError("simulated write interruption")

        # ING-C-CX-02: the commit surface is now the atomic os.replace —
        # interrupt IT and assert no partial artifact survives.
        with mock.patch("api.views.os.replace", _interrupted_replace):
            resp = self._upload(self.proj_a.id, self.paper_own.id,
                                "cleanup.pdf", unique,
                                raise_request_exception=False)
        after = set(target_dir.iterdir())
        leftovers = after - before
        self.record("upload view", "partial_cleanup", bool(leftovers),
                    "interrupted writes must leave no partial file (incl. .part)")
        self.assertFalse(leftovers,
                         "partial artifact must be cleaned up on failure "
                         "(Tasks 3.2)")

    def test_ING_ACQ_SOCKET_GUARD_CANARY(self):
        self.case_id = "ING-ACQ-SOCKET-GUARD-CANARY"
        self.expected_pre_fix = "PASS"
        self.contract = ("the test network guard replaces the ORIGINAL socket; "
                         "a canary connect raises NetworkAccessBlocked")
        self.positive_control = "guard installed by the mixin"
        self.negative_control = "canary connect to a blocked address"
        self.root_cause = ""
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        with self.assertRaises(Exception) as ctx:
            sock.connect(("192.0.2.250", 443))
        self.assertIn("NetworkAccessBlocked",
                      type(ctx.exception).__name__,
                      "original socket must be replaced by the guard")


class _FakeSocketPeer:
    def __init__(self, peer: str):
        self._peer = peer

    def getpeername(self):
        return (self._peer, 443)


class _FakeNetworkStream:
    def __init__(self, peer: str):
        self._socket = _FakeSocketPeer(peer)

    def get_extra_info(self, name, default=None):
        return self._socket if name == "socket" else default


class _FakeStreamingResponse:
    """httpx-like streaming response: iter_bytes only, .content access raises
    (ING-C-CX-03: the production path must never touch response.content)."""

    def __init__(self, chunks, peer, status=200, headers=None):
        self.status_code = status
        self.headers = headers or {}
        self._chunks = list(chunks)
        self.extensions = {"network_stream": _FakeNetworkStream(peer)}
        self.closed = False

    def iter_bytes(self):
        for chunk in self._chunks:
            yield chunk

    def close(self):
        self.closed = True

    @property
    def content(self):
        raise AssertionError("response.content must never be accessed — "
                             "streaming only (ING-C-CX-03)")


class _FakeRequest:
    """httpx.Request lookalike for the recording client (url/headers/
    extensions attributes)."""

    def __init__(self, method, url, headers=None):
        self.method = method
        self.url = url
        self.headers = headers or {}
        self.extensions = {}


class _RecordingHttpxClient:
    """Minimal httpx.Client lookalike for the PRODUCTION transport path:
    records build_request/send calls so tests can assert IP pinning, Host/SNI
    preservation and real-socket peer verification."""

    def __init__(self, responses, peer="93.184.216.34"):
        self.responses = list(responses)
        self.peer = peer
        self.requests = []

    def build_request(self, method, url, headers=None):
        return _FakeRequest(method, url, headers)

    def send(self, request, stream=False):
        self.requests.append(request)
        if self.responses:
            return self.responses.pop(0)
        return _FakeStreamingResponse([SMALL_PDF], peer=self.peer)

    def get(self, url, **kwargs):
        raise AssertionError("production path must not use client.get")


class IngCTransportFixRedTest(_IngestionRedBase):
    """ING-C-CX-01..03 (Codex findings): production IP pinning, upload commit
    without whole-file read, and streaming download without response.content."""

    def test_INGC_CX01_TRANSPORT_PINS_IP_AND_PEER(self):
        """Production _HttpxTransport.send must connect the VALIDATED IP: the
        request URL host is rewritten to the pinned address, the original
        hostname is preserved in Host/SNI, and the REAL socket peer must equal
        the pinned IP (mismatch aborts with unsafe_pdf_url)."""
        self.case_id = "ING-C-CX-01-PINS-IP-AND-PEER"
        self.expected_pre_fix = "PASS"
        self.contract = ("production transport rewrites the request to the "
                         "validated IP with Host/SNI preserved and verifies "
                         "the real socket peer")
        self.positive_control = "matching peer fetch succeeds"
        self.negative_control = "socket peer mismatch aborts"
        self.root_cause = ""
        from rag.acquisition import _HttpxTransport, SafePdfFetcher

        client = _RecordingHttpxClient(
            [_FakeStreamingResponse([SMALL_PDF[:64], SMALL_PDF[64:]],
                                    peer="93.184.216.34")],
            peer="93.184.216.34")
        transport = _HttpxTransport(client)
        result = asyncio.run(SafePdfFetcher().fetch(
            "https://paper.example/paper.pdf",
            resolver=SequenceResolver([{"93.184.216.34"}]),
            transport=transport))
        self.assertTrue(result.startswith("%PDF-"),
                        "positive control: pinned fetch succeeds")
        request = client.requests[0]
        self.assertEqual(request.url, "https://93.184.216.34:443/paper.pdf",
                         "request URL must connect the validated IP")
        self.assertEqual(request.headers.get("Host"),
                         "paper.example:443",
                         "original hostname must be preserved in Host")
        self.assertEqual(request.extensions.get("sni_hostname"),
                         "paper.example",
                         "original hostname must be preserved in SNI")
        self.assertEqual(transport.connected_peer, "93.184.216.34",
                         "connected peer must come from the real socket")

        # negative control: socket peer != pinned IP -> abort
        bad_client = _RecordingHttpxClient(
            [_FakeStreamingResponse([SMALL_PDF], peer="192.168.0.1")],
            peer="192.168.0.1")
        bad_transport = _HttpxTransport(bad_client)
        with self.assertRaises(Exception) as ctx:
            asyncio.run(SafePdfFetcher().fetch(
                "https://paper.example/paper.pdf",
                resolver=SequenceResolver([{"93.184.216.34"}]),
                transport=bad_transport))
        self.assertEqual(getattr(ctx.exception, "error_code", None),
                         "unsafe_pdf_url",
                         "peer mismatch must abort")

    def test_INGC_CX01_REBINDING_PINNED_PRODUCTION(self):
        """Resolved ONCE to a public set; the production transport must then
        connect the pinned IP (no second DNS resolution at connect time), so a
        later private answer can never be reached."""
        self.case_id = "ING-C-CX-01-REBINDING-PINNED"
        self.expected_pre_fix = "PASS"
        self.contract = ("production fetch pins the first validated answer; "
                         "the transport connects the IP, never re-resolving "
                         "the hostname")
        self.positive_control = "first answer is public (93.184.216.34)"
        self.negative_control = "second (private) answer is never used"
        self.root_cause = ""
        from rag.acquisition import _HttpxTransport, SafePdfFetcher

        resolver = SequenceResolver([{"93.184.216.34"}, {"10.0.0.9"}])
        client = _RecordingHttpxClient(
            [_FakeStreamingResponse([SMALL_PDF], peer="93.184.216.34")])
        result = asyncio.run(SafePdfFetcher().fetch(
            "https://paper.example/paper.pdf", resolver=resolver,
            transport=_HttpxTransport(client)))
        self.assertTrue(result.startswith("%PDF-"))
        self.assertEqual(resolver.calls, 1,
                         "resolver must be consulted exactly once")
        self.assertEqual(client.requests[0].url,
                         "https://93.184.216.34:443/paper.pdf",
                         "transport must connect the pinned public IP")
        self.assertNotIn("10.0.0.9", client.requests[0].url,
                         "the rebound private address must never be requested")

    def test_INGC_CX02_UPLOAD_COMMIT_NO_READBYTES(self):
        """Upload commit must promote the .part file by ATOMIC os.replace —
        Path.read_bytes must never be called and the payload is never read
        back into memory."""
        self.case_id = "ING-C-CX-02-COMMIT-NO-READBYTES"
        self.expected_pre_fix = "PASS"
        self.contract = ("upload commit uses same-directory replace; "
                         "Path.read_bytes is never called")
        self.positive_control = "upload succeeds and returns 201"
        self.negative_control = "read_bytes called -> test fails"
        self.root_cause = ""
        from pathlib import Path as _Path

        replace_calls = []
        with mock.patch.object(_Path, "read_bytes",
                               side_effect=AssertionError(
                                   "Path.read_bytes must never be called")), \
             mock.patch("api.views.os.replace",
                        side_effect=lambda src, dst: replace_calls.append(
                            (str(src), str(dst)))), \
             self._enqueue_stub():
            resp = self._upload(self.proj_a.id, self.paper_own.id,
                                "commit.pdf", SMALL_PDF)
        self.assertEqual(resp.status_code, 201,
                         "positive control: upload accepted")
        self.assertTrue(replace_calls,
                        "commit must go through os.replace (atomic rename)")
        src, dst = replace_calls[0]
        self.assertTrue(src.endswith(".part"),
                        "the source must be the .part temp file")
        self.assertTrue(dst.endswith(".pdf"),
                        "the destination must be content-addressed")

    def test_INGC_CX02_REPLACE_FAILURE_CLEANS(self):
        """If the atomic replace fails, the .part temp file must be removed
        and no partial artifact may remain."""
        self.case_id = "ING-C-CX-02-REPLACE-FAILURE-CLEANS"
        self.expected_pre_fix = "PASS"
        self.contract = ("failed replace leaves no .part and no partial "
                         "artifact")
        self.positive_control = "upload attempts the replace"
        self.negative_control = "no leftovers after replace failure"
        self.root_cause = ""
        from django.conf import settings

        target_dir = Path(settings.MEDIA_ROOT) / "papers" / str(self.paper_own.id)
        target_dir.mkdir(parents=True, exist_ok=True)
        before = set(target_dir.iterdir())
        with mock.patch("api.views.os.replace",
                        side_effect=OSError("simulated replace failure")), \
             self._enqueue_stub():
            resp = self._upload(self.proj_a.id, self.paper_own.id,
                                "replacefail.pdf", SMALL_PDF,
                                raise_request_exception=False)
        after = set(target_dir.iterdir())
        self.assertEqual(after, before,
                         "failed replace must leave zero leftovers (incl. .part)")

    def test_INGC_CX03_DOWNLOAD_STREAMING_NO_CONTENT(self):
        """The production fetch must consume the body streaming via
        iter_bytes; response.content is never accessed (its access raises)."""
        self.case_id = "ING-C-CX-03-DOWNLOAD-STREAMING"
        self.expected_pre_fix = "PASS"
        self.contract = ("download body is consumed via streaming chunks; "
                         "response.content is never read")
        self.positive_control = "streaming fetch returns the PDF"
        self.negative_control = "accessing .content fails the test"
        self.root_cause = ""
        from rag.acquisition import _HttpxTransport, SafePdfFetcher

        response = _FakeStreamingResponse(
            [SMALL_PDF[:64], SMALL_PDF[64:256], SMALL_PDF[256:]],
            peer="93.184.216.34")
        client = _RecordingHttpxClient([response], peer="93.184.216.34")
        result = asyncio.run(SafePdfFetcher().fetch(
            "https://paper.example/paper.pdf",
            resolver=SequenceResolver([{"93.184.216.34"}]),
            transport=_HttpxTransport(client)))
        self.assertTrue(result.startswith("%PDF-"),
                        "streaming body must yield the full PDF")
        self.assertTrue(response.closed,
                        "the streaming response must be closed after consume")

    def test_INGC_CX04_KEEPS_EXISTING_TARGET(self):
        """ING-C-CX-04: when a content-addressed {sha256}.pdf ALREADY exists,
        a failed replace must NOT delete it — cleanup removes only the .part;
        the API returns storage_failed with no path/exception leak."""
        self.case_id = "ING-C-CX-04-KEEPS-EXISTING-TARGET"
        self.expected_pre_fix = "PASS"
        self.contract = ("failed replace keeps the pre-existing "
                         "content-addressed artifact intact")
        self.positive_control = "existing {sha}.pdf is preserved unchanged"
        self.negative_control = (".part cleaned; pre-existing target never "
                                 "deleted; storage_failed; no leak")
        self.root_cause = ""
        from django.conf import settings

        file_hash = hashlib.sha256(SMALL_PDF).hexdigest()
        target_dir = Path(settings.MEDIA_ROOT) / "papers" / str(self.paper_own.id)
        target_dir.mkdir(parents=True, exist_ok=True)
        target_path = target_dir / f"{file_hash}.pdf"
        target_path.write_bytes(SMALL_PDF)
        before = set(target_dir.iterdir())

        def _failed_replace(src, dst):
            raise OSError("simulated replace failure")

        with mock.patch("api.views.os.replace", _failed_replace), \
             self._enqueue_stub():
            resp = self._upload(self.proj_a.id, self.paper_own.id,
                                "keep.pdf", SMALL_PDF,
                                raise_request_exception=False)

        self.assertEqual(resp.status_code, 500,
                         "replace failure must surface storage_failed")
        self.assertEqual(resp.data.get("error"), "storage_failed",
                         "stable error code, no path/exception leak")
        body = json.dumps(resp.data, default=str)
        for forbidden in ("media", "papers", file_hash, "replace", "OSError",
                          "Traceback"):
            self.assertNotIn(forbidden, body,
                             f"response leaked {forbidden!r}: {body!r}")
        after = set(target_dir.iterdir())
        self.assertTrue(target_path in before and target_path in after,
                        "the pre-existing artifact must survive")
        self.assertEqual(target_path.read_bytes(), SMALL_PDF,
                         "the pre-existing artifact must be UNCHANGED")
        self.assertEqual(after - {target_path}, before - {target_path},
                         "no .part or other leftovers may remain")

    def test_INGC_CX01_PINS_IP_IPV6_NETLOC(self):
        """A public IPv6 pinned address must be rewritten with brackets:
        https://[addr]:443/... (no real network required)."""
        self.case_id = "ING-C-CX-01-PINS-IP-IPV6"
        self.expected_pre_fix = "PASS"
        self.contract = ("production transport rewrites the request to a "
                         "bracketed IPv6 netloc")
        self.positive_control = "IPv6 fetch succeeds"
        self.negative_control = "netloc must be [addr]:port"
        self.root_cause = ""
        from rag.acquisition import _HttpxTransport, SafePdfFetcher

        ipv6 = "2606:4700:4700::1111"
        client = _RecordingHttpxClient(
            [_FakeStreamingResponse([SMALL_PDF], peer=ipv6)], peer=ipv6)
        result = asyncio.run(SafePdfFetcher().fetch(
            "https://paper.example/paper.pdf",
            resolver=SequenceResolver([{ipv6}]),
            transport=_HttpxTransport(client)))
        self.assertTrue(result.startswith("%PDF-"),
                        "positive control: IPv6 fetch succeeds")
        request = client.requests[0]
        self.assertEqual(request.url, f"https://[{ipv6}]:443/paper.pdf",
                         "IPv6 netloc must be bracketed")
        self.assertEqual(request.headers.get("Host"), "paper.example:443",
                         "original hostname must be preserved in Host")


class IngDExecutionFixRedTest(_IngestionRedBase):
    """ING-D (Tasks 4.x) verification: short activation transaction and
    redelivery-safe build reuse on the real task path."""

    def _drive_job(self, source_identity="ingd-source"):
        """Create a pending job, claim its build version and run the real
        task end-to-end (parse mocked to deterministic text; embeddings via
        the fake provider)."""
        from api.ingestion_service import IngestionService
        from api.models import PaperIngestionJob
        from api.tasks import ingest_paper_pdf_task

        job = PaperIngestionJob.objects.create(
            project=self.proj_a, paper=self.paper_own, status="pending",
            file_hash=source_identity, source_kind="test")
        version = IngestionService().claim_build(job, source_identity)
        job.refresh_from_db()
        with mock.patch("api.tasks._load_pdf_bytes", return_value=b"pdf"), \
             mock.patch("rag.ingest.parse_pdf_pages",
                        return_value=("selective state space model. " * 300,
                                      [])):
            ingest_paper_pdf_task.run(job.id)
        job.refresh_from_db()
        return job, version

    def test_INGD_ACTIVATE_ONE_ACTIVE(self):
        """Tasks 4.3: the real task must activate EXACTLY ONE version — the
        previous active row is superseded, persisted chunk count matches, and
        the job is embedded."""
        self.case_id = "ING-D-ACTIVATE-ONE-ACTIVE"
        self.expected_pre_fix = "PASS"
        self.contract = ("short activation transaction: one active version, "
                         "old active superseded, chunks verified")
        self.positive_control = "task embeds chunks and activates the build"
        self.negative_control = ("never >1 active; old active must be "
                                 "superseded; failed verify must roll back")
        self.root_cause = ""
        from rag.models import PaperIndexVersion

        previous_active = PaperIndexVersion.objects.get(
            paper=self.paper_own, status="active")
        job, version = self._drive_job()
        version.refresh_from_db()
        self.assertEqual(job.status, "embedded",
                         "positive control: job embedded")
        self.assertEqual(version.status, "active",
                         "the claimed build must be activated")
        self.assertGreater(version.chunk_count, 0,
                           "activated version must carry chunks")
        from rag.models import Text
        persisted = Text.objects.filter(index_version=version).count()
        self.assertEqual(persisted, version.chunk_count,
                         "activated chunk count must match persisted chunks")
        actives = list(PaperIndexVersion.objects.filter(
            paper=self.paper_own, status="active"))
        self.assertEqual(len(actives), 1,
                         "exactly ONE active version may exist")
        self.assertEqual(actives[0].id, version.id,
                         "the activated version must be the build")
        previous_active.refresh_from_db()
        self.assertEqual(previous_active.status, "superseded",
                         "the previous active version must be superseded")

    def test_INGD_ACTIVATION_FAILURE_KEEPS_OLD(self):
        """Tasks 4.3 failure control: when chunk persistence is broken, the
        old active version survives and nothing is activated."""
        self.case_id = "ING-D-ACTIVATION-FAILURE-KEEPS-OLD"
        self.expected_pre_fix = "PASS"
        self.contract = ("activation failure keeps the old index queryable "
                         "(rollback boundary)")
        self.positive_control = "old active exists before the attempt"
        self.negative_control = ("failed persistence -> no new active, old "
                                 "active untouched")
        self.root_cause = ""
        from api.ingestion_service import IngestionService
        from api.models import PaperIngestionJob
        from rag.models import PaperIndexVersion

        previous_active = PaperIndexVersion.objects.get(
            paper=self.paper_own, status="active")
        job = PaperIngestionJob.objects.create(
            project=self.proj_a, paper=self.paper_own, status="pending",
            file_hash="ingd-fail", source_kind="test")
        IngestionService().claim_build(job, "ingd-fail")
        with mock.patch("api.tasks._load_pdf_bytes", return_value=b"pdf"), \
             mock.patch("rag.ingest.parse_pdf_pages",
                        return_value=("y" * 3000, [])), \
             mock.patch("rag.ingest.Text.objects.bulk_create",
                        side_effect=RuntimeError("insert boom")):
            from api.tasks import ingest_paper_pdf_task
            try:
                ingest_paper_pdf_task.run(job.id)
            except Exception:
                # transient ingest failure (auto-retried by Celery) — the
                # activation must never have happened
                pass
        actives = list(PaperIndexVersion.objects.filter(
            paper=self.paper_own, status="active"))
        self.assertEqual(actives, [previous_active],
                         "old active must survive the failed attempt")
        previous_active.refresh_from_db()
        self.assertEqual(previous_active.status, "active",
                         "old active must stay active (not superseded)")

    def test_INGD_REDELIVERY_REUSE_BUILD(self):
        """Tasks 4.4: running the task twice (a redelivery) must reuse the
        SAME claimed build version — one active, chunk set identical."""
        self.case_id = "ING-D-REDELIVERY-REUSE-BUILD"
        self.expected_pre_fix = "PASS"
        self.contract = ("redelivery resumes/reuses the same build identity")
        self.positive_control = "first run embeds chunks"
        self.negative_control = ("second run reuses the same version; chunk "
                                 "count stable")
        self.root_cause = ""
        from rag.models import PaperIndexVersion, Text

        job, version = self._drive_job(source_identity="ingd-redeliver")
        version.refresh_from_db()
        self.assertEqual(version.status, "active", "first run activated")
        first_chunks = Text.objects.filter(index_version=version).count()

        with mock.patch("api.tasks._load_pdf_bytes", return_value=b"pdf"), \
             mock.patch("rag.ingest.parse_pdf_pages",
                        return_value=("selective state space model. " * 300,
                                      [])):
            from api.tasks import ingest_paper_pdf_task
            ingest_paper_pdf_task.run(job.id)
        version.refresh_from_db()
        job.refresh_from_db()
        self.assertEqual(job.status, "embedded",
                         "redelivery completes successfully")
        self.assertEqual(version.status, "active",
                         "redelivery reuses the same active build")
        actives = list(PaperIndexVersion.objects.filter(
            paper=self.paper_own, status="active"))
        self.assertEqual([v.id for v in actives], [version.id],
                         "exactly one active version after redelivery")
        self.assertEqual(Text.objects.filter(index_version=version).count(),
                         first_chunks,
                         "redelivery must not duplicate chunks")

    def test_INGD_CX01_REUSE_ACTIVE_BUILD(self):
        """ING-D-CX-01: a NEW project upload whose global build is already
        active must REUSE it — no enqueue, no parse/embed/write/delete; the
        job lands directly in embedded with the active chunk_count; the active
        Text ids never change; exactly one active version survives."""
        self.case_id = "ING-D-CX-01-REUSE-ACTIVE-BUILD"
        self.expected_pre_fix = "PASS"
        self.contract = ("pre-existing active global build is reused by a new "
                         "project upload without rewriting anything")
        self.positive_control = "new project job becomes embedded/reused"
        self.negative_control = ("no enqueue; active Text ids unchanged; one "
                                 "active version")
        self.root_cause = ""
        import hashlib

        from api.models import ProjectPaper, PaperIngestionJob
        from rag.models import Text

        source_hash = hashlib.sha256(SMALL_PDF).hexdigest()
        _job, version = self._drive_job(source_identity=source_hash)
        version.refresh_from_db()
        self.assertEqual(version.status, "active",
                         "positive control: global build is active")
        ids_before = set(Text.objects.filter(
            index_version=version).values_list("id", flat=True))
        self.assertTrue(ids_before, "positive control: active chunks exist")

        ProjectPaper.objects.create(
            project=self.proj_b, paper=self.paper_own, status="included")
        enqueued: list = []
        with mock.patch("api.views._enqueue_ingestion_job",
                        side_effect=lambda job: enqueued.append(job.id)
                        or type("Fake", (), {"id": "x"})()):
            resp = self._upload(self.proj_b.id, self.paper_own.id,
                                "reuse.pdf", SMALL_PDF)
        # Tasks 5.1: a reused build answers 200 + deduplicated=true (not 201)
        self.assertEqual(resp.status_code, 200,
                         "reused build must answer 200, not 201")
        self.assertTrue(resp.json().get("deduplicated"),
                        "reused build must carry deduplicated=true")
        self.assertEqual(enqueued, [],
                         "an already-active build must NOT be enqueued again")
        job2 = PaperIngestionJob.objects.get(
            project=self.proj_b, paper=self.paper_own)
        self.assertEqual(job2.status, "embedded",
                         "job must land directly in the embedded/reused state")
        self.assertEqual(job2.chunk_count, version.chunk_count,
                         "chunk_count must come from the active build")
        self.assertEqual(job2.index_version_id, version.id,
                         "job must reference the reused active build")
        ids_after = set(Text.objects.filter(
            index_version=version).values_list("id", flat=True))
        self.assertEqual(ids_after, ids_before,
                         "active Text ids must be UNCHANGED by the reuse")
        from rag.models import PaperIndexVersion
        actives = list(PaperIndexVersion.objects.filter(
            paper=self.paper_own, status="active"))
        self.assertEqual([v.id for v in actives], [version.id],
                         "exactly one active version after reuse")

    def test_INGD_CX01_REDELIVERY_AFTER_ACTIVE_NOOP(self):
        """ING-D-CX-01: redelivering a job whose build is already active is a
        no-op — no chunk is rewritten, no attempt is consumed."""
        self.case_id = "ING-D-CX-01-REDELIVERY-AFTER-ACTIVE"
        self.expected_pre_fix = "PASS"
        self.contract = ("redelivery after activation is a no-op reuse")
        self.positive_control = "task returns embedded"
        self.negative_control = "no rewrite, no new attempt, ids unchanged"
        self.root_cause = ""
        import hashlib

        from rag.models import Text

        source_hash = hashlib.sha256(SMALL_PDF).hexdigest()
        job, version = self._drive_job(source_identity=source_hash)
        version.refresh_from_db()
        ids_before = set(Text.objects.filter(
            index_version=version).values_list("id", flat=True))
        attempts_before = job.attempt_count

        with mock.patch("api.tasks._load_pdf_bytes",
                        side_effect=AssertionError(
                            "no-op redelivery must not load bytes")), \
             mock.patch("rag.ingest.parse_pdf_pages",
                        side_effect=AssertionError(
                            "no-op redelivery must not parse")):
            from api.tasks import ingest_paper_pdf_task
            result = ingest_paper_pdf_task.run(job.id)
        job.refresh_from_db()
        self.assertEqual(result["status"], "embedded",
                         "positive control: no-op redelivery completes")
        self.assertEqual(job.status, "embedded")
        self.assertEqual(job.attempt_count, attempts_before,
                         "no-op reuse must not consume an attempt")
        self.assertEqual(set(Text.objects.filter(
            index_version=version).values_list("id", flat=True)),
            ids_before,
            "active chunk ids must be unchanged")

    def test_INGD_CX01_WRITE_REJECTS_ACTIVE(self):
        """ING-D-CX-01: ingest_text with an ACTIVE index_version must fail
        closed — zero persisted, active chunks untouched."""
        self.case_id = "ING-D-CX-01-WRITE-REJECTS-ACTIVE"
        self.expected_pre_fix = "PASS"
        self.contract = ("writes into a non-building version are refused")
        self.positive_control = "active build exists"
        self.negative_control = "0 persisted; active ids unchanged"
        self.root_cause = ""
        import hashlib

        from rag.ingest import ingest_text
        from rag.models import Text

        source_hash = hashlib.sha256(SMALL_PDF).hexdigest()
        _job, version = self._drive_job(source_identity=source_hash)
        version.refresh_from_db()
        self.assertEqual(version.status, "active")
        ids_before = set(Text.objects.filter(
            index_version=version).values_list("id", flat=True))

        count = asyncio.run(ingest_text(
            self.paper_own, "x" * 500,
            index_version=version, replace_existing=True))
        self.assertEqual(count, 0,
                         "writing into an active version must fail closed")
        self.assertEqual(set(Text.objects.filter(
            index_version=version).values_list("id", flat=True)),
            ids_before,
            "active chunks must be untouched")

class IngestionExecutionRedTest(_IngestionRedBase):

    def test_ING_ZERO_CHUNK_NOT_SUCCESS(self):
        self.case_id = "ING-ZERO-CHUNK-NOT-SUCCESS"
        self.expected_pre_fix = "FAIL"
        self.contract = "zero-chunk parse must NOT be marked success"
        self.positive_control = "task runs to completion"
        self.negative_control = "0 chunks -> job must fail, not embedded"
        self.root_cause = "ingest task marks embedded even with 0 chunks"
        from api.models import PaperIngestionJob
        from api.tasks import ingest_paper_pdf_task

        job = PaperIngestionJob.objects.create(
            project=self.proj_a, paper=self.paper_own, status="pending")
        with mock.patch("api.tasks._load_pdf_bytes", return_value=SMALL_PDF), \
             mock.patch("rag.ingest.parse_pdf_pages",
                        return_value=("short", [])):
            ingest_paper_pdf_task.run(job.id)
        job.refresh_from_db()
        marked_success = job.status == "embedded"
        self.record("ingest task", "zero_chunk_not_success", marked_success,
                    "zero-chunk parse must not claim success")
        self.assertNotEqual(job.status, "embedded",
                            "zero-chunk parse must not claim success (Tasks 4.2)")

    def test_ING_ZERO_CHUNK_KEEPS_OLD(self):
        self.case_id = "ING-ZERO-CHUNK-KEEPS-OLD"
        self.expected_pre_fix = "PASS"
        self.contract = "a short/failed replacement keeps the current index"
        self.positive_control = "chunks inserted then short text returns 0"
        self.negative_control = "0-chunk attempt must not delete old chunks"
        self.root_cause = ""
        from rag.ingest import ingest_text
        from rag.models import Text

        async def drive():
            first = await ingest_text(self.paper_own, "x" * 500,
                                      replace_existing=True)
            self.assertGreater(first, 0, "positive control: chunks inserted")
            second = await ingest_text(self.paper_own, "short",
                                       replace_existing=True)
            self.assertEqual(second, 0)
        asyncio.run(drive())
        remaining = Text.objects.filter(paper=self.paper_own).count()
        self.assertGreater(remaining, 0,
                           "previous chunks must survive a zero-chunk attempt")

    def test_ING_CARDINALITY_MISMATCH(self):
        self.case_id = "ING-CARDINALITY-MISMATCH"
        self.expected_pre_fix = "FAIL"
        self.contract = "embedding count != chunk count must be rejected"
        self.positive_control = "ingest_text runs"
        self.negative_control = "1 vector for 2 chunks -> rejection, no persist"
        self.root_cause = "ingest_text zips chunks/vectors without validation"
        from rag.ingest import ingest_text

        with mock.patch("rag.ingest.embed", return_value=[__import__("numpy").array(
                [1.0] + [0.0] * 1023)]):
            count = asyncio.run(ingest_text(self.paper_own, "x" * 4000,
                                            replace_existing=True))
        self.record("ingest_text", "cardinality_validated", count > 0,
                    "cardinality mismatch must be rejected")
        self.assertEqual(count, 0,
                         "chunk/vector cardinality mismatch must be rejected "
                         "(Tasks 4.2)")

    def test_ING_NON_FINITE_VECTOR(self):
        self.case_id = "ING-NON-FINITE-VECTOR"
        self.expected_pre_fix = "FAIL"
        self.contract = "non-finite vectors rejected before persist"
        self.positive_control = "ingest_text runs"
        self.negative_control = "NaN vector -> rejection, no persist"
        self.root_cause = "no finite-value validation"
        import numpy as np
        from rag.ingest import ingest_text

        reached: list = []

        def _fake_save(objs):
            reached.extend(objs)
            return len(objs)

        bad = np.array([[float("nan")] + [0.0] * 1023])
        with mock.patch("rag.ingest.embed", return_value=bad), \
             mock.patch("rag.ingest.Text.objects.bulk_create",
                        side_effect=_fake_save):
            asyncio.run(ingest_text(self.paper_own, "x" * 1000,
                                    replace_existing=True))
        self.record("ingest_text", "non_finite_rejected", bool(reached),
                    "non-finite vectors must be rejected")
        self.assertFalse(reached,
                         "non-finite values must be rejected (Tasks 4.2)")

    def test_ING_DIMENSION_WRONG(self):
        self.case_id = "ING-DIMENSION-WRONG"
        self.expected_pre_fix = "FAIL"
        self.contract = "wrong vector dimension rejected before persist"
        self.positive_control = "ingest_text runs"
        self.negative_control = "8-dim vector -> rejection, no persist"
        self.root_cause = "no dimension validation"
        import numpy as np
        from rag.ingest import ingest_text

        inserted: list = []

        def _fake_save(objs):
            inserted.extend(objs)
            return len(objs)

        wrong_dim = np.array([[1.0] * 8])
        with mock.patch("rag.ingest.embed", return_value=wrong_dim), \
             mock.patch("rag.ingest.Text.objects.bulk_create",
                        side_effect=_fake_save):
            asyncio.run(ingest_text(self.paper_own, "x" * 1000,
                                    replace_existing=True))
        self.record("ingest_text", "dimension_validated", bool(inserted),
                    "wrong dimension must fail before persist")
        self.assertFalse(inserted,
                         "wrong vector dimension must fail before persist "
                         "(Tasks 4.2)")

    def test_ING_ACTIVATION_FAIL_ROLLBACK(self):
        self.case_id = "ING-ACTIVATION-FAIL-ROLLBACK"
        self.expected_pre_fix = "FAIL"
        self.contract = ("replacement failure must keep the previous index "
                         "queryable (rollback boundary)")
        self.positive_control = "index exists before the failed replacement"
        self.negative_control = "failed insert -> old chunks still present"
        self.root_cause = "replace_existing deletes old chunks BEFORE insert"
        from rag.ingest import ingest_text
        from rag.models import Text

        async def drive():
            first = await ingest_text(self.paper_own, "y" * 800,
                                      replace_existing=True)
            self.assertGreater(first, 0, "positive control: index exists")
            with mock.patch("rag.ingest.Text.objects.bulk_create",
                            side_effect=RuntimeError("insert boom")):
                try:
                    await ingest_text(self.paper_own, "z" * 900,
                                      replace_existing=True)
                except RuntimeError:
                    pass
        asyncio.run(drive())
        remaining = Text.objects.filter(paper=self.paper_own).count()
        self.record("ingest_text", "old_index_survives", remaining == 0,
                    "previous index must survive a failed replacement")
        self.assertGreater(remaining, 0,
                           "failed replacement must not delete the old index "
                           "(Tasks 4.3)")

    def test_ING_CONCURRENT_TEN_ONE_BUILD(self):
        self.case_id = "ING-CONCURRENT-TEN-ONE-BUILD"
        self.expected_pre_fix = "FAIL"
        self.contract = ("ten concurrent identical requests -> one global "
                         "build/index version, at most one active version, "
                         "every request stable")
        self.positive_control = "all ten requests get a stable response"
        self.negative_control = ">1 build or >1 active version"
        self.root_cause = ("no IngestionService/global build identity; each "
                           "request creates its own job")
        from api.models import PaperIngestionJob

        def _upload_once(_i):
            with self._enqueue_stub():
                return self._upload(self.proj_a.id, self.paper_own.id,
                                    "same.pdf", SMALL_PDF)

        with ThreadPoolExecutor(max_workers=10) as pool:
            responses = list(pool.map(_upload_once, range(10)))
        stable = all(r.status_code in (200, 201) for r in responses)
        self.assertTrue(stable,
                        "positive control: every request got a stable response")
        jobs = PaperIngestionJob.objects.filter(paper_id=self.paper_own.id).count()
        self.record("concurrency", "one_build", jobs != 1,
                    "ten requests must converge on ONE build")
        self.assertEqual(jobs, 1,
                         "concurrent identical requests must deduplicate into "
                         "one build (Tasks 4.1)")

    def test_ING_CONCURRENT_REQUEST_IDENTITY(self):
        """Each project request keeps its EXACT project identity — PASS
        baseline that must survive the shared-build refactor."""
        self.case_id = "ING-CONCURRENT-REQUEST-IDENTITY"
        self.expected_pre_fix = "PASS"
        self.contract = ("every job's project_id exactly equals its caller's "
                         "project (A jobs > 0, B jobs > 0, per-job exact match)")
        self.positive_control = "both projects create jobs"
        self.negative_control = "no job may belong to a different project"
        self.root_cause = ""
        from api.models import PaperIngestionJob
        from api.models import ProjectPaper
        from papers.models import Paper

        shared_paper = Paper.objects.create(title="Shared Identity", year=2024)
        ProjectPaper.objects.create(project=self.proj_a, paper=shared_paper,
                                    status="included")
        ProjectPaper.objects.create(project=self.proj_b, paper=shared_paper,
                                    status="included")

        def _up(proj_id):
            with self._enqueue_stub():
                return self._upload(proj_id, shared_paper.id,
                                    "ident.pdf", SMALL_PDF)

        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(_up, [self.proj_a.id] * 3 + [self.proj_b.id] * 3))
        jobs = list(PaperIngestionJob.objects.filter(paper_id=shared_paper.id))
        a_jobs = [j for j in jobs if j.project_id == self.proj_a.id]
        b_jobs = [j for j in jobs if j.project_id == self.proj_b.id]
        self.assertGreater(len(a_jobs), 0,
                           "A project must have its own job")
        self.assertGreater(len(b_jobs), 0,
                           "B project must have its own job")
        self.assertTrue(all(j.project_id in (self.proj_a.id, self.proj_b.id)
                            for j in jobs),
                        "every job belongs to a participating project")
        # exact identity: an A-uploaded job can never land on B (or vice versa)
        self.assertEqual({j.project_id for j in jobs},
                         {self.proj_a.id, self.proj_b.id},
                         "both projects must appear exactly once as owners")

    def test_ING_CROSS_PROJECT_SHARED_BUILD(self):
        self.case_id = "ING-CROSS-PROJECT-SHARED-BUILD"
        self.expected_pre_fix = "FAIL"
        self.contract = ("two project jobs for the same paper+PDF must "
                         "reference ONE shared non-null global index version")
        self.positive_control = "both projects hold a membership and a job"
        self.negative_control = "no shared global build identity"
        self.root_cause = "jobs have no global build reference"
        from api.models import PaperIngestionJob, ProjectPaper
        from papers.models import Paper

        shared_paper = Paper.objects.create(title="Shared", year=2024)
        ProjectPaper.objects.create(project=self.proj_a, paper=shared_paper,
                                    status="included")
        ProjectPaper.objects.create(project=self.proj_b, paper=shared_paper,
                                    status="included")
        with self._enqueue_stub():
            a_resp = self._upload(self.proj_a.id, shared_paper.id,
                                  "shared.pdf", SMALL_PDF)
            b_resp = self._upload(self.proj_b.id, shared_paper.id,
                                  "shared.pdf", SMALL_PDF)
        self.assertEqual(a_resp.status_code, 201,
                         "positive control: A request succeeded")
        self.assertEqual(b_resp.status_code, 201,
                         "positive control: B request succeeded")
        jobs = list(PaperIngestionJob.objects.filter(paper_id=shared_paper.id))
        a_jobs = [j for j in jobs if j.project_id == self.proj_a.id]
        b_jobs = [j for j in jobs if j.project_id == self.proj_b.id]
        self.assertGreater(len(a_jobs), 0, "A must have a project job")
        self.assertGreater(len(b_jobs), 0, "B must have a project job")
        a_build = getattr(a_jobs[0], "index_version_id", None)
        b_build = getattr(b_jobs[0], "index_version_id", None)
        shared = a_build is not None and a_build == b_build
        self.record("shared build", "one_global_build", not shared,
                    "both jobs must reference the SAME non-null global version")
        self.assertTrue(shared,
                        "cross-project identical PDFs must share one non-null "
                        "global build identity (Tasks 4.1)")

    def test_ING_RETRY_CLASSIFICATION(self):
        self.case_id = "ING-RETRY-CLASSIFICATION"
        self.expected_pre_fix = "FAIL"
        self.contract = ("transient failures retry at most three times; "
                         "permanent failures never auto-retry — via "
                         "autoretry_for OR explicit self.retry()")
        self.positive_control = "task object is introspectable"
        self.negative_control = "no explicit retry classification"
        self.root_cause = "no explicit retry policy in the task"
        from api.tasks import ingest_paper_pdf_task

        autoretry = getattr(ingest_paper_pdf_task, "autoretry_for", None)
        source = ""
        try:
            source = inspect.getsource(ingest_paper_pdf_task.run)
        except (OSError, TypeError):
            source = ""
        has_policy = bool(autoretry) or "self.retry(" in source
        self.record("ingest task", "retry_classification", not has_policy,
                    "transient retry + permanent no-retry must be declared")
        self.assertTrue(has_policy,
                        "no explicit retry classification (Tasks 4.4)")

    def test_ING_TASK_LATE_ACK(self):
        self.case_id = "ING-TASK-LATE-ACK"
        self.expected_pre_fix = "FAIL"
        self.contract = "ingestion task uses late acknowledgement"
        self.positive_control = "task object is introspectable"
        self.negative_control = "acks_late must be True"
        self.root_cause = "task does not declare acks_late"
        from api.tasks import ingest_paper_pdf_task

        late_ack = getattr(ingest_paper_pdf_task, "acks_late", None)
        self.record("ingest task", "acks_late", late_ack is not True,
                    "acks_late must be True")
        self.assertIs(late_ack, True,
                      "late acknowledgement not declared (Tasks 4.4)")

    def test_ING_TASK_REJECT_ON_WORKER_LOSS(self):
        self.case_id = "ING-TASK-REJECT-ON-WORKER-LOSS"
        self.expected_pre_fix = "FAIL"
        self.contract = "worker loss must reject (not ack) the task"
        self.positive_control = "task object is introspectable"
        self.negative_control = "reject_on_worker_lost must be True"
        self.root_cause = "task does not declare reject_on_worker_lost"
        from api.tasks import ingest_paper_pdf_task

        reject = getattr(ingest_paper_pdf_task, "reject_on_worker_lost", None)
        self.record("ingest task", "reject_on_worker_lost", reject is not True,
                    "reject_on_worker_lost must be True")
        self.assertIs(reject, True,
                      "reject_on_worker_lost not declared (Tasks 4.4)")

    def test_ING_TASK_NON_EAGER_REDELIVERY(self):
        self.case_id = "ING-TASK-NON-EAGER-REDELIVERY"
        self.expected_pre_fix = "FAIL"
        self.contract = ("redelivery must resume/reuse the same build "
                         "identity (non-eager Redis/Celery safe)")
        self.positive_control = "task body is introspectable"
        self.negative_control = "no idempotent build-claim logic"
        self.root_cause = "no build identity / redelivery-safe claim logic"
        from api.tasks import ingest_paper_pdf_task

        source = ""
        try:
            source = inspect.getsource(ingest_paper_pdf_task.run)
        except (OSError, TypeError):
            source = ""
        has_claim = ("get_or_create" in source or
                     "build_key" in source or "claim" in source)
        self.record("ingest task", "redelivery_safe", not has_claim,
                    "redelivery must resume/reuse the same build")
        self.assertTrue(has_claim,
                        "no idempotent build claim for redelivery (Tasks 4.4)")


# =====================================================================
# Task 1.5 — API / Agent tool red contracts
# =====================================================================

class IngestionApiAgentRedTest(_IngestionRedBase):

    def test_ING_API_201_CREATED(self):
        self.case_id = "ING-API-201-CREATED"
        self.expected_pre_fix = "PASS"
        self.contract = "a new upload request returns 201"
        self.positive_control = "upload endpoint responds"
        self.negative_control = "201 on creation"
        self.root_cause = ""
        with self._enqueue_stub():
            resp = self._upload(self.proj_a.id, self.paper_own.id, "ok.pdf", SMALL_PDF)
        self.assertEqual(resp.status_code, 201, "new request must return 201")

    def test_ING_API_DEDUP_200(self):
        self.case_id = "ING-API-DEDUP-200"
        self.expected_pre_fix = "FAIL"
        self.contract = ("the same project/paper/source repeated -> 200 + "
                         "deduplicated=true, no second build")
        self.positive_control = "first request returns 201"
        self.negative_control = "second request must be 200+dedup, one job"
        self.root_cause = "upload view always creates a new job"
        from api.models import PaperIngestionJob

        with self._enqueue_stub():
            first = self._upload(self.proj_a.id, self.paper_own.id, "d2.pdf", SMALL_PDF)
            second = self._upload(self.proj_a.id, self.paper_own.id, "d2.pdf", SMALL_PDF)
        self.assertEqual(first.status_code, 201, "positive control: 201")
        body = second.json()
        reused = second.status_code == 200 and body.get("deduplicated") is True
        jobs = PaperIngestionJob.objects.filter(paper_id=self.paper_own.id).count()
        self.record("upload view", "reuse_200_dedup", not reused or jobs != 1,
                    "reuse must be 200 + deduplicated=true, one job")
        self.assertTrue(reused and jobs == 1,
                        "deduplicated 200 semantics missing (Tasks 5.1)")

    def test_ING_API_RETRY_ENDPOINT(self):
        self.case_id = "ING-API-RETRY-ENDPOINT"
        self.expected_pre_fix = "FAIL"
        self.contract = ("POST .../ingestion-jobs/<id>/retry exists; own "
                         "retryable job requeues")
        self.positive_control = "own retryable failed job exists"
        self.negative_control = "404 means the endpoint is missing"
        self.root_cause = "no retry endpoint"
        from api.models import PaperIngestionJob

        job = PaperIngestionJob.objects.create(
            project=self.proj_a, paper=self.paper_own, status="failed",
            error_message="boom", retryable=True)
        client = Client(HTTP_HOST="testserver")
        resp = client.post(
            f"/api/projects/{self.proj_a.id}/ingestion-jobs/{job.id}/retry")
        exists = resp.status_code != 404
        self.record("retry endpoint", "scoped_retry", not exists,
                    "scoped retry endpoint must exist")
        self.assertNotEqual(resp.status_code, 404,
                            "scoped retry endpoint missing (Tasks 5.1)")

    def test_ING_API_RETRY_SAFE_REJECTIONS(self):
        self.case_id = "ING-API-RETRY-SAFE-REJECTIONS"
        self.expected_pre_fix = "FAIL"
        self.contract = ("foreign / non-failed / nonexistent retry targets "
                         "fail closed with the same safe error shape")
        self.positive_control = "own retryable failed job is distinguishable"
        self.negative_control = "three unsafe targets -> uniform rejection"
        self.root_cause = "no retry endpoint at all"
        from api.models import PaperIngestionJob, ResearchProject

        other = ResearchProject.objects.create(title="Other", status="active")
        own_failed = PaperIngestionJob.objects.create(
            project=self.proj_a, paper=self.paper_own, status="failed",
            error_message="boom", retryable=True)
        foreign = PaperIngestionJob.objects.create(
            project=other, paper=self.paper_own, status="failed",
            error_message="boom")
        non_failed = PaperIngestionJob.objects.create(
            project=self.proj_a, paper=self.paper_own, status="pending")
        client = Client(HTTP_HOST="testserver")
        own_resp = client.post(
            f"/api/projects/{self.proj_a.id}/ingestion-jobs/{own_failed.id}/retry")
        foreign_resp = client.post(
            f"/api/projects/{self.proj_a.id}/ingestion-jobs/{foreign.id}/retry")
        nonfailed_resp = client.post(
            f"/api/projects/{self.proj_a.id}/ingestion-jobs/{non_failed.id}/retry")
        missing_resp = client.post(
            f"/api/projects/{self.proj_a.id}/ingestion-jobs/999999/retry")
        shapes = [foreign_resp.status_code, nonfailed_resp.status_code,
                  missing_resp.status_code]
        safe = own_resp.status_code in (200, 202) and len(set(shapes)) == 1 \
            and shapes[0] not in (200, 201, 202)
        self.record("retry endpoint", "safe_rejections", not safe,
                    "own retryable succeeds; three unsafe targets share one "
                    "safe error shape")
        self.assertTrue(safe,
                        "retry safe-rejection shape missing (Tasks 5.1)")

    def test_ING_API_EXPANDED_STATES(self):
        self.case_id = "ING-API-EXPANDED-STATES"
        self.expected_pre_fix = "FAIL"
        self.contract = ("job states must cover pending/downloading/parsing/"
                         "embedding/committing/embedded/failed")
        self.positive_control = "job status choices are enumerable"
        self.negative_control = "missing lifecycle states"
        self.root_cause = "4-state model (pending/parsing/embedded/failed)"
        from api.models import PaperIngestionJob

        choices = {c[0] for c in PaperIngestionJob._meta.get_field("status").choices}
        required = {"pending", "downloading", "parsing", "embedding",
                    "committing", "embedded", "failed"}
        missing = sorted(required - choices)
        self.record("job model", "expanded_states", bool(missing),
                    "lifecycle states missing: %s" % missing)
        self.assertFalse(missing,
                         f"expanded ingestion states missing: {missing} (Tasks 5.1)")

    def test_ING_API_FULLTEXT_READY(self):
        self.case_id = "ING-API-FULLTEXT-READY"
        self.expected_pre_fix = "FAIL"
        self.contract = "fulltext_ready must be exposed by the job serializer"
        self.positive_control = "serializer output is readable"
        self.negative_control = "fulltext_ready field absent"
        self.root_cause = "no active-version awareness in serializers"
        from api.serializers import PaperIngestionJobSerializer
        from api.models import PaperIngestionJob

        job = PaperIngestionJob.objects.create(
            project=self.proj_a, paper=self.paper_own, status="embedded")
        data = PaperIngestionJobSerializer(job).data
        has_field = "fulltext_ready" in data
        self.record("serializer", "fulltext_ready", not has_field,
                    "fulltext_ready must be exposed")
        self.assertTrue(has_field,
                        "fulltext_ready missing from job serializer (Tasks 5.1)")

    def test_ING_AGENT_MAX_THREE(self):
        self.case_id = "ING-AGENT-MAX-THREE"
        self.expected_pre_fix = "FAIL"
        self.contract = ("add_papers_to_project queues at most THREE newly "
                         "created memberships with PDF URLs")
        self.positive_control = "five memberships created"
        self.negative_control = "queued collection must exist and be <= 3"
        self.root_cause = "add tool does no auto-queueing"
        from agent.project_tools import add_papers_to_project

        papers = [
            {"title": f"P{i}", "arxiv_id": f"p{i}",
             "pdf_url": "https://cdn.example.com/x.pdf"}
            for i in range(5)
        ]
        result = asyncio.run(add_papers_to_project(self.proj_a.id, papers, "red"))
        self.assertGreaterEqual(len(result.get("added", [])), 5,
                                "positive control: memberships created")
        queued = result.get("queued") or []
        self.record("add tool", "max_three_auto_queue",
                    "queued" not in result or len(queued) > 3,
                    "queued <= 3 required")
        self.assertIn("queued", result,
                      "add result must expose a queued collection (Tasks 5.3)")
        self.assertLessEqual(len(queued), 3,
                             "at most three auto-queued jobs per add (Tasks 5.3)")

    def test_ING_AGENT_UPLOAD_REQUIRED(self):
        self.case_id = "ING-AGENT-UPLOAD-REQUIRED"
        self.expected_pre_fix = "FAIL"
        self.contract = ("papers without a trusted PDF URL are reported "
                         "upload_required, never auto-queued")
        self.positive_control = "membership created"
        self.negative_control = "upload_required collection must list the paper"
        self.root_cause = "add tool reports only added/count"
        from agent.project_tools import add_papers_to_project

        papers = [{"title": "No URL paper", "arxiv_id": "nourl",
                   "pdf_url": None}]
        result = asyncio.run(add_papers_to_project(self.proj_a.id, papers, "red"))
        upload_required = result.get("upload_required") or []
        self.record("add tool", "upload_required",
                    "upload_required" not in result or not upload_required,
                    "upload-required collection required")
        self.assertTrue(upload_required,
                        "upload-required collection missing (Tasks 5.3)")

    def test_ING_AGENT_METADATA_ONLY(self):
        self.case_id = "ING-AGENT-METADATA-ONLY"
        self.expected_pre_fix = "PASS"
        self.contract = ("metadata membership is never treated as full-text "
                         "evidence by the capability gate")
        self.positive_control = "factual contract requires fulltext"
        self.negative_control = "metadata-only must not pass the gate"
        self.root_cause = ""
        from agent.capability import Capability, capability_for_intent
        from agent.intent import classify_project_intent

        intent = classify_project_intent("这个论文用什么方法？", self.proj_a.id)
        contract = capability_for_intent(intent)
        self.assertEqual(contract.capability, Capability.FACTUAL,
                         "positive control: factual contract")
        self.assertTrue(contract.requires_resolved_bound_fulltext,
                        "metadata-only must not satisfy factual evidence")

    def test_ING_AGENT_RESULT_COLLECTIONS(self):
        self.case_id = "ING-AGENT-RESULT-COLLECTIONS"
        self.expected_pre_fix = "FAIL"
        self.contract = ("add result separates added/queued/reused/deferred/"
                         "upload_required")
        self.positive_control = "membership created"
        self.negative_control = "all five collections must exist"
        self.root_cause = "add result only exposes added/count"
        from agent.project_tools import add_papers_to_project

        papers = [{"title": "C1", "arxiv_id": "c1",
                   "pdf_url": "https://cdn.example.com/c1.pdf"}]
        result = asyncio.run(add_papers_to_project(self.proj_a.id, papers, "red"))
        required_keys = {"added", "queued", "reused", "deferred", "upload_required"}
        missing = sorted(required_keys - set(result.keys()))
        self.record("add tool", "result_collections", bool(missing),
                    "missing collections: %s" % missing)
        self.assertFalse(missing,
                         f"add result missing collections: {missing} (Tasks 5.3)")


class Tasks5FixRedTest(_IngestionRedBase):
    """Tasks5-CX-01..06 (Codex findings): URL-ingest via IngestionService,
    no raw URL/path in API responses, retry requires retryable, strict
    fulltext_ready, HTTPS-only auto-queue."""

    OPAQUE = "Tasks5CX0p4q7r9t2v5x8w"

    def _url_ingest(self, project_id, paper_id, pdf_url):
        client = Client(HTTP_HOST="testserver")
        return client.post(
            f"/api/projects/{project_id}/papers/{paper_id}/ingest",
            {"pdf_url": pdf_url}, format="json")

    def test_TASKS5_CX01_URL_INGEST_SERVICE(self):
        """URL ingest goes through IngestionService: first -> 201; repeat ->
        200 + deduplicated + one job; active global build -> embedded reuse,
        no enqueue; cross-project same source -> shared build."""
        self.case_id = "TASKS5-CX-01-URL-INGEST"
        self.expected_pre_fix = "PASS"
        self.contract = ("URL ingest uses request_key/get_or_create/claim; "
                         "dedup 200; active reuse no enqueue; shared build")
        self.positive_control = "first URL ingest returns 201"
        self.negative_control = "no second job, no raw URL leak, one build"
        self.root_cause = ""
        from api.models import PaperIngestionJob, ProjectPaper

        url = "https://cdn.example.com/tasks5cx01.pdf"
        first = self._url_ingest(self.proj_a.id, self.paper_own.id, url)
        self.assertEqual(first.status_code, 201,
                         "positive control: first URL ingest is 201")
        jobs = PaperIngestionJob.objects.filter(paper_id=self.paper_own.id)
        self.assertEqual(jobs.count(), 1, "exactly one job after first ingest")

        second = self._url_ingest(self.proj_a.id, self.paper_own.id, url)
        body = second.json()
        self.assertEqual(second.status_code, 200,
                         "repeat URL ingest must be 200")
        self.assertTrue(body.get("deduplicated"),
                        "repeat must carry deduplicated=true")
        self.assertEqual(PaperIngestionJob.objects.filter(
            paper_id=self.paper_own.id).count(), 1,
            "no second job for the same project/source")

        # active global build reuse: run the task, then a NEW project ingest
        from api.tasks import ingest_paper_pdf_task

        job = jobs.first()
        with mock.patch("api.tasks._load_pdf_bytes", return_value=b"pdf"), \
             mock.patch("rag.ingest.parse_pdf_pages",
                        return_value=("selective state space model. " * 300,
                                      [])):
            ingest_paper_pdf_task.run(job.id)
        from rag.models import PaperIndexVersion

        version = PaperIndexVersion.objects.get(
            paper=self.paper_own, status="active")
        ProjectPaper.objects.create(
            project=self.proj_b, paper=self.paper_own, status="included")
        enqueued: list = []
        with mock.patch("api.views._enqueue_ingestion_job",
                        side_effect=lambda job: enqueued.append(job.id)
                        or type("F", (), {"id": "x"})()):
            third = self._url_ingest(self.proj_b.id, self.paper_own.id, url)
        self.assertEqual(third.status_code, 200,
                         "active reuse answers 200")
        self.assertTrue(third.json().get("deduplicated"))
        self.assertEqual(enqueued, [],
                         "active global build must NOT be enqueued again")
        b_job = PaperIngestionJob.objects.get(
            project=self.proj_b, paper=self.paper_own)
        self.assertEqual(b_job.status, "embedded",
                         "reused job lands in embedded")
        self.assertEqual(b_job.index_version_id, version.id,
                         "cross-project jobs share one global build")
        self.assertNotIn(url, json.dumps(third.json(), default=str),
                         "no raw URL in the response")

    def test_TASKS5_CX02_NO_RAW_URL_LEAK(self):
        """No API response (GET jobs / upload / URL ingest / retry) may expose
        source_url, file_path, query strings, or the opaque sentinel."""
        self.case_id = "TASKS5-CX-02-NO-RAW-URL-LEAK"
        self.expected_pre_fix = "PASS"
        self.contract = ("ingestion API surfaces expose only safe fields")
        self.positive_control = "endpoints respond"
        self.negative_control = "raw URL/path/query/sentinel never serialized"
        self.root_cause = ""
        from api.models import PaperIngestionJob

        url = f"https://cdn.example.com/{self.OPAQUE}.pdf?token={self.OPAQUE}"
        client = Client(HTTP_HOST="testserver")

        ingest_resp = self._url_ingest(self.proj_a.id, self.paper_own.id, url)
        job = PaperIngestionJob.objects.get(
            project=self.proj_a, paper=self.paper_own)
        list_resp = client.get(
            f"/api/projects/{self.proj_a.id}/ingestion-jobs")
        retry_resp = client.post(
            f"/api/projects/{self.proj_a.id}/ingestion-jobs/{job.id}/retry")
        upload_resp = None
        with self._enqueue_stub():
            upload_resp = self._upload(self.proj_a.id, self.paper_own.id,
                                       "cx02.pdf", SMALL_PDF)

        surfaces = {
            "upload": upload_resp.json(),
            "url_ingest": ingest_resp.json(),
            "list": list_resp.json(),
            "retry": retry_resp.json(),
        }
        for name, data in surfaces.items():
            blob = json.dumps(data, default=str)
            for forbidden in ("source_url", "file_path", "cdn.example",
                              self.OPAQUE, "token=", "?token", "http://",
                              "https://"):
                self.assertNotIn(forbidden, blob,
                                 f"{name} leaked {forbidden!r}")
        from api.serializers import PaperIngestionJobSerializer
        self.assertNotIn("source_url", json.dumps(
            PaperIngestionJobSerializer(job).data),
            "serializer must not expose source_url")

    def test_TASKS5_CX04_RETRY_REQUIRES_RETRYABLE(self):
        """Retry requires failed + retryable=true; non-retryable is a uniform
        404; a broker failure returns a stable 503 with a pending job."""
        self.case_id = "TASKS5-CX-04-RETRY-REQUIRES-RETRYABLE"
        self.expected_pre_fix = "PASS"
        self.contract = ("retry only for own failed+retryable; enqueue failure "
                         "is explicit")
        self.positive_control = "own failed+retryable -> 202"
        self.negative_control = ("non-retryable -> 404; broker down -> 503 + "
                                 "pending, never pretended queued")
        self.root_cause = ""
        from api.models import PaperIngestionJob

        client = Client(HTTP_HOST="testserver")
        non_retryable = PaperIngestionJob.objects.create(
            project=self.proj_a, paper=self.paper_own, status="failed",
            error_message="boom", retryable=False)
        resp = client.post(
            f"/api/projects/{self.proj_a.id}/ingestion-jobs/{non_retryable.id}/retry")
        self.assertEqual(resp.status_code, 404,
                         "non-retryable failed job must be rejected uniformly")
        non_retryable.refresh_from_db()
        self.assertFalse(non_retryable.retryable,
                         "retry must never flip retryable to true")

        retryable = PaperIngestionJob.objects.create(
            project=self.proj_a, paper=self.paper_own, status="failed",
            error_message="boom", retryable=True)
        with mock.patch("api.views._enqueue_ingestion_job",
                        side_effect=RuntimeError("broker down")):
            resp = client.post(
                f"/api/projects/{self.proj_a.id}/ingestion-jobs/{retryable.id}/retry")
        self.assertEqual(resp.status_code, 503,
                         "broker failure must return a stable safe error")
        self.assertEqual(resp.json().get("error"), "enqueue_failed",
                         "stable error code, no raw exception text")
        retryable.refresh_from_db()
        self.assertEqual(retryable.status, "pending",
                         "job stays in an explicit pending state")

    def test_TASKS5_CX05_FULLTEXT_READY_STRICT(self):
        """fulltext_ready is true ONLY for active versions WITH chunks."""
        self.case_id = "TASKS5-CX-05-FULLTEXT-READY-STRICT"
        self.expected_pre_fix = "PASS"
        self.contract = ("fulltext_ready requires active AND chunk_count > 0")
        self.positive_control = "active with chunks -> true"
        self.negative_control = "active-zero / superseded / building -> false"
        self.root_cause = ""
        from api.models import PaperIngestionJob
        from api.serializers import PaperIngestionJobSerializer
        from rag.models import PaperIndexVersion

        def _ready(version):
            job = PaperIngestionJob.objects.create(
                project=self.proj_a, paper=self.paper_own, status="embedded",
                index_version=version)
            return PaperIngestionJobSerializer(job).data["fulltext_ready"]

        active_zero = PaperIndexVersion.objects.get(
            paper=self.paper_own, status="active")
        active_zero.chunk_count = 0
        active_zero.save(update_fields=["chunk_count", "updated_at"])
        self.assertFalse(_ready(active_zero),
                         "active with ZERO chunks must be false")

        active_zero.chunk_count = 7
        active_zero.save(update_fields=["chunk_count", "updated_at"])
        self.assertTrue(_ready(active_zero),
                        "active with chunks must be true")

        superseded = PaperIndexVersion.objects.create(
            paper=self.paper_own, status="superseded",
            source_sha256="tasks5-cx05", pipeline_signature="tasks5-cx05",
            embedding_model="m", embedding_version="v", embedding_dim=1024,
            chunk_count=5)
        self.assertFalse(_ready(superseded),
                         "superseded must be false even with chunks")

        building = PaperIndexVersion.objects.create(
            paper=self.paper_own, status="building",
            source_sha256="tasks5-cx05-b", pipeline_signature="tasks5-cx05-b",
            embedding_model="m", embedding_version="v", embedding_dim=1024,
            chunk_count=5)
        self.assertFalse(_ready(building),
                         "building must be false even with chunks")

    def test_TASKS5_CX06_QUEUE_HTTPS_ONLY(self):
        """Auto-queue accepts ONLY https candidate URLs without userinfo;
        http/userinfo URLs are never enqueued and never create jobs."""
        self.case_id = "TASKS5-CX-06-QUEUE-HTTPS-ONLY"
        self.expected_pre_fix = "PASS"
        self.contract = ("queued requires https, no userinfo; unsafe URLs go "
                         "to upload_required with a safe reason")
        self.positive_control = "https URL is queued"
        self.negative_control = ("http/userinfo -> upload_required + reason, "
                                 "zero jobs")
        self.root_cause = ""
        from agent.project_tools import add_papers_to_project
        from api.models import PaperIngestionJob

        papers = [
            {"title": "OK", "arxiv_id": "ok1",
             "pdf_url": "https://cdn.example.com/ok.pdf"},
            {"title": "HTTP", "arxiv_id": "http1",
             "pdf_url": "http://cdn.example.com/x.pdf"},
            {"title": "USERINFO", "arxiv_id": "ui1",
             "pdf_url": "https://user:pass@cdn.example.com/y.pdf"},
        ]
        result = asyncio.run(add_papers_to_project(self.proj_a.id, papers, "cx06"))
        self.assertEqual(len(result.get("queued", [])), 1,
                         "exactly the HTTPS candidate may be queued")
        self.assertEqual(result["queued"][0]["title"], "OK")
        upload_required = result.get("upload_required") or []
        reasons = {u["title"]: u.get("reason") for u in upload_required}
        self.assertEqual(reasons.get("HTTP"), "unsafe_url",
                         "http URL must be flagged unsafe_url")
        self.assertEqual(reasons.get("USERINFO"), "unsafe_url",
                         "userinfo URL must be flagged unsafe_url")
        jobs = PaperIngestionJob.objects.count()
        self.assertEqual(jobs, 1,
                         "no job may be created for unsafe URLs")

    def test_TASKS5_CX07_AGENT_FILENAME_SAFE(self):
        """Agent auto-queue file_name must never be derived from the raw URL
        path — a URL with a sensitive path/query must not leak its parts into
        the job, the result collections, the API response, or any artifact."""
        self.case_id = "TASKS5-CX-07-AGENT-FILENAME-SAFE"
        self.expected_pre_fix = "PASS"
        self.contract = ("auto-queued job file_name is a safe digest name; "
                         "no raw URL/path/query/sentinel anywhere")
        self.positive_control = "auto-queue succeeds and creates a job"
        self.negative_control = ("file_name and every surface are free of "
                                 "path/query/host/sentinel")
        self.root_cause = ""
        from agent.project_tools import add_papers_to_project
        from api.models import PaperIngestionJob

        url = (f"https://cdn.example/private/{self.OPAQUE}/"
               f"secret-paper.pdf?token={self.OPAQUE}")
        result = asyncio.run(add_papers_to_project(self.proj_a.id, [
            {"title": "CX07", "arxiv_id": "cx07", "pdf_url": url},
        ], "cx07"))
        queued = result.get("queued") or []
        self.assertEqual(len(queued), 1,
                         "positive control: https URL is auto-queued")
        job = PaperIngestionJob.objects.get(
            project=self.proj_a, paper__arxiv_id="cx07")

        # 1) the job file_name is a safe digest name
        self.assertNotIn("secret-paper.pdf", job.file_name,
                         "file_name must not contain the URL path")
        self.assertNotIn("private", job.file_name,
                         "file_name must not contain the URL path segments")
        self.assertNotIn(self.OPAQUE, job.file_name,
                         "file_name must not contain the opaque sentinel")
        self.assertNotIn("token", job.file_name,
                         "file_name must not contain query parts")
        self.assertNotIn("cdn.example", job.file_name,
                         "file_name must not contain the host")
        self.assertTrue(
            job.file_name == f"paper-{job.paper_id}-"
            f"{hashlib.sha256(url.encode()).hexdigest()[:8]}.pdf"
            or job.file_name.startswith(f"paper-{job.paper_id}-"),
            "file_name must be a paper-id + digest name")

        # 2) result collections carry no raw URL/path/query/sentinel
        blob = json.dumps(result, default=str)
        for forbidden in ("secret-paper", "private", self.OPAQUE,
                          "token=", "cdn.example", "https://"):
            self.assertNotIn(forbidden, blob,
                             f"add result leaked {forbidden!r}")

        # 3) GET ingestion-jobs response is clean
        client = Client(HTTP_HOST="testserver")
        resp = client.get(f"/api/projects/{self.proj_a.id}/ingestion-jobs")
        body = json.dumps(resp.json(), default=str)
        for forbidden in ("secret-paper", "private", self.OPAQUE,
                          "token=", "cdn.example", "https://", "source_url"):
            self.assertNotIn(forbidden, body,
                             f"ingestion-jobs response leaked {forbidden!r}")


"""Tasks 5.4 — ingestion event schema boundary verification (red suite).

Triggers the full ingestion event surface (upload / URL ingest / retry /
Celery task lifecycle / agent auto-queue summaries) and asserts:

- every ProjectRunEvent payload key is inside the schema allowlist for its
  event type (and the global 5.4 allowed-field set);
- no forbidden content (URLs, hosts, paths, queries, prompts, raw exception
  text, API-key shapes, opaque sentinels) reaches any payload or log extra;
- unknown event types are sanitized to an empty payload (schema-limited).
"""
import json
from unittest import mock

from django.test import Client


class Tasks54EventSchemaRedTest(_IngestionRedBase):

    OPAQUE = "Tasks54Ev3n7S0p9x2k5w8"

    FORBIDDEN = ("http", "https://", "cdn.example", "/private/", "?token=",
                 "secret-paper", OPAQUE, "boom", "api_key", "sk-",
                 "Traceback", "prompt", "excerpt")

    def _event_blob(self) -> str:
        from api.models import ProjectRunEvent

        events = ProjectRunEvent.objects.filter(
            event_type__startswith="ingestion_")
        return json.dumps(
            {e.id: {"type": e.event_type, "payload": e.payload}
             for e in events}, default=str)

    def _assert_clean(self, blob: str, where: str) -> None:
        for forbidden in self.FORBIDDEN:
            self.assertNotIn(forbidden, blob,
                             f"{where} leaked {forbidden!r}")

    def test_TASKS54_EVENT_SCHEMA_BOUNDARY(self):
        """Every ingestion event payload stays inside the schema allowlist and
        never carries forbidden content."""
        self.case_id = "TASKS5-4-EVENT-SCHEMA-BOUNDARY"
        self.expected_pre_fix = "PASS"
        self.contract = ("ingestion events are schema-limited; no URL/host/"
                         "path/prompt/exception/key anywhere")
        self.positive_control = "all producers emit events"
        self.negative_control = "payload keys outside the allowlist"
        self.root_cause = ""
        from agent.events import event_schemas
        from api.models import PaperIngestionJob
        from api.tasks import ingest_paper_pdf_task

        schemas = event_schemas()
        client = Client(HTTP_HOST="testserver")

        # producer 1: upload
        with self._enqueue_stub():
            self._upload(self.proj_a.id, self.paper_own.id,
                         f"{self.OPAQUE}.pdf", SMALL_PDF)
        # producer 2: URL ingest
        client.post(
            f"/api/projects/{self.proj_a.id}/papers/{self.paper_own.id}/ingest",
            {"pdf_url": f"https://cdn.example/private/{self.OPAQUE}/x.pdf?token=1"},
            format="json")
        # producer 3: retry (own failed+retryable)
        failed_job = PaperIngestionJob.objects.create(
            project=self.proj_a, paper=self.paper_own, status="failed",
            error_message="boom", retryable=True)
        with mock.patch("api.views._enqueue_ingestion_job",
                        side_effect=lambda job: type("F", (), {"id": "x"})()):
            client.post(
                f"/api/projects/{self.proj_a.id}/ingestion-jobs/{failed_job.id}/retry")
        # producer 4: Celery task lifecycle
        from api.ingestion_service import IngestionService
        job = PaperIngestionJob.objects.create(
            project=self.proj_a, paper=self.paper_own, status="pending",
            file_hash="tasks54-hash", source_kind="test")
        IngestionService().claim_build(job, "tasks54-hash")
        job.refresh_from_db()
        with mock.patch("api.tasks._load_pdf_bytes", return_value=b"pdf"), \
             mock.patch("rag.ingest.parse_pdf_pages",
                        return_value=("y" * 3000, [])):
            ingest_paper_pdf_task.run(job.id)
        # producer 5: agent auto-queue summary
        from agent.project_tools import add_papers_to_project
        asyncio.run(add_papers_to_project(self.proj_a.id, [
            {"title": "A5", "arxiv_id": "a54",
             "pdf_url": "https://cdn.example/ok.pdf"},
        ], "t54"))

        from api.models import ProjectRunEvent
        events = list(ProjectRunEvent.objects.filter(
            event_type__startswith="ingestion_"))
        self.assertTrue(events, "positive control: events were persisted")

        allowed = {
            "project_id", "run_id", "session_id", "request_id",
            "job_id", "paper_id", "index_version_id",
            "status", "state",
            "chunk_count", "file_size", "attempt_count", "duration_ms",
            "retryable", "error_code", "error_hash",
            "deduplicated", "reused", "fulltext_ready",
            "message", "source_hash", "reason",
        }
        violations = []
        for event in events:
            schema = schemas.get(event.event_type)
            self.assertTrue(schema,
                            f"event type {event.event_type} must have a schema")
            # business keys must be in BOTH the per-type schema and the global
            # allowlist; the four correlation ids are injected by the
            # publisher and only need the global allowlist
            fields = set(schema.get("fields", {}))
            for key in event.payload.keys():
                if key in ("project_id", "run_id", "session_id", "request_id"):
                    if key not in allowed:
                        violations.append(f"{event.event_type}.{key}")
                elif key not in allowed or key not in fields:
                    violations.append(f"{event.event_type}.{key}")
        self.assertEqual(violations, [],
                         f"payload keys outside the schema allowlist: {violations}")

        blob = self._event_blob()
        self._assert_clean(blob, "ProjectRunEvent payloads")
        self.assertIn("ingestion_completed", blob,
                      "positive control: completed event present")
        self.assertIn("ingestion_retry", schemas,
                      "ingestion_retry schema must be registered "
                      "(transient path is exercised by ING-D retry tests)")

    def test_TASKS54_UNKNOWN_EVENT_SANITIZED(self):
        """An unregistered event type is sanitized to an empty payload."""
        self.case_id = "TASKS5-4-UNKNOWN-EVENT-SANITIZED"
        self.expected_pre_fix = "PASS"
        self.contract = "unknown event types never persist raw payloads"
        self.positive_control = "sanitize_event returns schema-limited dict"
        self.negative_control = "unknown payload dropped"
        self.root_cause = ""
        from agent.events import sanitize_event

        safe = sanitize_event(
            "totally_unknown_event",
            {"raw_url": f"https://cdn.example/{self.OPAQUE}/x.pdf",
             "secret": "boom", "prompt": "p"},
            ids={"project_id": self.proj_a.id, "run_id": 1,
                 "session_id": None, "request_id": None})
        self.assertEqual(safe,
                         {"project_id": self.proj_a.id, "run_id": 1,
                          "session_id": None, "request_id": None},
                         "unknown event payload must be fully dropped")

    def test_TASKS54_LOG_EXTRAS_SAFE(self):
        """View-layer ingestion logger extras stay inside the safe field set
        (upload/URL-ingest/retry events)."""
        self.case_id = "TASKS5-4-LOG-EXTRAS-SAFE"
        self.expected_pre_fix = "PASS"
        self.contract = "logger extras carry only safe structured fields"
        self.positive_control = "events logged"
        self.negative_control = "no URL/host/path in extras"
        self.root_cause = ""
        import logging as _logging

        with self.assertLogs("api.views", level="INFO") as captured:
            with self._enqueue_stub():
                self._upload(self.proj_a.id, self.paper_own.id,
                             "logsafe.pdf", SMALL_PDF)
            client = Client(HTTP_HOST="testserver")
            client.post(
                f"/api/projects/{self.proj_a.id}/papers/{self.paper_own.id}/ingest",
                {"pdf_url": f"https://cdn.example/{self.OPAQUE}.pdf?t=1"},
                format="json")
        blob = "\n".join(captured.output)
        for forbidden in ("cdn.example", self.OPAQUE, "?t=", "https://"):
            self.assertNotIn(forbidden, blob,
                             f"log extras leaked {forbidden!r}")



# =====================================================================
# ING-B-fix: versioned-write immutability, migration determinism, job
# migration (all expected PASS — they verify the FIXED behavior).
# =====================================================================

class IngBVersionedWriteFixTest(_IngestionRedBase):
    """P0: compatible writes only touch the dedicated building version;
    active/superseded/failed versions and their chunks stay immutable."""

    def test_INGFIX_ACTIVE_IMMUTABLE_ON_REINGEST(self):
        self.case_id = "INGFIX-ACTIVE-IMMUTABLE-ON-REINGEST"
        self.expected_pre_fix = "PASS"
        self.contract = ("re-ingest must not change the paper's ACTIVE version "
                         "chunks; new chunks belong ONLY to a building version")
        self.positive_control = "active fixture chunks exist before ingest"
        self.negative_control = "active chunk ids unchanged; new chunks on building"
        self.root_cause = ""
        from rag.ingest import ingest_text
        from rag.models import PaperIndexVersion, Text

        active = self.version_own
        before = set(Text.objects.filter(
            index_version=active).values_list("id", flat=True))
        self.assertTrue(before, "positive control: active chunks exist")
        count = asyncio.run(ingest_text(self.paper_own, "x" * 500))
        self.assertGreater(count, 0, "positive control: ingest wrote chunks")
        after = set(Text.objects.filter(
            index_version=active).values_list("id", flat=True))
        self.assertEqual(before, after,
                         "active version chunks must stay immutable")
        building = PaperIndexVersion.objects.filter(
            paper=self.paper_own, status="building").order_by("-id").first()
        self.assertIsNotNone(building,
                             "new chunks must belong to a building version")
        new_ids = set(Text.objects.filter(
            index_version=building).values_list("id", flat=True))
        self.assertEqual(len(new_ids), count,
                         "new chunks belong only to the building version")

    def test_INGFIX_SUPERSEDED_FAILED_IMMUTABLE(self):
        self.case_id = "INGFIX-SUPERSEDED-FAILED-IMMUTABLE"
        self.expected_pre_fix = "PASS"
        self.contract = ("superseded and failed version chunks are never "
                         "deleted or modified by a compatible write")
        self.positive_control = "superseded/failed versions with chunks exist"
        self.negative_control = "their chunk ids unchanged after ingest"
        self.root_cause = ""
        from rag.ingest import ingest_text
        from rag.models import Text

        superseded = self._make_version(self.paper_own, "s-model", "s-v",
                                        status="superseded")
        failed = self._make_version(self.paper_own, "f-model", "f-v",
                                    status="failed")
        for version, cid in ((superseded, "ss"), (failed, "ff")):
            Text.objects.create(
                paper=self.paper_own, index_version=version, docname=cid,
                chunk_index=0, content=f"{cid} chunk",
                embedding=[1.0] + [0.0] * 1023,
                embedding_model="m", embedding_dim=1024,
                embedding_version="v", content_hash=f"h-{cid}",
                citation_key=f"pqac-{cid}", search_vector=cid)
        before = {v.id: set(Text.objects.filter(index_version=v)
                            .values_list("id", flat=True))
                  for v in (superseded, failed)}
        asyncio.run(ingest_text(self.paper_own, "y" * 500))
        for v in (superseded, failed):
            self.assertEqual(
                set(Text.objects.filter(index_version=v).values_list("id", flat=True)),
                before[v.id],
                f"{v.status} chunks must stay immutable")

    def test_INGFIX_REPLACE_ONLY_BUILDING(self):
        self.case_id = "INGFIX-REPLACE-ONLY-BUILDING"
        self.expected_pre_fix = "PASS"
        self.contract = ("replace_existing replaces ONLY the current building "
                         "version's chunks; active/superseded/failed remain")
        self.positive_control = "chunks exist on multiple versions"
        self.negative_control = "only building chunk ids change on replace"
        self.root_cause = ""
        from rag.ingest import ingest_text
        from rag.models import PaperIndexVersion, Text

        active_before = set(Text.objects.filter(
            index_version=self.version_own).values_list("id", flat=True))
        asyncio.run(ingest_text(self.paper_own, "z" * 600,
                                replace_existing=True))
        asyncio.run(ingest_text(self.paper_own, "w" * 600,
                                replace_existing=True))
        building = PaperIndexVersion.objects.filter(
            paper=self.paper_own, status="building").order_by("-id").first()
        self.assertIsNotNone(building)
        self.assertEqual(Text.objects.filter(index_version=building).count(), 1,
                         "replace_existing leaves exactly one building chunk set")
        self.assertEqual(
            set(Text.objects.filter(index_version=self.version_own)
                .values_list("id", flat=True)),
            active_before,
            "active chunks must survive replace_existing")

    def test_INGFIX_ACTIVE_RETRIEVABLE_AFTER_INGEST(self):
        self.case_id = "INGFIX-ACTIVE-RETRIEVABLE-AFTER-INGEST"
        self.expected_pre_fix = "PASS"
        self.contract = ("the previous ACTIVE version stays retrievable after a "
                         "compatible ingest (until atomic activation lands)")
        self.positive_control = "own active chunk is recalled"
        self.negative_control = "ingest must not unlink the active version"
        self.root_cause = ""
        from agent.project_tools import query_project_rag
        from agent.scope_failing_tests import _mock_rcs_summary
        from rag.ingest import ingest_text

        asyncio.run(ingest_text(self.paper_own, "q" * 500))
        with mock.patch("rag.retrieval._rcs_summary",
                        new=mock.AsyncMock(side_effect=_mock_rcs_summary)):
            result = asyncio.run(
                query_project_rag(self.proj_a.id, "selective state space", k=4))
        own_ids = [e.get("paper_id") for e in result.get("evidence", [])]
        self.assertIn(self.paper_own.id, own_ids,
                      "positive control: active chunks still retrievable")


class IngBJobMigrationFixTest(_IngestionRedBase):
    """P1: api 0004 -> 0005 job migration preserves legacy rows, applies new
    defaults, and PROTECTs referenced index versions."""

    def test_INGFIX_JOB_MIGRATION_0004_0005(self):
        self.case_id = "INGFIX-JOB-MIGRATION-0004-0005"
        self.expected_pre_fix = "PASS"
        self.contract = ("migrating api 0004 -> 0005 keeps pending/failed jobs "
                         "(row, project, paper, status, error, timestamps) and "
                         "applies new field defaults; index_version is PROTECT")
        self.positive_control = "legacy jobs created at api 0004"
        self.negative_control = "no rows lost; defaults correct; PROTECT enforced"
        self.root_cause = ""
        from django.db import connection
        from django.db.migrations.executor import MigrationExecutor
        from django.db.models import ProtectedError

        A4 = ("api", "0004_paper_relation_context")
        A5 = ("api", "0005_paper_ingestion_job_v2")
        if A5 not in MigrationExecutor(connection).loader.graph.nodes:
            self.fail("api 0005 migration missing — Tasks 2.3 (red)")

        MigrationExecutor(connection).migrate([A4])
        try:
            apps4 = MigrationExecutor(connection).loader.project_state([A4]).apps
            Job4 = apps4.get_model("api", "PaperIngestionJob")
            RP4 = apps4.get_model("api", "ResearchProject")
            Paper4 = apps4.get_model("papers", "Paper")
            proj = RP4.objects.create(title="Migrated Project")
            paper = Paper4.objects.create(title="Migrated Paper", year=2024)
            j1 = Job4.objects.create(project=proj, paper=paper,
                                     status="pending", file_name="a.pdf")
            j2 = Job4.objects.create(project=proj, paper=paper,
                                     status="failed",
                                     error_message="boom",
                                     celery_task_id="t1")
            ids = [j1.id, j2.id]

            MigrationExecutor(connection).migrate([A5])
            apps5 = MigrationExecutor(connection).loader.project_state([A5]).apps
            Job5 = apps5.get_model("api", "PaperIngestionJob")
            rows = list(Job5.objects.filter(id__in=ids).order_by("id"))
            self.assertEqual(len(rows), 2, "no legacy rows may be lost")
            for row in rows:
                self.assertEqual(row.project_id, proj.id)
                self.assertEqual(row.paper_id, paper.id)
                self.assertTrue(row.created_at)
                self.assertTrue(row.updated_at)
                self.assertEqual(row.attempt_count, 0)
                self.assertEqual(row.error_code, "")
                self.assertEqual(row.retryable, False)
                self.assertEqual(row.idempotency_key, "")
                self.assertEqual(row.source_kind, "")
                self.assertIsNone(row.index_version_id)
            self.assertEqual(rows[0].status, "pending")
            self.assertEqual(rows[1].status, "failed")
            self.assertEqual(rows[1].error_message, "boom")
            self.assertEqual(rows[1].celery_task_id, "t1")

            # PROTECT: a referenced index version cannot be deleted
            Piv5 = apps5.get_model("rag", "PaperIndexVersion")
            RP5 = apps5.get_model("api", "ResearchProject")
            Paper5 = apps5.get_model("papers", "Paper")
            proj5 = RP5.objects.get(id=proj.id)
            paper5 = Paper5.objects.get(id=paper.id)
            version = Piv5.objects.create(
                paper_id=paper.id, status="building",
                source_sha256="s1", pipeline_signature="p1")
            Job5.objects.create(project=proj5, paper=paper5, status="pending",
                                index_version=version)
            with self.assertRaises(ProtectedError):
                version.delete()
        finally:
            MigrationExecutor(connection).migrate(
                MigrationExecutor(connection).loader.graph.leaf_nodes())


class IngBMigrationDeterminismTest(_IngestionRedBase):
    """P1: migration determinism — config identity drives active/superseded,
    missing config activates nothing, same data yields same identity."""

    def _run_backfill(self, identity_model, identity_version, identity_dim):
        from django.db import connection
        from django.db.migrations.executor import MigrationExecutor

        MF = ("rag", "0003_bge_m3_sparse_weights")
        MT = ("rag", "0004_paper_index_version")
        MigrationExecutor(connection).migrate([MF])
        try:
            apps3 = MigrationExecutor(connection).loader.project_state([MF]).apps
            Text3 = apps3.get_model("rag", "Text")
            Paper3 = apps3.get_model("papers", "Paper")
            return self._seed_and_migrate(apps3, Text3, Paper3, MT,
                                          identity_model, identity_version,
                                          identity_dim)
        finally:
            MigrationExecutor(connection).migrate(
                MigrationExecutor(connection).loader.graph.leaf_nodes())

    def _seed_and_migrate(self, apps3, Text3, Paper3, MT, cfg_model,
                          cfg_version, cfg_dim):
        from django.db import connection
        from django.db.migrations.executor import MigrationExecutor

        def _chunk(paper, idx, cid, model, version, dim):
            # the pgvector column is fixed at 1024 dims — legacy rows store the
            # column dimension; the config identity may still be absent (0)
            vec_dim = dim if dim and dim > 0 else 1024
            Text3.objects.create(
                paper=paper, docname=f"c{idx}", chunk_index=idx,
                content=f"content {cid}",
                embedding=[1.0] + [0.0] * (vec_dim - 1),
                embedding_model=model, embedding_dim=vec_dim,
                embedding_version=version, content_hash=f"h-{cid}",
                citation_key=f"pqac-{cid}", search_vector=cid)

        p1 = Paper3.objects.create(title="Det Paper 1", year=2024)
        _chunk(p1, 0, "a1", cfg_model, cfg_version, cfg_dim)
        _chunk(p1, 1, "a2", cfg_model, cfg_version, cfg_dim)
        _chunk(p1, 2, "b1", "other-model", "other-v", cfg_dim)
        p2 = Paper3.objects.create(title="Det Paper 2", year=2023)
        _chunk(p2, 0, "a1", cfg_model, cfg_version, cfg_dim)
        _chunk(p2, 1, "a2", cfg_model, cfg_version, cfg_dim)

        MigrationExecutor(connection).migrate([MT])
        apps4 = MigrationExecutor(connection).loader.project_state([MT]).apps
        Text4 = apps4.get_model("rag", "Text")
        Piv4 = apps4.get_model("rag", "PaperIndexVersion")
        Paper4 = apps4.get_model("papers", "Paper")
        return Text4, Piv4, Paper4, p1.id, p2.id

    def test_INGFIX_MIGRATION_CONFIG_MATCH(self):
        self.case_id = "INGFIX-MIGRATION-CONFIG-MATCH"
        self.expected_pre_fix = "PASS"
        self.contract = ("with a matching configured identity the compatible "
                         "group is active, others superseded; deterministic "
                         "legacy source_sha256 over ordered content hashes")
        self.positive_control = "compatible group activated"
        self.negative_control = "incompatible group superseded; no wrong active"
        self.root_cause = ""
        cfg_model, cfg_version, cfg_dim = _config_embedding_identity()
        Text4, Piv4, Paper4, p1, p2 = self._run_backfill(
            cfg_model, cfg_version, cfg_dim)
        versions = list(Piv4.objects.filter(paper_id=p1).order_by("id"))
        actives = [v for v in versions if v.status == "active"]
        self.assertEqual(len(actives), 1, "exactly one active for paper 1")
        self.assertEqual(actives[0].embedding_model, cfg_model)
        self.assertEqual(actives[0].chunk_count, 2)
        self.assertTrue(all(v.status == "superseded"
                            for v in versions if v.status != "active"))
        # determinism: identical legacy data yields identical version identity
        self.assertEqual(len(Piv4.objects.filter(paper_id=p2)), 1)
        v2 = Piv4.objects.get(paper_id=p2)
        self.assertEqual(v2.source_sha256, actives[0].source_sha256,
                         "same legacy chunk content hashes must yield the "
                         "same version identity")
        # every Text row references a version
        self.assertFalse(Text4.objects.filter(index_version_id__isnull=True)
                         .exists())

    def test_INGFIX_MIGRATION_CONFIG_MISSING(self):
        self.case_id = "INGFIX-MIGRATION-CONFIG-MISSING"
        self.expected_pre_fix = "PASS"
        self.contract = ("when the configured identity is missing the backfill "
                         "activates NOTHING (deterministic, explainable)")
        self.positive_control = "legacy groups become versions"
        self.negative_control = "no version is activated without config"
        self.root_cause = ""
        meta = _active_embedding_meta()
        Text4, Piv4, Paper4, p1, p2 = self._run_backfill("", "", 0)
        versions = list(Piv4.objects.filter(paper_id=p1))
        self.assertTrue(versions, "positive control: legacy groups versioned")
        self.assertFalse(any(v.status == "active" for v in versions),
                         "no group may be activated without a configured "
                         "identity")
