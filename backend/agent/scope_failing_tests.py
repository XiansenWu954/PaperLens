"""Stage B Tasks 1.1-1.6: Security contract tests (v4, per §13 directive).

v4 changes (this round):
- NetworkCallCounter patches the ACTUAL symbols the running code looks up at
  call time, not just the defining module (verified against production code).
- Guard is auto-installed for EVERY case via NetworkGuardTestCaseMixin.setUp
  and torn down in tearDown with `calls == []` assertion (§13.7).
- Every case records machine-readable evidence (guard installed, intercepted
  entry points, call counts, expected_pre_fix vs actual) to
  $PAPERLENS_STAGE_B_ARTIFACTS_DIR/network-counter.jsonl.
- DatabaseEnvironmentManifestTest proves the runtime DB identity
  (vendor == postgresql, masked host/name digests, pgvector extension version)
  instead of guessing (§13.1).

Run (PostgreSQL gate):
  docker compose run --rm -v "$PWD/docs/internal/stage-b-artifacts-20260810:/artifacts" \
    -e PAPERLENS_STAGE_B_ARTIFACTS_DIR=/artifacts backend \
    python manage.py test agent.scope_failing_tests --noinput -v 2
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
from datetime import datetime, timezone
from unittest import mock

from django.conf import settings
from django.db import connection
from django.test import TransactionTestCase

from api.models import ProjectPaper, ResearchProject
from agent.evidence import make_evidence_id
from papers.models import Paper
from rag.models import Text


def _e1024(seed: float = 1.0) -> list[float]:
    return [seed] + [0.0] * 1023


# =====================================================================
# Machine-readable artifacts (§13.9.4: fixed raw output, no hand-written stats)
#
# Every run MUST target its own unique artifact directory (set
# PAPERLENS_STAGE_B_ARTIFACTS_DIR to a per-run subdirectory); historical runs
# are never overwritten. Each case writes exactly ONE file from its own
# tearDown — no shared append, no cross-teardown races.
# =====================================================================

ARTIFACTS_DIR = os.environ.get("PAPERLENS_STAGE_B_ARTIFACTS_DIR", "")


def _artifact_path(name: str) -> str | None:
    if not ARTIFACTS_DIR:
        return None
    os.makedirs(ARTIFACTS_DIR, exist_ok=True)
    return os.path.join(ARTIFACTS_DIR, name)


def _write_json(name: str, record: dict) -> None:
    path = _artifact_path(name)
    if not path:
        return
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(record, fh, ensure_ascii=False, default=str, indent=2)


def _drain_asgiref_db_sessions() -> None:
    """Close DB sessions held by asgiref's persistent worker threads (§10 gate).

    Delegates to the shared suite-level helper in config.test_runner, which is
    also wired into GuardedTestRunner.teardown_databases for the full default
    regression.
    """
    from config.test_runner import drain_asgiref_db_sessions as _runner_drain

    _runner_drain()


# =====================================================================
# §13.7: Fail-on-call network counter — patched symbols ARE the runtime lookups
# =====================================================================

class NetworkCallCounter:
    """Tracks calls to the exact symbols the running code resolves at call time.

    Verified against production modules:
    - ``llm.deepseek.DeepSeekClient``: chat_loop.ChatAgentLoop.run,
      harness.ProjectAgentHarness._call_live_answer_model and
      retrieval._rcs_summary all execute ``from llm.deepseek import
      DeepSeekClient`` inside the function body → module-attribute patch works.
    - ``llm.deepseek.OpenAI``: bound into llm/deepseek.py at module import time
      (``from openai import OpenAI``) → must patch the module attribute, not
      ``openai.OpenAI`` (which is not the looked-up symbol there).
    - ``datasources.registry.search``: project_tools.search_papers executes
      ``from datasources.registry import search`` at call time.
    - ``FlagEmbedding.BGEM3FlagModel`` / ``sentence_transformers.SentenceTransformer``:
      embedding.get_embedder imports them inside the function body (model download).
    """

    ENTRY_POINTS = (
        "llm.deepseek.DeepSeekClient",
        "llm.deepseek.OpenAI",
        "datasources.registry.search",
        "FlagEmbedding.BGEM3FlagModel",
        "sentence_transformers.SentenceTransformer",
    )

    def __init__(self):
        self.calls: list[str] = []

    def make_counter(self, name: str):
        def _counter(*a, **kw):
            self.calls.append(name)
            raise AssertionError(
                f"NETWORK CALL DETECTED: {name} was called during offline test. "
                f"Counter={len(self.calls)}. This test must be fully offline.")
        return _counter

    def install_patches(self):
        """Return a context manager that patches all real network entry points."""
        from contextlib import ExitStack
        stack = ExitStack()
        for symbol in self.ENTRY_POINTS:
            stack.enter_context(
                mock.patch(symbol, side_effect=self.make_counter(symbol))
            )
        return stack


class NetworkGuardTestCaseMixin:
    """Auto-installs the fail-on-call guard for EVERY case; tearDown asserts zero calls.

    Every case records: guard installed?, intercepted entry points, call count,
    expected_pre_fix (declared per test), actual status (from the real outcome).
    """

    case_id = ""              # set per test method, e.g. "READ-OWN"
    expected_pre_fix = ""     # set per test method: "PASS" or "FAIL"

    def setUp(self):
        super().setUp()
        self._counter = NetworkCallCounter()
        self._counter_stack = self._counter.install_patches()
        self._guard_installed = True

    def tearDown(self):
        calls = list(getattr(self._counter, "calls", []))
        guard_violation = bool(calls)
        body_status = self._body_status()
        guard_assert_raised = False
        try:
            if guard_violation:
                guard_assert_raised = True
                self.fail(
                    f"NETWORK CALLS DETECTED during {self.id()}: {calls} — "
                    "offline test violated the network gate.")
        finally:
            if getattr(self, "_counter_stack", None) is not None:
                self._counter_stack.close()
            _drain_asgiref_db_sessions()
            self._record_case(body_status, guard_violation, guard_assert_raised)
            super().tearDown()

    def _body_status(self) -> str:
        """Status of the test BODY up to tearDown entry.

        tearDown-time outcomes (e.g. the network-guard assertion above) are not
        part of this snapshot: they are recorded as `guard_violation` /
        `guard_assert_raised`. The authoritative per-case `actual` is filled by
        the post-suite aggregator from the runner result, which includes
        tearDown failures.
        """
        try:
            result = self._outcome.result
        except AttributeError:
            return "UNKNOWN"
        if any(test is self for test, _tb in result.errors):
            return "ERROR"
        if any(test is self for test, _tb in result.failures):
            return "FAIL"
        return "PASS"

    def _record_case(self, body_status: str, guard_violation: bool,
                     guard_assert_raised: bool) -> None:
        _write_json(f"network-counter-{self.case_id or 'CASE'}.json", {
            "case_id": self.case_id,
            "test": self.id(),
            "expected_pre_fix": self.expected_pre_fix,
            "body_status": body_status,
            "actual": None,
            "network_guard_installed": getattr(self, "_guard_installed", False),
            "network_entry_points": list(NetworkCallCounter.ENTRY_POINTS),
            "network_call_count": len(getattr(self._counter, "calls", [])),
            "network_calls": list(getattr(self._counter, "calls", [])),
            "guard_violation": guard_violation,
            "guard_assert_raised": guard_assert_raised,
        })


# =====================================================================
# §13.1: DB environment manifest — runtime evidence, not guesses
# =====================================================================

class DatabaseEnvironmentManifestTest(NetworkGuardTestCaseMixin, TransactionTestCase):
    """Proves the database identity actually used by the test run.

    GATE LOCATION (constraint): this class is an EXPLICIT Stage B Docker gate.
    It lives only in the explicit red-suite module (`scope_failing_tests.py`,
    which is NOT matched by default `test*.py` discovery). It MUST NOT be moved
    into the default unit regression, which is allowed to run on SQLite
    fallback. On SQLite these cases FAIL on purpose — the report must then say
    "SQLite", never "PostgreSQL validation".
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._manifest = cls._build_manifest()
        _write_json("db-manifest.json", cls._manifest)

    @classmethod
    def _build_manifest(cls) -> dict:
        vendor = connection.vendor
        cfg = connection.settings_dict
        host = str(cfg.get("HOST") or "")
        name = str(cfg.get("NAME") or "")

        def digest(value: str) -> str:
            return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]

        manifest = {
            "vendor": vendor,
            "db_name_masked_digest": digest(name),
            "db_host_masked_digest": digest(host),
            "db_name_present": bool(name),
            "pgvector_version": None,
            "network_guard_active": False,
            "hf_hub_offline": os.environ.get("HF_HUB_OFFLINE"),
            "transformers_offline": os.environ.get("TRANSFORMERS_OFFLINE"),
            "embedding_provider": settings.PAPERLENS_EMBEDDING_PROVIDER,
            "embedding_model": settings.PAPERLENS_EMBEDDING_MODEL,
            "embedding_dim": settings.PAPERLENS_EMBEDDING_DIM,
            "live_llm_in_tests": bool(settings.PROJECT_CHAT_LIVE_LLM),
            "run_at": datetime.now(timezone.utc).isoformat(),
        }
        try:
            from config.test_runner import _guard_active
            manifest["network_guard_active"] = bool(_guard_active)
        except Exception:
            pass
        if vendor == "postgresql":
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT extversion FROM pg_extension WHERE extname = 'vector'")
                row = cursor.fetchone()
                manifest["pgvector_version"] = row[0] if row else None
        return manifest

    def test_vendor_is_postgresql(self):
        """Gate: the security suite must run on PostgreSQL+pgvector."""
        self.case_id = "DB-MANIFEST-VENDOR"
        self.expected_pre_fix = "PASS"
        self.assertEqual(
            self._manifest["vendor"], "postgresql",
            f"vendor={self._manifest['vendor']!r} — PostgreSQL gate not met. "
            "Report must NOT claim PostgreSQL validation.")

    def test_database_identity_masked_digest(self):
        """Host/name are recorded only as masked sha256 digests (no secrets)."""
        self.case_id = "DB-MANIFEST-IDENTITY"
        self.expected_pre_fix = "PASS"
        self.assertTrue(self._manifest["db_name_present"])
        for key in ("db_name_masked_digest", "db_host_masked_digest"):
            value = self._manifest[key]
            self.assertIsInstance(value, str)
            self.assertEqual(len(value), 16, f"{key} must be a 16-hex digest")
            self.assertNotIn("postgres://", value)
            self.assertNotIn("paperlens@", value)

    def test_pgvector_extension_and_version(self):
        """pgvector extension must exist and be version-queryable."""
        self.case_id = "DB-MANIFEST-PGVECTOR"
        self.expected_pre_fix = "PASS"
        self.assertIsNotNone(
            self._manifest["pgvector_version"],
            "pgvector extension missing (or vendor != postgresql)")

    def test_embedding_provider_is_fake_in_tests(self):
        """Deterministic test embedding boundary, no model download."""
        self.case_id = "DB-MANIFEST-EMBEDDING"
        self.expected_pre_fix = "PASS"
        self.assertEqual(self._manifest["embedding_provider"], "fake")

    def test_live_llm_off_in_tests_and_network_guard_active(self):
        """Offline guarantees: no live LLM, socket guard active, HF offline."""
        self.case_id = "DB-MANIFEST-OFFLINE"
        self.expected_pre_fix = "PASS"
        self.assertFalse(self._manifest["live_llm_in_tests"])
        self.assertTrue(self._manifest["network_guard_active"])
        self.assertEqual(self._manifest["hf_hub_offline"], "1")


class NetworkGuardCanaryTest(NetworkGuardTestCaseMixin, TransactionTestCase):
    """§10 canary: the INSTALLED guarded socket entries MUST reject a reserved
    TEST-NET address (RFC 5737, non-routable) by raising NetworkAccessBlocked
    BEFORE invoking the original socket functions — no DNS, no real outbound
    attempt. Both patched entries are verified: socket.create_connection and
    socket.socket.connect."""

    def test_create_connection_blocks_reserved_address_before_original(self):
        self.case_id = "NETGUARD-CANARY-CREATE"
        self.expected_pre_fix = "PASS"
        import socket as _socket

        from config.test_runner import (
            NetworkAccessBlocked, _guard_active, _original_create_connection)

        self.assertTrue(_guard_active, "socket guard must be active in offline suite")
        reached: list = []

        def spy(*args, **kwargs):
            reached.append(args)
            return _original_create_connection(*args, **kwargs)

        with mock.patch("config.test_runner._original_create_connection", side_effect=spy):
            with self.assertRaises(NetworkAccessBlocked):
                _socket.create_connection(("192.0.2.1", 9999), timeout=2)
        self.assertEqual(reached, [],
                         "guard must reject BEFORE invoking the original socket")

    def test_socket_connect_blocks_reserved_address_before_original(self):
        self.case_id = "NETGUARD-CANARY-CONNECT"
        self.expected_pre_fix = "PASS"
        import socket as _socket

        from config.test_runner import (
            NetworkAccessBlocked, _guard_active, _original_socket_connect)

        self.assertTrue(_guard_active, "socket guard must be active in offline suite")
        reached: list = []

        def spy(sock, *args, **kwargs):
            reached.append(args)
            return _original_socket_connect(sock, *args, **kwargs)

        with mock.patch("config.test_runner._original_socket_connect", side_effect=spy):
            sock = _socket.socket()
            try:
                with self.assertRaises(NetworkAccessBlocked):
                    sock.connect(("192.0.2.1", 9999))
            finally:
                sock.close()
        self.assertEqual(reached, [],
                         "guard must reject BEFORE invoking the original socket.connect")


class ScopeFixtureMixin:
    """Standard scope fixture per §3."""

    def setUpScopeFixture(self):
        # Active index version comes from the effective embedding provider
        # (fake in tests). Fixture chunks carry the ACTIVE version so resolver
        # semantics are real without weakening production constraints; the
        # inactive-version case is created explicitly in the citation tests.
        meta = _active_embedding_meta()
        active_model = str(meta["embedding_model"])
        active_version = str(meta["embedding_version"])
        self.proj_a = ResearchProject.objects.create(title="Project A", status="active")
        self.proj_b = ResearchProject.objects.create(title="Project B", status="active")

        self.paper_own = Paper.objects.create(
            title="Own Included Paper", abstract="OWN_ABSTRACT selective state spaces.",
            year=2024, arxiv_id="own-inc-1", referenced_works=["W-shared-1", "W-own-a"])
        ProjectPaper.objects.create(project=self.proj_a, paper=self.paper_own, status="included")
        self.text_own = Text.objects.create(
            paper=self.paper_own, docname="own chunk 0", chunk_index=0,
            content="OWN_SENTINEL selective state space model SSM",
            embedding=_e1024(1.0), embedding_model=active_model, embedding_dim=1024,
            embedding_version=active_version, content_hash="h_own",
            citation_key="pqac-own", search_vector="Own Included Paper selective state space model SSM")

        self.paper_foreign = Paper.objects.create(
            title="Foreign Paper B", abstract="FOREIGN_ABSTRACT graph attention GAT.",
            year=2023, arxiv_id="foreign-inc-1", referenced_works=["W-shared-1", "W-foreign-b"])
        ProjectPaper.objects.create(project=self.proj_b, paper=self.paper_foreign, status="included")
        self.text_foreign = Text.objects.create(
            paper=self.paper_foreign, docname="foreign chunk 0", chunk_index=0,
            content="FOREIGN_SENTINEL graph attention network transformer",
            embedding=_e1024(2.0), embedding_model=active_model, embedding_dim=1024,
            embedding_version=active_version, content_hash="h_foreign",
            citation_key="pqac-foreign", search_vector="Foreign Paper B graph attention network transformer")

        self.paper_excluded = Paper.objects.create(
            title="Excluded Paper", abstract="EXCLUDED_ABSTRACT.", year=2022, arxiv_id="excl-1",
            referenced_works=["W-shared-1"])
        ProjectPaper.objects.create(project=self.proj_a, paper=self.paper_excluded, status="excluded")
        self.text_excluded = Text.objects.create(
            paper=self.paper_excluded, docname="excluded chunk 0", chunk_index=0,
            content="EXCLUDED_SENTINEL excluded content",
            embedding=_e1024(3.0), embedding_model=active_model, embedding_dim=1024,
            embedding_version=active_version, content_hash="h_excluded",
            citation_key="pqac-excluded", search_vector="Excluded Paper excluded content")

        self.paper_unlinked = Paper.objects.create(
            title="Unlinked Global Paper", abstract="UNLINKED_ABSTRACT.", year=2021, arxiv_id="unlink-1",
            referenced_works=["W-shared-1", "W-unlinked-c"])
        self.text_unlinked = Text.objects.create(
            paper=self.paper_unlinked, docname="unlinked chunk 0", chunk_index=0,
            content="UNLINKED_SENTINEL unlinked content",
            embedding=_e1024(4.0), embedding_model=active_model, embedding_dim=1024,
            embedding_version=active_version, content_hash="h_unlinked",
            citation_key="pqac-unlinked", search_vector="Unlinked Global Paper unlinked content")

        # STALE-index chunk on the own paper (§20.2): real id + hash, but a
        # different (inactive) embedding model/version. Evidence producers must
        # never surface it as fulltext.
        self.text_own_stale = Text.objects.create(
            paper=self.paper_own, docname="own stale chunk", chunk_index=1,
            content="OWN_STALE_SENTINEL stale content",
            embedding=_e1024(9.0), embedding_model="stale-model", embedding_dim=1024,
            embedding_version="stale-version", content_hash="h_own_stale",
            citation_key="pqac-own-stale", search_vector="own stale content")

        self.nonexistent_id = 999999

    def _assert_sentinel_absent(self, result, sentinel, where="result"):
        raw = json.dumps(result, default=str)
        self.assertNotIn(sentinel, raw, f"LEAK: '{sentinel}' in {where}")


# Mock helpers for embedding (non-deterministic boundary only)
def _active_embedding_meta() -> dict:
    """Effective embedding metadata (fake provider in tests) = active index
    version for resolver semantics. Fixtures use it directly."""
    from rag.embedding import embedding_metadata

    return embedding_metadata()


def _mock_embed_return(texts, **kw):
    """Return fixed embeddings for fixture content — does NOT bypass scope."""
    import numpy as np
    return np.array([_e1024(1.0)] * len(texts), dtype="float32")


def _mock_rcs_summary(question, text):
    """Mock RCS scorer: returns Evidence without calling DeepSeek.
    Does NOT bypass scope — the real retriever still selects candidates."""
    from rag.models import Evidence
    return Evidence(
        text=text, question=question,
        summary=text.content[:100], score=8.0,
        citation_key=text.citation_key,
    )


# =====================================================================
# Task 3.1/3.2: typed evidence envelope schema
# =====================================================================

ENVELOPE_FIELDS = (
    "evidence_id", "project_id", "paper_id", "chunk_id", "content_hash",
    "excerpt", "page_start", "page_end", "section",
    "retrieval_sources", "retrieval_scores", "embedding_version",
)


class EvidenceIdContractTest(NetworkGuardTestCaseMixin, TransactionTestCase):
    """§20.1: evidence_id is a deterministic digest of the normalized
    project/paper/chunk/content_hash/embedding_version representation — same
    version is stable, any content or version change produces a different id."""

    def test_EVIDENCE_ID_STABLE(self):
        self.case_id = "EVIDENCE-ID-STABLE"
        self.expected_pre_fix = "PASS"
        eid1 = make_evidence_id(1, 2, 3, "h1", "v1")
        eid2 = make_evidence_id(1, 2, 3, "h1", "v1")
        self.assertEqual(eid1, eid2, "same chunk version must produce the same id")
        self.assertTrue(eid1.startswith("ev-"))

    def test_EVIDENCE_ID_CONTENT_CHANGE(self):
        self.case_id = "EVIDENCE-ID-CONTENT-CHANGE"
        self.expected_pre_fix = "PASS"
        self.assertNotEqual(
            make_evidence_id(1, 2, 3, "h1", "v1"),
            make_evidence_id(1, 2, 3, "h2", "v1"),
            "content hash change must produce a different id")

    def test_EVIDENCE_ID_VERSION_CHANGE(self):
        self.case_id = "EVIDENCE-ID-VERSION-CHANGE"
        self.expected_pre_fix = "PASS"
        self.assertNotEqual(
            make_evidence_id(1, 2, 3, "h1", "v1"),
            make_evidence_id(1, 2, 3, "h1", "v2"),
            "embedding version change must produce a different id")


class EvidenceEnvelopeSchemaTest(ScopeFixtureMixin, NetworkGuardTestCaseMixin, TransactionTestCase):
    """Task 3.1: full-text evidence carries the typed envelope; metadata
    candidates never disguise themselves as full-text; chunk_index stays
    display-only (resolution is database-driven, covered by CIT-*)."""

    def setUp(self):
        super().setUp()
        self.setUpScopeFixture()
        meta = _active_embedding_meta()
        self.paper_own2 = Paper.objects.create(
            title="Own Second Paper", abstract="OWN2_ABSTRACT transformers.",
            year=2017, arxiv_id="own-env-2")
        ProjectPaper.objects.create(project=self.proj_a, paper=self.paper_own2, status="included")
        Text.objects.create(
            paper=self.paper_own2, docname="own2 env chunk", chunk_index=0,
            content="OWN2_ENV_SENTINEL transformer self-attention",
            embedding=_e1024(6.0), embedding_model=meta["embedding_model"], embedding_dim=1024,
            embedding_version=meta["embedding_version"], content_hash="h_own2_env",
            citation_key="pqac-own2-env", search_vector="Own Second Paper transformer")

    def _assert_envelope(self, item, where):
        for field in ENVELOPE_FIELDS:
            self.assertIn(field, item, f"envelope field {field} missing in {where}")
            if field in ("page_start", "page_end", "section"):
                # display metadata may be absent/None on the underlying chunk
                continue
            self.assertIsNotNone(item.get(field), f"envelope field {field} None in {where}")
        self.assertEqual(item.get("evidence_type"), "fulltext", where)
        # §20.1: the producer output must match the SHARED evidence-id factory.
        self.assertEqual(
            item["evidence_id"],
            make_evidence_id(item["project_id"], item["paper_id"], item["chunk_id"],
                             item["content_hash"], item["embedding_version"]),
            f"evidence_id must come from the shared factory in {where}")

    def test_rag_fulltext_items_carry_envelope(self):
        """Task 3.2: query_project_rag fulltext items carry the full envelope."""
        self.case_id = "ENVELOPE-RAG"
        self.expected_pre_fix = "PASS"
        from agent.project_tools import query_project_rag
        with mock.patch("rag.retrieval.embed", side_effect=_mock_embed_return), \
             mock.patch("rag.retrieval._rcs_summary",
                        new=mock.AsyncMock(side_effect=_mock_rcs_summary)):
            result = asyncio.run(query_project_rag(self.proj_a.id, "selective state space", k=5))
        fulltext = [e for e in result.get("evidence", [])
                    if e.get("evidence_type") == "fulltext"]
        self.assertTrue(fulltext, "positive: fulltext evidence must be present")
        for item in fulltext:
            self._assert_envelope(item, "rag evidence")
        self.assertEqual(fulltext[0]["chunk_id"], self.text_own.id)
        self.assertEqual(fulltext[0]["content_hash"], "h_own")
        self.assertEqual(fulltext[0]["project_id"], self.proj_a.id)

    def test_read_chunks_carry_envelope(self):
        self.case_id = "ENVELOPE-READ"
        self.expected_pre_fix = "PASS"
        from agent.project_tools import read_paper_section
        result = asyncio.run(read_paper_section(self.proj_a.id, self.paper_own.id))
        chunks = result.get("chunks", [])
        self.assertTrue(chunks, "positive: own chunks must be present")
        for chunk in chunks:
            self._assert_envelope(chunk, "read chunk")

    def test_compare_chunks_carry_envelope(self):
        self.case_id = "ENVELOPE-COMPARE"
        self.expected_pre_fix = "PASS"
        from agent.project_tools import compare_papers
        result = asyncio.run(compare_papers(
            self.proj_a.id, [self.paper_own.id, self.paper_own2.id], "methods"))
        papers = result.get("papers", [])
        self.assertEqual(len(papers), 2, "positive: both own papers resolved")
        for paper in papers:
            self.assertTrue(paper.get("chunks"), "positive: own chunks present")
            for chunk in paper["chunks"]:
                self._assert_envelope(chunk, "compare chunk")

    def test_metadata_evidence_is_never_fulltext_envelope(self):
        self.case_id = "ENVELOPE-METADATA"
        self.expected_pre_fix = "PASS"
        from agent.project_tools import query_project_rag
        empty_proj = ResearchProject.objects.create(title="No chunks project", status="active")
        paper_no_chunks = Paper.objects.create(
            title="No Chunks Paper", abstract="NO_CHUNKS_ABSTRACT selective state spaces.",
            year=2020, arxiv_id="no-chunks-1")
        ProjectPaper.objects.create(project=empty_proj, paper=paper_no_chunks, status="included")
        result = asyncio.run(query_project_rag(empty_proj.id, "selective state space", k=5))
        items = result.get("evidence", [])
        self.assertTrue(items, "positive: metadata fallback must produce evidence")
        for item in items:
            self.assertEqual(item.get("evidence_type"), "metadata")
            self.assertNotIn("chunk_id", item,
                             "metadata evidence must not carry a chunk binding")
            self.assertNotIn("content_hash", item)


# =====================================================================
# Task 1.1: read_paper_section (§4)
# =====================================================================

class ReadPaperSectionScopeTest(ScopeFixtureMixin, NetworkGuardTestCaseMixin, TransactionTestCase):

    def setUp(self):
        super().setUp()
        self.setUpScopeFixture()

    def test_READ_OWN_positive(self):
        """expected: PASS baseline — own paper returns OWN_SENTINEL."""
        self.case_id = "READ-OWN"
        self.expected_pre_fix = "PASS"
        from agent.project_tools import read_paper_section
        result = asyncio.run(read_paper_section(self.proj_a.id, self.paper_own.id))
        chunks = result.get("chunks", [])
        self.assertTrue(chunks, "positive control: own must not be empty")
        self.assertIn("OWN_SENTINEL", chunks[0].get("content", ""))

    def test_READ_FOREIGN_leak(self):
        """expected: FAIL — foreign paper leaks FOREIGN_SENTINEL."""
        self.case_id = "READ-FOREIGN"
        self.expected_pre_fix = "PASS"
        from agent.project_tools import read_paper_section
        result = asyncio.run(read_paper_section(self.proj_a.id, self.paper_foreign.id))
        self._assert_sentinel_absent(result, "FOREIGN_SENTINEL")

    def test_READ_EXCLUDED_leak(self):
        """expected: FAIL — excluded paper leaks EXCLUDED_SENTINEL."""
        self.case_id = "READ-EXCLUDED"
        self.expected_pre_fix = "PASS"
        from agent.project_tools import read_paper_section
        result = asyncio.run(read_paper_section(self.proj_a.id, self.paper_excluded.id))
        self._assert_sentinel_absent(result, "EXCLUDED_SENTINEL")

    def test_READ_UNLINKED_leak(self):
        """expected: FAIL — unlinked paper leaks UNLINKED_SENTINEL."""
        self.case_id = "READ-UNLINKED"
        self.expected_pre_fix = "PASS"
        from agent.project_tools import read_paper_section
        result = asyncio.run(read_paper_section(self.proj_a.id, self.paper_unlinked.id))
        self._assert_sentinel_absent(result, "UNLINKED_SENTINEL")

    def test_READ_MISSING_empty(self):
        """expected: PASS baseline — nonexistent returns empty."""
        self.case_id = "READ-MISSING"
        self.expected_pre_fix = "PASS"
        from agent.project_tools import read_paper_section
        result = asyncio.run(read_paper_section(self.proj_a.id, self.nonexistent_id))
        self._assert_sentinel_absent(result, "SENTINEL")

    def test_READ_STALE_chunk_excluded(self):
        """§20.2: a stale-index chunk must never be returned by
        read_paper_section; the active own chunk is the positive control."""
        self.case_id = "READ-STALE-EXCLUDED"
        self.expected_pre_fix = "PASS"
        from agent.project_tools import read_paper_section
        result = asyncio.run(read_paper_section(self.proj_a.id, self.paper_own.id))
        chunks = result.get("chunks", [])
        self.assertTrue(chunks, "positive: active chunks must be returned")
        chunk_ids = {c.get("chunk_id") for c in chunks if c.get("chunk_id")}
        self.assertIn(self.text_own.id, chunk_ids,
                      "positive: active own chunk must be returned")
        self.assertNotIn(self.text_own_stale.id, chunk_ids,
                         "stale-index chunk must never be returned")
        self._assert_sentinel_absent(result, "OWN_STALE_SENTINEL")


# =====================================================================
# Task 1.2: Full project tool scope matrix (§5, §12.1-12.8)
# =====================================================================

class ListProjectPapersScopeTest(ScopeFixtureMixin, NetworkGuardTestCaseMixin, TransactionTestCase):
    """§5/§12.8: inventory semantics — own included visible; foreign/unlinked
    absolutely invisible; excluded membership MAY be returned, but only with
    explicit status=excluded and never with chunks/fulltext/evidence."""

    def setUp(self):
        super().setUp()
        self.setUpScopeFixture()

    def _assert_inventory(self, papers: list[dict]):
        by_title = {p.get("title", ""): p for p in papers}
        # Positive control
        self.assertIn("Own Included Paper", by_title,
                      "positive: own included must appear")
        # Negative controls — foreign/unlinked are P0
        self.assertNotIn("Foreign Paper B", by_title,
                         "P0: foreign must not appear in list")
        self.assertNotIn("Unlinked Global Paper", by_title,
                         "P0: unlinked must not appear in list")
        # Excluded: conditional — if returned, must be explicit + evidence-free
        excluded_rows = [p for title, p in by_title.items() if title == "Excluded Paper"]
        self.assertLessEqual(len(excluded_rows), 1)
        if excluded_rows:
            row = excluded_rows[0]
            self.assertEqual(row.get("status"), "excluded",
                             "excluded membership must carry explicit status")
            for field in ("chunks", "content", "fulltext", "evidence"):
                value = row.get(field)
                self.assertTrue(value in (None, "", [], {}),
                                f"excluded inventory must not carry {field}")
            raw = json.dumps(row, default=str)
            self.assertNotIn("SENTINEL", raw,
                             "excluded inventory must not expose fulltext evidence")

    def test_list_own_visible_foreign_unlinked_invisible(self):
        self.case_id = "LIST-OWN-EXCLUDED-FOREIGN"
        self.expected_pre_fix = "PASS"
        from agent.project_tools import list_project_papers
        result = asyncio.run(list_project_papers(self.proj_a.id))
        self._assert_inventory(result.get("papers", []))


class QueryProjectRagScopeTest(ScopeFixtureMixin, NetworkGuardTestCaseMixin, TransactionTestCase):
    """§12.1/§13.2: REAL retriever on fixture DB; only mock embed/RCS.

    Positive control semantics (constraint): queries are designed so the own
    evidence is reliably recalled by the REAL retriever (own terms in the
    query + own-only scope), while forbidden papers' terms are also present in
    the query — so IF scope leaked, the forbidden text would surface via
    lexical/vector recall and the test would go red. We never force a ranking
    that cannot hold for a foreign-specific query.
    """

    def setUp(self):
        super().setUp()
        self.setUpScopeFixture()

    def _rag(self, project_id: int, question: str, k: int = 5) -> dict:
        from agent.project_tools import query_project_rag
        with mock.patch("rag.retrieval.embed", side_effect=_mock_embed_return), \
             mock.patch("rag.retrieval._rcs_summary",
                        new=mock.AsyncMock(side_effect=_mock_rcs_summary)):
            return asyncio.run(query_project_rag(project_id, question, k=k))

    def _assert_evidence_papers(self, result, allowed_ids, forbidden_ids):
        ev = result.get("evidence", [])
        self.assertTrue(ev, "positive control: own evidence must be non-empty")
        paper_ids = {e.get("paper_id") for e in ev}
        self.assertTrue(paper_ids & allowed_ids,
                        f"positive: evidence must contain an own paper (got {paper_ids})")
        for fid in forbidden_ids:
            self.assertNotIn(fid, paper_ids,
                             f"forbidden paper {fid} leaked into RAG evidence")

    def test_rag_own_positive_control(self):
        """Positive: query own content via REAL retriever → own evidence present."""
        self.case_id = "RAG-OWN"
        self.expected_pre_fix = "PASS"
        result = self._rag(self.proj_a.id, "selective state space", k=5)
        self._assert_evidence_papers(result, {self.paper_own.id}, set())

    def test_rag_foreign_negative_with_own_positive(self):
        """§13.2: foreign paper never in project A evidence; own must be non-empty first."""
        self.case_id = "RAG-FOREIGN-NONVACUOUS"
        self.expected_pre_fix = "PASS"
        # common query: recalls own (selective/state/space) AND would surface
        # foreign if scope leaked (graph attention)
        result = self._rag(self.proj_a.id, "selective state space graph attention", k=5)
        self._assert_evidence_papers(result, {self.paper_own.id}, {self.paper_foreign.id})

    def test_rag_excluded_negative_with_own_positive(self):
        """§12.2: excluded membership must not produce evidence."""
        self.case_id = "RAG-EXCLUDED"
        self.expected_pre_fix = "PASS"
        # "excluded" only appears in the excluded paper's search vector — if the
        # scope filter regressed, EXCLUDED content would surface.
        result = self._rag(self.proj_a.id, "selective state space excluded content", k=5)
        self._assert_evidence_papers(result, {self.paper_own.id}, {self.paper_excluded.id})

    def test_rag_unlinked_negative_with_own_positive(self):
        """§12.2: globally unlinked paper must never produce evidence."""
        self.case_id = "RAG-UNLINKED"
        self.expected_pre_fix = "PASS"
        result = self._rag(self.proj_a.id, "selective state space unlinked content", k=5)
        self._assert_evidence_papers(result, {self.paper_own.id}, {self.paper_unlinked.id})

    def test_rag_empty_paper_ids_fail_closed(self):
        """§13.2: paper_ids=[] through the bottom-level retriever MUST fail closed."""
        self.case_id = "RAG-EMPTY-IDS"
        self.expected_pre_fix = "PASS"
        from rag.retrieval import hybrid_retrieve_texts
        with mock.patch("rag.retrieval.embed", side_effect=_mock_embed_return):
            results = asyncio.run(hybrid_retrieve_texts("test", paper_ids=[], final_k=5))
        self.assertEqual(results, [], "empty paper_ids must return zero, not global")

    def test_rag_stale_chunk_excluded(self):
        """§20.2: a stale-index chunk must never reach RAG evidence; the active
        own chunk is the positive control."""
        self.case_id = "RAG-STALE-EXCLUDED"
        self.expected_pre_fix = "PASS"
        result = self._rag(self.proj_a.id, "stale content selective state space", k=5)
        ev = result.get("evidence", [])
        self.assertTrue(ev, "positive: evidence must be non-empty")
        chunk_ids = {e.get("chunk_id") for e in ev if e.get("chunk_id")}
        self.assertIn(self.text_own.id, chunk_ids,
                      "positive: active own chunk must be retrieved")
        self.assertNotIn(self.text_own_stale.id, chunk_ids,
                         "stale-index chunk must never enter RAG evidence")

    def test_python_store_scope_active(self):
        """§21.3: a prebuilt NumpyVectorStore may contain stale / foreign /
        out-of-scope Texts; the python candidate path must intersect dense
        results with the scoped+active queryset — only own-active may return."""
        self.case_id = "PYTHON-STORE-SCOPE-ACTIVE"
        self.expected_pre_fix = "PASS"
        import numpy as np

        from rag.retrieval import _python_hybrid_candidates
        from rag.store import NumpyVectorStore

        meta = _active_embedding_meta()
        store = NumpyVectorStore()
        store.build_from([self.text_own, self.text_own_stale, self.text_foreign])
        query_vec = np.array(_e1024(1.0), dtype="float32")
        dense, _lexical = _python_hybrid_candidates(
            "selective state space", query_vec,
            paper_ids=[self.paper_own.id],
            dense_k=10, lexical_k=10, store=store, meta=meta)
        dense_ids = {t.id for t in dense}
        self.assertIn(self.text_own.id, dense_ids,
                      "positive: own-active must be returned")
        self.assertNotIn(self.text_own_stale.id, dense_ids,
                         "stale chunk must not bypass the active filter")
        self.assertNotIn(self.text_foreign.id, dense_ids,
                         "foreign chunk must not bypass the project scope")

    def test_python_store_allowed_below_k(self):
        """§22: forbidden candidates occupying the prebuilt store's Top-K must
        NOT starve the legal own-active candidate — the store is rebuilt from
        the scoped+active texts for this query."""
        self.case_id = "PYTHON-STORE-ALLOWED-BELOW-K"
        self.expected_pre_fix = "PASS"
        import numpy as np

        from rag.retrieval import _python_hybrid_candidates
        from rag.store import NumpyVectorStore

        meta = _active_embedding_meta()
        # forbidden chunks are CLOSEST to the query vector; own-active ranks
        # BELOW the top-K of the prebuilt store (search(k=1) would return
        # only forbidden without the rebuild).
        store = NumpyVectorStore()
        store.build_from([self.text_own_stale, self.text_foreign, self.text_own])
        query_vec = np.array(_e1024(2.0), dtype="float32")  # near foreign/stale
        dense, _lexical = _python_hybrid_candidates(
            "selective state space", query_vec,
            paper_ids=[self.paper_own.id],
            dense_k=1, lexical_k=10, store=store, meta=meta)
        dense_ids = {t.id for t in dense}
        self.assertIn(self.text_own.id, dense_ids,
                      "own-active below top-k must still be recalled")
        self.assertNotIn(self.text_own_stale.id, dense_ids)
        self.assertNotIn(self.text_foreign.id, dense_ids)


class ComparePapersScopeTest(ScopeFixtureMixin, NetworkGuardTestCaseMixin, TransactionTestCase):
    """§12.7: own+foreign/excluded/unlinked — scoped error OR own present with
    explicit missing-item disclosure; forbidden content must never appear."""

    def setUp(self):
        super().setUp()
        self.setUpScopeFixture()
        meta = _active_embedding_meta()
        self.paper_own2 = Paper.objects.create(
            title="Own Second Paper", abstract="OWN2_ABSTRACT transformers.",
            year=2017, arxiv_id="own-2")
        ProjectPaper.objects.create(project=self.proj_a, paper=self.paper_own2, status="included")
        Text.objects.create(
            paper=self.paper_own2, docname="own2 chunk 0", chunk_index=0,
            content="OWN2_SENTINEL transformer self-attention",
            embedding=_e1024(5.0), embedding_model=meta["embedding_model"], embedding_dim=1024,
            embedding_version=meta["embedding_version"], content_hash="h_own2",
            citation_key="pqac-own2", search_vector="Own Second Paper transformer")

    def test_compare_own_positive(self):
        """Both own papers must resolve with full-text evidence."""
        self.case_id = "COMPARE-OWN"
        self.expected_pre_fix = "PASS"
        from agent.project_tools import compare_papers
        result = asyncio.run(compare_papers(self.proj_a.id,
                                            [self.paper_own.id, self.paper_own2.id], "methods"))
        titles = [p.get("title", "")[:20] for p in result.get("papers", [])]
        self.assertIn("Own Included Paper", titles, "positive: first own present")
        self.assertIn("Own Second Paper", titles, "positive: second own present")
        chunks_present = all(p.get("chunks") for p in result.get("papers", []))
        self.assertTrue(chunks_present, "positive: both own papers carry fulltext chunks")

    def _scoped_compare(self, forbidden_paper_id: int):
        from agent.project_tools import compare_papers
        return asyncio.run(compare_papers(
            self.proj_a.id, [self.paper_own.id, forbidden_paper_id], "methods"))

    def _assert_scoped_compare(self, forbidden_paper, forbidden_sentinel,
                               forbidden_title: str):
        result = self._scoped_compare(forbidden_paper.id)
        # Negative: forbidden content must never be returned
        self._assert_sentinel_absent(result, forbidden_sentinel, "compare")
        raw = json.dumps(result, default=str)
        self.assertNotIn(forbidden_title, raw,
                         "LEAK: forbidden title in compare result")
        self.assertNotIn("Traceback", raw,
                         "internal exception text must not surface")
        if "error" in result:
            err = result.get("error")
            self.assertIsInstance(err, str, "scoped error must be a string")
            self.assertTrue(err.strip(), "scoped error must be non-empty")
            # Stable + desensitized: foreign/excluded/unlinked must be
            # indistinguishable from a nonexistent id (no existence or
            # membership disclosure, no title/status leakage).
            nonexistent_result = self._scoped_compare(self.nonexistent_id)
            self.assertEqual(err, nonexistent_result.get("error"),
                             "scoped error must be identical to the nonexistent control")
            return
        # §12.7 alternative: compare proceeded — own must be present and the
        # forbidden side disclosed as a typed evidence gap.
        papers = result.get("papers", [])
        own_present = any("Own Included" in p.get("title", "") for p in papers)
        self.assertTrue(own_present, "positive: own must be present when compare proceeds")
        gaps = result.get("evidence_gaps") or []
        self.assertTrue(
            any(g.get("paper_id") == forbidden_paper.id for g in gaps),
            "forbidden paper must be disclosed as a typed evidence gap")

    def test_compare_foreign_scoped(self):
        self.case_id = "COMPARE-FOREIGN"
        self.expected_pre_fix = "PASS"
        self._assert_scoped_compare(self.paper_foreign, "FOREIGN_SENTINEL",
                                    self.paper_foreign.title)

    def test_compare_excluded_scoped(self):
        self.case_id = "COMPARE-EXCLUDED"
        self.expected_pre_fix = "PASS"
        self._assert_scoped_compare(self.paper_excluded, "EXCLUDED_SENTINEL",
                                    self.paper_excluded.title)

    def test_compare_unlinked_scoped(self):
        self.case_id = "COMPARE-UNLINKED"
        self.expected_pre_fix = "PASS"
        self._assert_scoped_compare(self.paper_unlinked, "UNLINKED_SENTINEL",
                                    self.paper_unlinked.title)

    def test_compare_stale_chunk_excluded(self):
        """§20.2: a stale-index chunk must never enter compare evidence; both
        own papers' active chunks are the positive control."""
        self.case_id = "COMPARE-STALE-EXCLUDED"
        self.expected_pre_fix = "PASS"
        from agent.project_tools import compare_papers
        result = asyncio.run(compare_papers(
            self.proj_a.id, [self.paper_own.id, self.paper_own2.id], "methods"))
        papers = result.get("papers", [])
        self.assertEqual(len(papers), 2, "positive: both own papers resolved")
        all_chunk_ids = [
            c.get("chunk_id")
            for p in papers for c in p.get("chunks", []) if c.get("chunk_id")
        ]
        self.assertTrue(all_chunk_ids, "positive: own chunks present")
        self.assertNotIn(self.text_own_stale.id, all_chunk_ids,
                         "stale-index chunk must never enter compare evidence")


class GetProjectCitationGraphScopeTest(ScopeFixtureMixin, NetworkGuardTestCaseMixin, TransactionTestCase):
    """§13.3: graph fixture = two project papers with a DETERMINED
    bibliographic-coupling edge (shared referenced_works), plus foreign /
    excluded / unlinked papers whose referenced_works would couple with own
    papers IF they ever entered the graph. Verify own nodes + own edge, then
    that every forbidden relation is filtered out."""

    def setUp(self):
        super().setUp()
        self.setUpScopeFixture()
        # own2 shares two references with own → determined edge weight 2
        self.paper_own2 = Paper.objects.create(
            title="Own Second Paper", abstract="OWN2_ABSTRACT transformers.",
            year=2017, arxiv_id="own-g2",
            referenced_works=["W-shared-1", "W-own-a"])
        ProjectPaper.objects.create(project=self.proj_a, paper=self.paper_own2, status="included")

    def _graph(self):
        from agent.project_tools import get_project_citation_graph
        result = asyncio.run(get_project_citation_graph(self.proj_a.id))
        return result.get("graph", {})

    def test_graph_own_nodes_and_determined_edge(self):
        self.case_id = "GRAPH-OWN-EDGE"
        self.expected_pre_fix = "PASS"
        graph = self._graph()
        nodes = graph.get("nodes", [])
        edges = graph.get("edges", [])
        node_ids = {n.get("id") for n in nodes}
        # Positive: own nodes
        self.assertEqual(node_ids, {self.paper_own.id, self.paper_own2.id},
                         f"graph nodes must be exactly the own pair (got {node_ids})")
        # Positive: determined own edge (shared referenced_works)
        self.assertTrue(edges, "own edge must exist (shared referenced_works)")
        own_edge = [e for e in edges if {e.get("source"), e.get("target")} ==
                    {self.paper_own.id, self.paper_own2.id}]
        self.assertEqual(len(own_edge), 1, "exactly one own edge expected")
        self.assertGreaterEqual(own_edge[0].get("weight", 0), 1,
                                "own edge weight must reflect shared references")

    def test_graph_filters_foreign_excluded_unlinked(self):
        self.case_id = "GRAPH-FILTERS-FORBIDDEN"
        self.expected_pre_fix = "PASS"
        graph = self._graph()
        nodes = graph.get("nodes", [])
        edges = graph.get("edges", [])
        node_ids = {n.get("id") for n in nodes}
        forbidden = {self.paper_foreign.id, self.paper_excluded.id, self.paper_unlinked.id}
        # Negative: forbidden nodes never appear
        self.assertFalse(node_ids & forbidden, f"forbidden nodes leaked: {node_ids & forbidden}")
        titles = {n.get("title", "") for n in nodes}
        self.assertEqual(titles, {"Own Included Paper", "Own Second Paper"})
        # Negative: every edge endpoint is an own node
        own_ids = {self.paper_own.id, self.paper_own2.id}
        for e in edges:
            self.assertIn(e.get("source"), own_ids,
                          "edge endpoint must be an own node")
            self.assertIn(e.get("target"), own_ids,
                          "edge endpoint must be an own node")


class DraftReportScopeTest(ScopeFixtureMixin, NetworkGuardTestCaseMixin, TransactionTestCase):
    """§12.6/§13.3: real retrieval; only mock embed/RCS + final synthesis is the
    tool's own deterministic assembly. Positive control must prove OWN fulltext
    evidence (or own citation binding) was adopted — section non-empty alone is
    insufficient."""

    def setUp(self):
        super().setUp()
        self.setUpScopeFixture()
        self.proj_empty = ResearchProject.objects.create(title="Empty Project", status="active")

    def _report(self, project_id: int, question: str) -> dict:
        from agent.project_tools import draft_report_section
        with mock.patch("rag.retrieval.embed", side_effect=_mock_embed_return), \
             mock.patch("rag.retrieval._rcs_summary",
                        new=mock.AsyncMock(side_effect=_mock_rcs_summary)):
            return asyncio.run(draft_report_section(project_id, question))

    def test_report_own_fulltext_evidence_positive(self):
        """§13.3: result must prove own fulltext evidence + own citation binding."""
        self.case_id = "REPORT-OWN-FULLTEXT"
        self.expected_pre_fix = "PASS"
        result = self._report(self.proj_a.id, "selective state space model")
        evidence = result.get("evidence", [])
        # Positive: own fulltext evidence actually entered the report input
        self.assertTrue(evidence, "positive: report evidence must be non-empty")
        own_items = [e for e in evidence if e.get("paper_id") == self.paper_own.id]
        self.assertTrue(own_items, "positive: own paper evidence must be present")
        fulltext = any(e.get("evidence_type") == "fulltext" for e in own_items)
        self.assertTrue(fulltext,
                        "positive: report input evidence must be fulltext (not metadata)")
        # Positive: own citation binding appears in the section
        self.assertIn("[cite:pqac-own]", result.get("section", ""),
                      "positive: own citation binding must appear in the section")
        self.assertIn("OWN_SENTINEL", result.get("section", ""),
                      "positive: own fulltext excerpt must appear in the section")

    def test_report_excludes_foreign_excluded_unlinked_evidence(self):
        self.case_id = "REPORT-EXCLUDES-FORBIDDEN"
        self.expected_pre_fix = "PASS"
        result = self._report(self.proj_a.id, "selective state space model")
        evidence = result.get("evidence", [])
        self.assertTrue(evidence, "positive control: evidence must be non-empty")
        forbidden = {self.paper_foreign.id, self.paper_excluded.id, self.paper_unlinked.id}
        evidence_papers = {e.get("paper_id") for e in evidence}
        self.assertFalse(evidence_papers & forbidden,
                         f"forbidden evidence leaked into report: {evidence_papers & forbidden}")
        raw = json.dumps(result, default=str)
        for sentinel in ("FOREIGN_SENTINEL", "EXCLUDED_SENTINEL", "UNLINKED_SENTINEL"):
            self.assertNotIn(sentinel, raw, f"LEAK: {sentinel} in report")

    def test_report_empty_evidence_abstains(self):
        """§12.6: no evidence → abstain/error, never model-knowledge filler."""
        self.case_id = "REPORT-EMPTY-ABSTAINS"
        self.expected_pre_fix = "PASS"
        result = self._report(self.proj_empty.id, "selective state space model")
        self.assertEqual(result.get("evidence"), [],
                         "empty project must produce zero evidence")
        section = result.get("section", "")
        self.assertIn("还没有足够证据", section,
                      "empty evidence must produce the abstention section")
        self.assertNotIn("OWN_SENTINEL", section)


# =====================================================================
# Task 1.3: MCP scope tests (§6, §12.3-12.5)
# =====================================================================

class McpProjectToolScopeTest(ScopeFixtureMixin, NetworkGuardTestCaseMixin, TransactionTestCase):
    """§6/§12.3-12.5: formal MCP dispatch, own positive controls, forbidden
    negatives, outputSchema contract (§13.3: content non-empty is NOT schema
    validation)."""

    def setUp(self):
        super().setUp()
        self.setUpScopeFixture()
        # graph fixture: second own paper with shared references (determined edge)
        self.paper_own2 = Paper.objects.create(
            title="Own Second Paper", abstract="OWN2_ABSTRACT transformers.",
            year=2017, arxiv_id="own-mcp-g2",
            referenced_works=["W-shared-1", "W-own-a"])
        ProjectPaper.objects.create(project=self.proj_a, paper=self.paper_own2, status="included")

    def _call_mcp(self, tool_name, arguments, meta=None):
        from mcp_server.testing import call_tool_via_client
        return call_tool_via_client(tool_name, arguments, meta=meta)

    def _call_mcp_spy(self, tool_name, arguments, meta=None):
        """Call through the real handler entry with execute_project_tool spied."""
        from mcp_server.server import execute_project_tool as _real_execute
        captured: list[dict] = []

        async def spy(context, name, args):
            captured.append({"context": context, "name": name, "args": dict(args)})
            return {"papers": [], "count": 0}

        with mock.patch("mcp_server.server.execute_project_tool", side_effect=spy):
            result = self._call_mcp(tool_name, arguments, meta=meta)
        return result, captured

    def test_mcp_trusted_context_beats_forged_selector(self):
        """§18.2: with a server-bound project (PAPERLENS_MCP_PROJECT_ID=A) the
        forged selector B must NEVER override it; the executor receives a frozen
        context for A only, and the selector never reaches the tool."""
        self.case_id = "MCP-TRUSTED-CONTEXT"
        self.expected_pre_fix = "PASS"
        from agent.context import ToolExecutionContext

        with mock.patch.dict(os.environ,
                             {"PAPERLENS_MCP_PROJECT_ID": str(self.proj_a.id)}):
            result, captured = self._call_mcp_spy(
                "list_project_papers", {"project_id": self.proj_b.id})
        self.assertTrue(captured, "executor must be called")
        ctx = captured[0]["context"]
        self.assertIsInstance(ctx, ToolExecutionContext,
                              "executor must receive a frozen ToolExecutionContext")
        self.assertEqual(ctx.project_id, self.proj_a.id,
                         "server-bound project must not be overridden by the selector")
        self.assertNotIn("project_id", captured[0]["args"],
                         "the selector must not reach the tool implementation")

    def test_mcp_client_meta_never_acts_as_server_bound(self):
        """§18.2: the client-controlled params.meta can carry protocol metadata
        (progress_token) but must NEVER act as a server-bound authorization
        context; a project_id smuggled into meta must not override the selector
        bootstrap."""
        self.case_id = "MCP-CLIENT-META-IGNORED"
        self.expected_pre_fix = "PASS"
        from agent.context import ToolExecutionContext

        # progress_token meta is protocol metadata — ignored for authorization.
        result, captured = self._call_mcp_spy(
            "list_project_papers", {"project_id": self.proj_a.id},
            meta={"progress_token": "tok-123"})
        self.assertTrue(captured, "executor must be called")
        self.assertIsInstance(captured[0]["context"], ToolExecutionContext)
        self.assertEqual(captured[0]["context"].project_id, self.proj_a.id,
                         "progress_token meta must not alter the bootstrap project")
        # A project_id smuggled into client meta must NOT act as server-bound:
        # the explicit selector still bootstraps project A.
        result2, captured2 = self._call_mcp_spy(
            "list_project_papers", {"project_id": self.proj_a.id},
            meta={"project_id": self.proj_b.id})
        self.assertTrue(captured2, "executor must be called")
        self.assertEqual(captured2[0]["context"].project_id, self.proj_a.id,
                         "client meta project_id must not act as server-bound context")

    def test_mcp_bootstrap_valid_selector(self):
        """§18.2: without a server-bound project, an explicit valid selector is
        a single-user transport bootstrap only — it creates a frozen context."""
        self.case_id = "MCP-BOOTSTRAP-VALID"
        self.expected_pre_fix = "PASS"
        from agent.context import ToolExecutionContext

        result, captured = self._call_mcp_spy(
            "list_project_papers", {"project_id": self.proj_a.id})
        self.assertTrue(captured, "executor must be called")
        self.assertIsInstance(captured[0]["context"], ToolExecutionContext)
        self.assertEqual(captured[0]["context"].project_id, self.proj_a.id,
                         "bootstrap must create the context for the validated selector")

    def test_mcp_selector_error_shape_uniform(self):
        """§18.2: missing / invalid / nonexistent selectors MUST return the same
        stable error shape — no existence or reason disclosure."""
        self.case_id = "MCP-SELECTOR-ERROR-SHAPE"
        self.expected_pre_fix = "PASS"
        missing = self._call_mcp("list_project_papers", {})
        invalid = self._call_mcp("list_project_papers", {"project_id": "abc"})
        nonexistent = self._call_mcp("list_project_papers", {"project_id": 99999999})
        for result in (missing, invalid, nonexistent):
            self.assertTrue(result.is_error, "selector failure must be an error")
        payloads = [json.loads(self._extract_text(r)) for r in (missing, invalid, nonexistent)]
        self.assertEqual(payloads[0], payloads[1])
        self.assertEqual(payloads[1], payloads[2])
        for payload in payloads:
            self.assertEqual(payload.get("error"), "project_not_found")

    def test_mcp_bound_config_invalid_fails_closed(self):
        """§19: PAPERLENS_MCP_PROJECT_ID must be a positive integer pointing at
        a REAL project. Invalid config (negative/zero/non-numeric) or a
        nonexistent bound project returns the unified selector error and never
        silently produces an empty-project success."""
        self.case_id = "MCP-BOUND-CONFIG-INVALID"
        self.expected_pre_fix = "PASS"
        for bad in ("-5", "0", "abc", "1.5"):
            with mock.patch.dict(os.environ, {"PAPERLENS_MCP_PROJECT_ID": bad}):
                result = self._call_mcp("list_project_papers",
                                        {"project_id": self.proj_a.id})
            self.assertTrue(result.is_error,
                            f"invalid bound config {bad!r} must fail closed")
            payload = json.loads(self._extract_text(result))
            self.assertEqual(payload.get("error"), "project_not_found")
        # a positive integer bound that does not exist also fails closed
        with mock.patch.dict(os.environ, {"PAPERLENS_MCP_PROJECT_ID": "99999999"}):
            result = self._call_mcp("list_project_papers", {"project_id": self.proj_a.id})
        self.assertTrue(result.is_error,
                        "nonexistent bound project must fail closed")
        self.assertEqual(json.loads(self._extract_text(result)).get("error"),
                         "project_not_found")

    def _extract_text(self, result) -> str:
        """Extract text content from MCP CallToolResult."""
        for c in (result.content or []):
            if hasattr(c, 'text'):
                return c.text
        return json.dumps(result, default=str)

    def _call_rag(self, question):
        with mock.patch("rag.retrieval.embed", side_effect=_mock_embed_return), \
             mock.patch("rag.retrieval._rcs_summary",
                        new=mock.AsyncMock(side_effect=_mock_rcs_summary)):
            return self._call_mcp("query_project_rag",
                                  {"project_id": self.proj_a.id, "question": question})

    def test_mcp_list_own_positive(self):
        """§12.3: MCP list own positive control."""
        self.case_id = "MCP-LIST-OWN"
        self.expected_pre_fix = "PASS"
        result = self._call_mcp("list_project_papers", {"project_id": self.proj_a.id})
        text = self._extract_text(result)
        self.assertIn("Own Included Paper", text, "positive: own visible via MCP")

    def test_mcp_list_foreign_unlinked_invisible(self):
        """MCP inventory: foreign/unlinked invisible; excluded only with
        explicit status and no evidence."""
        self.case_id = "MCP-LIST-FOREIGN-EXCLUDED"
        self.expected_pre_fix = "PASS"
        result = self._call_mcp("list_project_papers", {"project_id": self.proj_a.id})
        payload = json.loads(self._extract_text(result))
        papers = payload.get("papers") or []
        # Reuse the same inventory contract as the in-app list
        from agent.scope_failing_tests import ListProjectPapersScopeTest
        ListProjectPapersScopeTest._assert_inventory(self, papers)
        self.assertNotIn("Foreign Paper B", self._extract_text(result),
                         "foreign must not appear via MCP")
        self.assertNotIn("Unlinked Global Paper", self._extract_text(result),
                         "unlinked must not appear via MCP")

    def test_mcp_rag_own_positive(self):
        """§12.3: MCP RAG own positive control."""
        self.case_id = "MCP-RAG-OWN"
        self.expected_pre_fix = "PASS"
        text = self._extract_text(self._call_rag("selective state space"))
        self.assertIn("OWN_SENTINEL", text, "positive: own evidence via MCP RAG")

    def test_mcp_rag_foreign_negative_with_own_positive(self):
        """§13.3: foreign must be absent AND own positive must be present."""
        self.case_id = "MCP-RAG-FOREIGN-POSITIVE"
        self.expected_pre_fix = "PASS"
        text = self._extract_text(self._call_rag("selective state space graph attention"))
        self.assertIn("OWN_SENTINEL", text,
                      "positive: own evidence must be present via MCP RAG")
        self.assertNotIn("FOREIGN_SENTINEL", text,
                         "foreign must not leak via MCP RAG")
        payload = json.loads(text)
        evidence = payload.get("evidence") or []
        self.assertTrue(evidence, "non-vacuous: evidence must not be empty")

    def test_mcp_graph_own_positive(self):
        """§12.4: MCP graph own nodes + own edge."""
        self.case_id = "MCP-GRAPH-OWN"
        self.expected_pre_fix = "PASS"
        result = self._call_mcp("get_project_citation_graph", {"project_id": self.proj_a.id})
        text = self._extract_text(result)
        self.assertIn("Own Included Paper", text, "positive: own graph node via MCP")
        payload = json.loads(text)
        node_ids = {n.get("id") for n in payload.get("graph", {}).get("nodes", [])}
        self.assertEqual(node_ids, {self.paper_own.id, self.paper_own2.id},
                         "positive: exactly the own node pair via MCP")
        edges = payload.get("graph", {}).get("edges", [])
        self.assertTrue(edges, "positive: own edge must exist via MCP")

    def test_mcp_graph_forbidden_filtered(self):
        self.case_id = "MCP-GRAPH-FORBIDDEN"
        self.expected_pre_fix = "PASS"
        result = self._call_mcp("get_project_citation_graph", {"project_id": self.proj_a.id})
        payload = json.loads(self._extract_text(result))
        node_ids = {n.get("id") for n in payload.get("graph", {}).get("nodes", [])}
        forbidden = {self.paper_foreign.id, self.paper_excluded.id, self.paper_unlinked.id}
        self.assertFalse(node_ids & forbidden,
                         f"forbidden nodes leaked via MCP: {node_ids & forbidden}")
        titles = {n.get("title", "") for n in payload.get("graph", {}).get("nodes", [])}
        self.assertNotIn("Foreign Paper B", titles)
        self.assertNotIn("Unlinked Global Paper", titles)

    def test_mcp_output_schema_declared_and_validated(self):
        """§13.3: read the REAL exported tool declaration; each project MCP tool
        MUST declare outputSchema AND the CallToolResult MUST carry
        structured_content, which is the authoritative output validated against
        that schema. Parsing the text content is NOT treated as structured
        output. Pre-fix neither exists → RED contract."""
        self.case_id = "MCP-OUTPUT-SCHEMA"
        self.expected_pre_fix = "PASS"
        from mcp_server import server as mcp_server

        names = set(mcp_server.MCP_PROJECT_TOOL_NAMES)
        self.assertTrue(names, "MCP project tool set must be non-empty")
        exported = {tool.name for tool in mcp_server._TOOLS}
        self.assertEqual(exported & names, names,
                         "every declared project MCP tool must be exported — "
                         "a tool missing from _TOOLS must not be silently skipped")
        offline_calls = {
            "list_project_papers": {"project_id": self.proj_a.id},
            "query_project_rag": {"project_id": self.proj_a.id,
                                  "question": "selective state space"},
            "get_project_citation_graph": {"project_id": self.proj_a.id},
        }
        for name in sorted(names):
            with self.subTest(tool=name):
                tool = next((t for t in mcp_server._TOOLS if t.name == name), None)
                self.assertIsNotNone(tool, f"declared tool {name} not exported")
                schema = getattr(tool, "output_schema", None)
                self.assertIsNotNone(
                    schema,
                    f"RED contract: MCP tool {name} declares no outputSchema")
                if schema is None:
                    continue
                if name not in offline_calls:
                    # search_papers: structured-result validation is explicitly
                    # DEFERRED (network-bound; schema-only check here). It must
                    # NOT be counted as validated output in the report.
                    continue
                with mock.patch("rag.retrieval.embed", side_effect=_mock_embed_return), \
                     mock.patch("rag.retrieval._rcs_summary",
                                new=mock.AsyncMock(side_effect=_mock_rcs_summary)):
                    result = self._call_mcp(name, offline_calls[name])
                self.assertFalse(result.is_error, "valid call must not be an error")
                structured = getattr(result, "structured_content", None)
                self.assertIsNotNone(
                    structured,
                    "RED contract: CallToolResult.structured_content missing — "
                    "text content is not authoritative structured output")
                if structured is None:
                    continue
                import jsonschema
                jsonschema.validate(instance=structured, schema=schema)


# =====================================================================
# Task 1.4: project_id override (§7, §12.9)
# =====================================================================

class ProjectIdOverrideTest(ScopeFixtureMixin, NetworkGuardTestCaseMixin, TransactionTestCase):
    """§7/§13.4: three layers — schema (no auth fields), execution (server binds
    project), audit (tool_scope_violation event with safe payload)."""

    def setUp(self):
        super().setUp()
        self.setUpScopeFixture()

    class _StatefulFC:
        """First call returns tool calls, then a plain final answer (no loop)."""

        def __init__(self, tool_calls: list[dict], final_content: str = "answer"):
            self._tool_calls = tool_calls
            self._final_content = final_content
            self.calls = 0

        def complete_with_tools(self, messages, tools, **kwargs):
            self.calls += 1
            if self.calls == 1:
                return {"content": "", "reasoning_content": "",
                        "tool_calls": self._tool_calls}
            return {"content": self._final_content, "reasoning_content": "",
                    "tool_calls": []}

        def complete(self, *a, **k):
            return {"content": self._final_content, "usage": {}}

    def test_SCHEMA_no_auth_fields(self):
        """§13.4: assert tool set non-empty first; per-tool: no auth fields,
        no unknown input properties."""
        self.case_id = "OVERRIDE-SCHEMA"
        self.expected_pre_fix = "PASS"
        from agent.project_tools import PROJECT_AGENT_TOOLS

        self.assertTrue(PROJECT_AGENT_TOOLS, "tool set must be non-empty")
        for tool in PROJECT_AGENT_TOOLS:
            fn = tool.get("function", {})
            params = fn.get("parameters", {})
            props = params.get("properties", {})
            with self.subTest(tool=fn.get("name")):
                self.assertNotIn("project_id", props,
                                 f"{fn.get('name')} exposes project_id")
                self.assertNotIn("run_id", props)
                self.assertNotIn("session_id", props)
                self.assertNotIn("actor", props)
                self.assertIs(params.get("additionalProperties"), False,
                              "additionalProperties must be false")

    def test_EXECUTION_model_override_blocked(self):
        """§13.4: model sends project B, the trusted context stays A; the
        executor receives the frozen context and NO auth fields; the attempted
        override is recorded on the tool_call event and as a violation."""
        self.case_id = "OVERRIDE-EXECUTION"
        self.expected_pre_fix = "PASS"
        from agent.chat_loop import ChatAgentLoop
        from agent.context import ToolExecutionContext
        captured = []
        a_id, b_id = self.proj_a.id, self.proj_b.id
        forged = json.dumps({"project_id": b_id, "question": "SECRET_QUESTION_MARKER"})

        async def fake_exec(context, name, args):
            captured.append({"context": context, "name": name, "args": dict(args)})
            return {"evidence": []}

        fc = self._StatefulFC([{"id": "c1", "name": "query_project_rag",
                                "arguments": forged}])
        events: list[dict] = []

        async def drive():
            async for ev in ChatAgentLoop(a_id, tool_executor=fake_exec).run("q", history=None):
                events.append(ev)

        with mock.patch("llm.deepseek.DeepSeekClient") as mc:
            mc.return_value = fc
            asyncio.run(drive())
        self.assertTrue(captured, "executor must be called")
        for c in captured:
            self.assertIsInstance(c["context"], ToolExecutionContext,
                                  "executor must receive a frozen context")
            self.assertEqual(c["context"].project_id, a_id,
                             f"executor received project_id={c['context'].project_id} not {a_id}")
            self.assertNotIn("project_id", c["args"],
                             "auth fields must not reach the tool implementation")
        tool_call_events = [e for e in events if e.get("event") == "tool_call"]
        self.assertTrue(tool_call_events, "tool_call event must be emitted")
        self.assertEqual(tool_call_events[0]["data"].get("model_supplied_project_id"), b_id,
                         "attempted override must be recorded for audit")
        violation = [e for e in events if e.get("event") == "tool_scope_violation"]
        self.assertTrue(violation, "tool_scope_violation event must be emitted")

    def test_AUDIT_scope_violation_event(self):
        """§13.4: audit via the formal harness path. The tool_scope_violation
        event must carry project/run/tool/rejected field/safe summary and must
        NOT contain the full prompt, keys, paper bodies or full args payload."""
        self.case_id = "OVERRIDE-AUDIT"
        self.expected_pre_fix = "PASS"
        from agent.harness import ProjectAgentHarness
        a_id, b_id = self.proj_a.id, self.proj_b.id
        forged = json.dumps({"project_id": b_id, "question": "SECRET_QUESTION_MARKER"})

        async def fake_exec(context, name, args):
            return {"papers": [], "count": 0}

        fc = self._StatefulFC([{"id": "c1", "name": "list_project_papers",
                                "arguments": forged}])
        with mock.patch("llm.deepseek.DeepSeekClient") as mc:
            mc.return_value = fc
            h = ProjectAgentHarness(a_id, use_llm=True, tool_executor=fake_exec)
            result = asyncio.run(h.run("q"))
        events = result.get("events", [])
        violation = [e for e in events if e.get("event") == "tool_scope_violation"]
        self.assertTrue(violation,
                        "expected_pre_fix FAIL: tool_scope_violation event must exist")
        if violation:
            data = violation[0].get("data", {})
            self.assertEqual(data.get("project_id"), a_id)
            self.assertEqual(data.get("tool"), "list_project_papers")
            self.assertEqual(data.get("rejected_fields"), ["project_id"],
                             "rejected authorization fields must be recorded by name")
            run_id = data.get("run_id")
            self.assertIsInstance(run_id, int, "run_id must be a valid numeric id")
            self.assertGreater(run_id, 0, "run_id must be non-empty")
            # Explicit safe audit summary: numeric attempted project id or its hash
            attempted = data.get("attempted_project_id", data.get("attempted_value"))
            attempted_hash = data.get("attempted_value_hash")
            self.assertTrue(
                attempted == b_id or (isinstance(attempted_hash, str) and attempted_hash),
                "audit must carry the numeric attempted project id or a hash of it")
            raw = json.dumps(data, default=str)
            self.assertNotIn("SECRET_QUESTION_MARKER", raw,
                             "audit must not contain the full arguments payload")
            self.assertNotIn("sk-", raw, "audit must not contain API keys")
            self.assertNotIn("DEEPSEEK_API_KEY", raw)
            self.assertNotIn("SENTINEL", raw, "audit must not contain paper bodies")
            self.assertNotIn("Traceback", raw)

    def test_REJECTED_FIELDS_all_authorization_fields_audited(self):
        """§18.1: every model-smuggled authorization field must be recorded by
        NAME (project_id/run_id/session_id/actor); values, prompt and payload
        must never reach the violation event."""
        self.case_id = "OVERRIDE-REJECTED-FIELDS"
        self.expected_pre_fix = "PASS"
        from agent.chat_loop import ChatAgentLoop
        a_id, b_id = self.proj_a.id, self.proj_b.id
        forged = json.dumps({
            "project_id": b_id,
            "run_id": 777,
            "session_id": 888,
            "actor": "evil_actor",
            "question": "SECRET_QUESTION_MARKER",
        })
        captured: list[dict] = []

        async def fake_exec(context, name, args):
            captured.append({"context": context, "name": name, "args": dict(args)})
            return {"evidence": []}

        fc = self._StatefulFC([{"id": "c1", "name": "query_project_rag",
                                "arguments": forged}])
        events: list[dict] = []

        async def drive():
            async for ev in ChatAgentLoop(a_id, tool_executor=fake_exec).run("q", history=None):
                events.append(ev)

        with mock.patch("llm.deepseek.DeepSeekClient") as mc:
            mc.return_value = fc
            asyncio.run(drive())
        self.assertTrue(captured, "executor must be called")
        self.assertNotIn("project_id", captured[0]["args"],
                         "project_id must not reach the tool")
        self.assertNotIn("run_id", captured[0]["args"])
        self.assertNotIn("session_id", captured[0]["args"])
        self.assertNotIn("actor", captured[0]["args"])
        self.assertEqual(captured[0]["args"].get("question"), "SECRET_QUESTION_MARKER",
                         "legitimate arguments must still flow")
        violation = [e for e in events if e.get("event") == "tool_scope_violation"]
        self.assertTrue(violation, "tool_scope_violation must be emitted")
        data = violation[0].get("data", {})
        self.assertEqual(data.get("rejected_fields"),
                         ["actor", "project_id", "run_id", "session_id"],
                         "all smuggled field NAMES must be audited")
        self.assertEqual(data.get("attempted_project_id"), b_id)
        raw = json.dumps(data, default=str)
        self.assertNotIn("evil_actor", raw, "field values must not be recorded")
        self.assertNotIn("777", raw, "field values must not be recorded")
        self.assertNotIn("888", raw, "field values must not be recorded")
        self.assertNotIn("SECRET_QUESTION_MARKER", raw,
                         "the full payload must not reach the event")

    def test_CONTEXT_MISMATCH_rejected(self):
        """§18.1: ChatAgentLoop(project_id=A, context=B) must be rejected —
        the frozen context is the only trusted identity."""
        self.case_id = "OVERRIDE-CONTEXT-MISMATCH"
        self.expected_pre_fix = "PASS"
        from agent.chat_loop import ChatAgentLoop
        from agent.context import create_context

        with self.assertRaises(ValueError):
            ChatAgentLoop(self.proj_a.id,
                          context=create_context(self.proj_b.id))


# =====================================================================
# Task 1.5: Citation self-assertion (§8, §12.10)
# =====================================================================

class CitationSelfAssertionTest(ScopeFixtureMixin, NetworkGuardTestCaseMixin, TransactionTestCase):
    """§8/§13.5: crafted evidence through the real harness path. Every case
    first asserts the target citation actually exists (non-vacuous), then
    checks reference_resolved directly."""

    def setUp(self):
        super().setUp()
        self.setUpScopeFixture()

    def _craft_and_check(self, evidence_items, answer_markers) -> dict:
        """Run the formal harness path with crafted evidence, return quality."""
        from agent.harness import ProjectAgentHarness
        a_id = self.proj_a.id

        class FC:
            def __init__(self):
                self.calls = 0

            def complete_with_tools(self, messages, tools, **kwargs):
                self.calls += 1
                if self.calls == 1:
                    return {"content": "", "reasoning_content": "",
                            "tool_calls": [{"id": "c1", "name": "query_project_rag",
                                            "arguments": json.dumps({"question": "q"})}]}
                return {"content": answer_markers, "reasoning_content": "",
                        "tool_calls": []}

            def complete(self, *a, **k):
                return {"content": answer_markers, "usage": {}}

        async def fake_exec(context, name, args):
            if name == "query_project_rag":
                return {"evidence": evidence_items}
            return {}

        with mock.patch("llm.deepseek.DeepSeekClient") as mc:
            mc.return_value = FC()
            h = ProjectAgentHarness(a_id, use_llm=True, tool_executor=fake_exec)
            result = asyncio.run(h.run("test"))
        q = next((e["data"] for e in result.get("events", [])
                  if e.get("event") == "quality_check"), {})
        self.assertTrue(q, "quality_check event must be emitted")
        return q

    def _citation_for_marker(self, q, marker):
        cits = [c for c in q.get("citations", []) if c.get("marker") == marker]
        self.assertTrue(cits, f"non-vacuous: target citation {marker} must exist")
        return cits[0]

    def _fulltext_evidence(self, paper_id, chunk_id, chunk_index, content_hash,
                           marker="pqac-own", embedding_version=None,
                           project_id=None, omit_project=False):
        """Crafted fulltext evidence in the typed envelope shape.

        Carries BOTH the stable binding fields (chunk_id/content_hash/...) and
        the legacy display fields (chunk_index/title/summary/...). chunk_index
        is display-only — resolution is decided by the database resolver.
        `omit_project` removes the envelope project identity (missing-identity
        red contract); `project_id` overrides it (mismatch/malformed tests)."""
        active = _active_embedding_meta()
        resolved_version = embedding_version or str(active["embedding_version"])
        resolved_project = project_id if project_id is not None else self.proj_a.id
        item = {
            "source_marker": marker, "citation": marker,
            "paper_id": paper_id, "chunk_id": chunk_id, "content_hash": content_hash,
            "evidence_type": "fulltext",
            "embedding_version": resolved_version,
            "excerpt": "s", "section": "x", "page_start": 1, "page_end": 2,
            "retrieval_sources": ["hybrid_rag"], "retrieval_scores": {"rcs": 8.0},
            "title": "T", "summary": "s", "chunk_index": chunk_index,
        }
        if not omit_project:
            item["project_id"] = resolved_project
            try:
                identity_project = int(resolved_project)
            except (TypeError, ValueError):
                identity_project = 0
            item["evidence_id"] = make_evidence_id(
                identity_project, paper_id, chunk_id, content_hash, resolved_version)
        else:
            item["evidence_id"] = make_evidence_id(
                self.proj_a.id, paper_id, chunk_id, content_hash, resolved_version)
        return [item]

    def test_CIT_NONEXISTENT(self):
        """expected: PASS (Task 3.3) — nonexistent chunk_id must NOT resolve."""
        self.case_id = "CIT-NONEXISTENT"
        self.expected_pre_fix = "PASS"
        q = self._craft_and_check(
            self._fulltext_evidence(self.text_own.paper_id, 99999, 99999, "h_own"),
            "answer [cite:pqac-own]")
        cit = self._citation_for_marker(q, "pqac-own")
        self.assertIs(cit.get("reference_resolved"), False,
                      "P0: nonexistent chunk marked resolved")

    def test_CIT_FOREIGN(self):
        """expected: PASS (Task 3.3) — foreign project chunk must NOT resolve."""
        self.case_id = "CIT-FOREIGN"
        self.expected_pre_fix = "PASS"
        q = self._craft_and_check(
            self._fulltext_evidence(self.text_foreign.paper_id,
                                    self.text_foreign.id, self.text_foreign.chunk_index,
                                    "h_foreign", marker="pqac-foreign"),
            "answer [cite:pqac-foreign]")
        cit = self._citation_for_marker(q, "pqac-foreign")
        self.assertIs(cit.get("reference_resolved"), False,
                      "P0: foreign project chunk marked resolved")

    def test_CIT_WRONG_HASH(self):
        """§13.5: direct assertIs — real own chunk, wrong content_hash must NOT resolve."""
        self.case_id = "CIT-WRONG-HASH"
        self.expected_pre_fix = "PASS"
        q = self._craft_and_check(
            self._fulltext_evidence(self.text_own.paper_id,
                                    self.text_own.id, self.text_own.chunk_index,
                                    "WRONG_HASH_NOT_MATCHING"),
            "answer [cite:pqac-own]")
        cit = self._citation_for_marker(q, "pqac-own")
        self.assertIs(cit.get("reference_resolved"), False,
                      "P0: wrong content_hash still resolved")

    def test_CIT_INACTIVE_VERSION(self):
        """Task 3.3 red contract (§19): a real chunk with a matching hash but a
        STALE embedding version must NOT resolve. Red until the CitationResolver
        enforces the active index version."""
        self.case_id = "CIT-INACTIVE-VERSION"
        self.expected_pre_fix = "PASS"
        q = self._craft_and_check(
            self._fulltext_evidence(self.text_own_stale.paper_id,
                                    self.text_own_stale.id, self.text_own_stale.chunk_index,
                                    "h_own_stale", marker="pqac-own-stale",
                                    embedding_version="stale-version"),
            "answer [cite:pqac-own-stale]")
        cit = self._citation_for_marker(q, "pqac-own-stale")
        self.assertIs(cit.get("reference_resolved"), False,
                      "inactive-version chunk must not resolve")

    def test_CIT_MARKER_ONLY(self):
        """expected: PASS baseline — marker without evidence must not resolve."""
        self.case_id = "CIT-MARKER-ONLY"
        self.expected_pre_fix = "PASS"
        answer = "answer [cite:pqac-nonexistent]"
        q = self._craft_and_check([], answer)
        self.assertIn("pqac-nonexistent", answer, "marker must be present in answer")
        target = [c for c in q.get("citations", [])
                  if c.get("marker") == "pqac-nonexistent"]
        self.assertEqual(target, [],
                         "marker without evidence must produce no citation entry")

    def test_CIT_VALID(self):
        """§13.5 positive: real own ACTIVE chunk + matching content_hash resolves."""
        self.case_id = "CIT-VALID"
        self.expected_pre_fix = "PASS"
        q = self._craft_and_check(
            self._fulltext_evidence(self.text_own.paper_id,
                                    self.text_own.id, self.text_own.chunk_index, "h_own"),
            "answer [cite:pqac-own]")
        cit = self._citation_for_marker(q, "pqac-own")
        self.assertTrue(cit.get("reference_resolved", False),
                        "valid own chunk must resolve")
        self.assertEqual(cit.get("reference_resolution_status"), "resolved")
        self.assertEqual(cit.get("resolution_reason"), "resolved")
        # Task 3.4: quality dimensions are separate fields
        self.assertEqual(q.get("retrieval_status"), "fulltext")
        self.assertEqual(q.get("reference_resolution_status"), "resolved")
        self.assertEqual(q.get("citation_binding_status"), "fully_bound")
        self.assertEqual(q.get("claim_support_status"), "pending")
        self.assertEqual(q.get("legacy_unresolved_count"), 0)
        self.assertEqual(cit.get("claim_support_status"), "pending")

    def test_CIT_LEGACY_UNRESOLVED(self):
        """Task 3.5: a legacy-format fulltext item (no chunk_id/content_hash)
        must be marked legacy_unresolved, never promoted to resolved."""
        self.case_id = "CIT-LEGACY-UNRESOLVED"
        self.expected_pre_fix = "PASS"
        legacy = [{
            "source_marker": "pqac-own", "citation": "pqac-own",
            "paper_id": self.text_own.paper_id, "chunk_index": self.text_own.chunk_index,
            "evidence_type": "fulltext",
            "title": "T", "summary": "s",
        }]
        q = self._craft_and_check(legacy, "answer [cite:pqac-own]")
        cit = self._citation_for_marker(q, "pqac-own")
        self.assertIs(cit.get("reference_resolved"), False,
                      "legacy item must not auto-upgrade to resolved")
        self.assertEqual(cit.get("resolution_reason"), "legacy_unresolved",
                         "legacy item must be marked legacy_unresolved")

    def test_CIT_MISSING_PROJECT(self):
        """§20.3: an envelope WITHOUT project identity must fail closed even
        when chunk/hash/version all match the database."""
        self.case_id = "CIT-MISSING-PROJECT"
        self.expected_pre_fix = "PASS"
        q = self._craft_and_check(
            self._fulltext_evidence(self.text_own.paper_id,
                                    self.text_own.id, self.text_own.chunk_index,
                                    "h_own", omit_project=True),
            "answer [cite:pqac-own]")
        cit = self._citation_for_marker(q, "pqac-own")
        self.assertIs(cit.get("reference_resolved"), False,
                      "missing project identity must fail closed")
        self.assertEqual(cit.get("resolution_reason"), "missing_project_identity")

    def test_CIT_MALFORMED_IDENTITY(self):
        """§20.3/§21.2: malformed project/chunk/paper ids fail closed in the
        RESOLVER itself (defense in depth for callers bypassing the harness
        parser); a valid sibling still resolves in the same batch."""
        self.case_id = "CIT-MALFORMED-IDENTITY"
        self.expected_pre_fix = "PASS"
        from agent.citations import CitationResolver
        from agent.evidence import evidence_identity_key
        malformed = self._fulltext_evidence(self.text_own.paper_id,
                                            self.text_own.id, self.text_own.chunk_index,
                                            "h_own", marker="pqac-malformed",
                                            project_id="abc")[0]
        valid = self._fulltext_evidence(self.text_own.paper_id,
                                        self.text_own.id, self.text_own.chunk_index,
                                        "h_own", marker="pqac-valid")[0]
        resolutions = CitationResolver(self.proj_a.id).resolve([malformed, valid])
        malformed_res = resolutions[evidence_identity_key(malformed)]
        self.assertIs(malformed_res.reference_resolved, False,
                      "malformed project id must fail closed")
        self.assertEqual(malformed_res.reason_code, "malformed_identity")
        valid_res = resolutions[evidence_identity_key(valid)]
        self.assertTrue(valid_res.reference_resolved,
                        "valid sibling must still resolve in the same batch")

    def test_CIT_EVIDENCE_ID_MISMATCH(self):
        """§21.1: the resolver must RECOMPUTE the canonical evidence id — a
        forged declared id on a real chunk fails closed."""
        self.case_id = "CIT-EVIDENCE-ID-MISMATCH"
        self.expected_pre_fix = "PASS"
        from agent.citations import CitationResolver
        from agent.evidence import evidence_identity_key
        forged = self._fulltext_evidence(self.text_own.paper_id,
                                         self.text_own.id, self.text_own.chunk_index,
                                         "h_own", marker="pqac-forged")[0]
        forged["evidence_id"] = "ev-" + "0" * 64  # forged id
        valid = self._fulltext_evidence(self.text_own.paper_id,
                                        self.text_own.id, self.text_own.chunk_index,
                                        "h_own", marker="pqac-valid")[0]
        resolutions = CitationResolver(self.proj_a.id).resolve([forged, valid])
        forged_res = resolutions[evidence_identity_key(forged)]
        self.assertIs(forged_res.reference_resolved, False,
                      "declared id must equal the recomputed canonical id")
        self.assertEqual(forged_res.reason_code, "evidence_id_mismatch")
        valid_res = resolutions[evidence_identity_key(valid)]
        self.assertTrue(valid_res.reference_resolved,
                        "valid sibling must still resolve")

    def test_CIT_DUPLICATE_EVIDENCE_ID_DIFFERENT_PAYLOAD(self):
        """§21.1: two DIFFERENT payloads sharing one declared evidence_id are
        ambiguous — the whole identity fails closed with the same result in
        either input order; a distinct valid sibling still resolves."""
        self.case_id = "CIT-DUPLICATE-EVIDENCE-ID-DIFFERENT-PAYLOAD"
        self.expected_pre_fix = "PASS"
        from agent.citations import CitationResolver
        from agent.evidence import evidence_identity_key
        payload_a = self._fulltext_evidence(self.text_own.paper_id,
                                            self.text_own.id, self.text_own.chunk_index,
                                            "h_own", marker="pqac-dup")[0]
        payload_b = self._fulltext_evidence(self.text_own.paper_id,
                                            99999, 99999, "h_own", marker="pqac-dup")[0]
        payload_b["evidence_id"] = payload_a["evidence_id"]
        meta = _active_embedding_meta()
        extra = Text.objects.create(
            paper=self.paper_own, docname="own dup-test chunk", chunk_index=3,
            content="OWN_DUP_SENTINEL duplicate-test content",
            embedding=_e1024(8.0), embedding_model=meta["embedding_model"],
            embedding_dim=1024, embedding_version=meta["embedding_version"],
            content_hash="h_own_dup", citation_key="pqac-own-dup",
            search_vector="own duplicate-test content")
        valid = self._fulltext_evidence(extra.paper_id,
                                        extra.id, extra.chunk_index,
                                        "h_own_dup", marker="pqac-valid")[0]
        key = evidence_identity_key(payload_a)
        for ordered in ([payload_a, payload_b], [payload_b, payload_a]):
            resolutions = CitationResolver(self.proj_a.id).resolve(ordered)
            dup = resolutions[key]
            self.assertIs(dup.reference_resolved, False,
                          "ambiguous duplicate identity must fail closed")
            self.assertEqual(dup.reason_code, "duplicate_identity_conflict",
                             "result must not depend on input order")
        resolutions = CitationResolver(self.proj_a.id).resolve(
            [payload_a, payload_b, valid])
        self.assertTrue(resolutions[evidence_identity_key(valid)].reference_resolved,
                        "distinct valid sibling must resolve")

    def test_CIT_MISSING_EVIDENCE_ID(self):
        """§22: evidence_id is REQUIRED non-empty — a full envelope with
        chunk/hash/version but NO declared evidence_id must fail closed in the
        resolver; a valid sibling is the positive control."""
        self.case_id = "CIT-MISSING-EVIDENCE-ID"
        self.expected_pre_fix = "PASS"
        from agent.citations import CitationResolver
        from agent.evidence import evidence_identity_key
        missing = self._fulltext_evidence(self.text_own.paper_id,
                                          self.text_own.id, self.text_own.chunk_index,
                                          "h_own", marker="pqac-noeid")[0]
        del missing["evidence_id"]
        valid = self._fulltext_evidence(self.text_own.paper_id,
                                        self.text_own.id, self.text_own.chunk_index,
                                        "h_own", marker="pqac-valid")[0]
        resolutions = CitationResolver(self.proj_a.id).resolve([missing, valid])
        missing_res = resolutions[evidence_identity_key(missing)]
        self.assertIs(missing_res.reference_resolved, False,
                      "missing evidence_id must fail closed")
        self.assertEqual(missing_res.reason_code, "evidence_id_mismatch")
        valid_res = resolutions[evidence_identity_key(valid)]
        self.assertTrue(valid_res.reference_resolved,
                        "valid sibling must still resolve in the same batch")

    def test_COLLECT_FOREIGN_METADATA_ONLY(self):
        """§22: foreign-project metadata ALONE must not count as metadata
        retrieval — retrieval_status is none and the legacy count expresses
        it; no valid sibling is used to mask the assertion."""
        self.case_id = "COLLECT-FOREIGN-METADATA-ONLY"
        self.expected_pre_fix = "PASS"
        meta_foreign = {
            "evidence_type": "metadata", "project_id": self.proj_b.id,
            "paper_id": self.paper_own.id,
            "source_marker": "pqac-foreign", "citation": "pqac-foreign", "title": "T",
        }
        q = self._craft_and_check([meta_foreign], "answer [cite:pqac-foreign]")
        self.assertGreaterEqual(q.get("evidence_count", 0), 1,
                                "legacy structure is kept for audit")
        self.assertEqual(q.get("retrieval_status"), "none",
                         "foreign metadata must not count as metadata retrieval")
        self.assertGreaterEqual(q.get("legacy_unresolved_count", 0), 1)
        cit = self._citation_for_marker(q, "pqac-foreign")
        self.assertEqual(cit.get("resolution_reason"), "legacy_unresolved")

    def test_EVIDENCE_ID_MISMATCH(self):
        """§21.1: the harness parser drops a fulltext structure whose declared
        evidence_id does not match the recomputed canonical id; a valid
        sibling is the positive control."""
        self.case_id = "EVIDENCE-ID-MISMATCH"
        self.expected_pre_fix = "PASS"
        forged = self._fulltext_evidence(self.text_own.paper_id,
                                         self.text_own.id, self.text_own.chunk_index,
                                         "h_own", marker="pqac-forged")[0]
        forged["evidence_id"] = "ev-" + "0" * 64
        valid = self._fulltext_evidence(self.text_own.paper_id,
                                        self.text_own.id, self.text_own.chunk_index,
                                        "h_own", marker="pqac-valid")[0]
        q = self._craft_and_check([forged, valid],
                                  "answer [cite:pqac-forged] [cite:pqac-valid]")
        self.assertEqual(q.get("evidence_count"), 1,
                         "forged-id structure must be dropped by the parser")
        forged_cits = [c for c in q.get("citations", [])
                       if c.get("marker") == "pqac-forged"]
        self.assertEqual(forged_cits, [])
        self.assertTrue(self._citation_for_marker(q, "pqac-valid")
                        .get("reference_resolved", False))

    def test_COLLECT_FULLTEXT_WRONG_TYPES(self):
        """§21.2: fields present with WRONG TYPES (non-positive ids, bool,
        non-string scalars) are malformed, not legacy — dropped without
        polluting evidence_count; a valid sibling is the positive control."""
        self.case_id = "COLLECT-FULLTEXT-WRONG-TYPES"
        self.expected_pre_fix = "PASS"
        wrong = {
            "evidence_type": "fulltext",
            "project_id": "abc", "paper_id": True, "chunk_id": [],
            "content_hash": "h", "embedding_version": "v", "evidence_id": "x",
            "source_marker": "pqac-wrong", "citation": "pqac-wrong",
        }
        valid = self._fulltext_evidence(self.text_own.paper_id,
                                        self.text_own.id, self.text_own.chunk_index,
                                        "h_own", marker="pqac-valid")[0]
        q = self._craft_and_check([wrong, valid], "answer [cite:pqac-valid]")
        self.assertEqual(q.get("evidence_count"), 1,
                         "wrong-typed fulltext must be dropped as malformed")
        self.assertEqual(q.get("retrieval_status"), "fulltext")
        wrong_cits = [c for c in q.get("citations", [])
                      if c.get("marker") == "pqac-wrong"]
        self.assertEqual(wrong_cits, [])
        self.assertTrue(self._citation_for_marker(q, "pqac-valid")
                        .get("reference_resolved", False))

    def test_COLLECT_METADATA_MISSING_PROJECT(self):
        """§21.2: metadata without project identity never counts as metadata
        retrieval (downgraded); a valid sibling metadata is the positive
        control."""
        self.case_id = "COLLECT-METADATA-MISSING-PROJECT"
        self.expected_pre_fix = "PASS"
        meta_missing = {
            "evidence_type": "metadata", "paper_id": self.paper_own.id,
            "source_marker": "pqac-mmeta", "citation": "pqac-mmeta", "title": "T",
        }
        q1 = self._craft_and_check([meta_missing], "answer [cite:pqac-mmeta]")
        self.assertGreaterEqual(q1.get("evidence_count", 0), 1,
                                "legacy structure is kept for audit")
        self.assertNotEqual(q1.get("retrieval_status"), "metadata",
                            "missing-project metadata must not count as metadata")
        meta_valid = {
            "evidence_type": "metadata", "project_id": self.proj_a.id,
            "paper_id": self.paper_own.id,
            "source_marker": "pqac-mvalid", "citation": "pqac-mvalid", "title": "T",
        }
        q2 = self._craft_and_check([meta_missing, meta_valid],
                                   "answer [cite:pqac-mvalid]")
        self.assertEqual(q2.get("retrieval_status"), "metadata",
                         "valid sibling metadata must be collected")
        self.assertEqual(self._citation_for_marker(q2, "pqac-mvalid")
                         .get("resolution_reason"), "metadata")

    def test_COLLECT_METADATA_FOREIGN_PROJECT(self):
        """§21.2: metadata whose project identity is FOREIGN to the trusted
        context never counts as metadata retrieval; a valid same-project
        sibling is the positive control."""
        self.case_id = "COLLECT-METADATA-FOREIGN-PROJECT"
        self.expected_pre_fix = "PASS"
        meta_foreign = {
            "evidence_type": "metadata", "project_id": self.proj_b.id,
            "paper_id": self.paper_own.id,
            "source_marker": "pqac-foreign", "citation": "pqac-foreign", "title": "T",
        }
        q1 = self._craft_and_check([meta_foreign], "answer [cite:pqac-foreign]")
        self.assertNotEqual(q1.get("retrieval_status"), "metadata",
                            "foreign metadata must not count as metadata")
        meta_valid = {
            "evidence_type": "metadata", "project_id": self.proj_a.id,
            "paper_id": self.paper_own.id,
            "source_marker": "pqac-mvalid", "citation": "pqac-mvalid", "title": "T",
        }
        q2 = self._craft_and_check([meta_foreign, meta_valid],
                                   "answer [cite:pqac-mvalid]")
        self.assertEqual(q2.get("retrieval_status"), "metadata")
        self.assertEqual(self._citation_for_marker(q2, "pqac-mvalid")
                         .get("resolution_reason"), "metadata")

    def test_CIT_ENVELOPE_VERSION_MISMATCH(self):
        """§20.3: database chunk is ACTIVE but the envelope declares a wrong
        embedding_version → unresolved (envelope_version_mismatch)."""
        self.case_id = "CIT-ENVELOPE-VERSION-MISMATCH"
        self.expected_pre_fix = "PASS"
        q = self._craft_and_check(
            self._fulltext_evidence(self.text_own.paper_id,
                                    self.text_own.id, self.text_own.chunk_index,
                                    "h_own", marker="pqac-own",
                                    embedding_version="wrong-version"),
            "answer [cite:pqac-own]")
        cit = self._citation_for_marker(q, "pqac-own")
        self.assertIs(cit.get("reference_resolved"), False,
                      "envelope version mismatch must fail closed")
        self.assertEqual(cit.get("resolution_reason"), "envelope_version_mismatch")

    def test_CIT_DUPLICATE_MARKER(self):
        """§20.3: resolution identity is the evidence_id — two candidates with
        the SAME marker are both retained (never first-wins); the valid one
        resolves and the invalid one stays unresolved."""
        self.case_id = "CIT-DUPLICATE-MARKER"
        self.expected_pre_fix = "PASS"
        valid = self._fulltext_evidence(self.text_own.paper_id,
                                        self.text_own.id, self.text_own.chunk_index,
                                        "h_own", marker="pqac-dup")[0]
        invalid = self._fulltext_evidence(self.text_own.paper_id,
                                          99999, 99999, "h_own", marker="pqac-dup")[0]
        q = self._craft_and_check([valid, invalid], "answer [cite:pqac-dup]")
        cits = [c for c in q.get("citations", []) if c.get("marker") == "pqac-dup"]
        self.assertEqual(len(cits), 2,
                         "both same-marker candidates must be retained")
        resolved = [c for c in cits if c.get("reference_resolved")]
        unresolved = [c for c in cits if not c.get("reference_resolved")]
        self.assertEqual(len(resolved), 1, "the valid candidate must resolve")
        self.assertEqual(len(unresolved), 1, "the invalid candidate must stay unresolved")
        self.assertEqual(unresolved[0].get("resolution_reason"), "chunk_missing")

    def test_COLLECT_FULLTEXT_MALFORMED(self):
        """§20.4: an evidence_type label alone is not typed evidence — an empty
        fulltext structure must not count toward evidence_count or make
        retrieval_status fulltext."""
        self.case_id = "COLLECT-FULLTEXT-MALFORMED"
        self.expected_pre_fix = "PASS"
        q = self._craft_and_check(
            [{"evidence_type": "fulltext"}], "answer [cite:pqac-own]")
        self.assertEqual(q.get("evidence_count"), 0,
                         "malformed structure must not pollute evidence_count")
        self.assertNotEqual(q.get("retrieval_status"), "fulltext")

    def test_COLLECT_METADATA_VALID(self):
        """§20.4: valid MetadataEvidence is collected as metadata (never
        fulltext) and its citation is not resolved."""
        self.case_id = "COLLECT-METADATA-VALID"
        self.expected_pre_fix = "PASS"
        meta = [{
            "evidence_type": "metadata", "project_id": self.proj_a.id,
            "paper_id": self.paper_own.id,
            "source_marker": "pqac-meta", "citation": "pqac-meta",
            "title": "Own", "summary": "abstract summary",
        }]
        q = self._craft_and_check(meta, "answer [cite:pqac-meta]")
        self.assertGreaterEqual(q.get("evidence_count", 0), 1,
                                "valid metadata evidence must be collected")
        self.assertEqual(q.get("retrieval_status"), "metadata")
        cit = self._citation_for_marker(q, "pqac-meta")
        self.assertIs(cit.get("reference_resolved"), False,
                      "metadata citation must never resolve")
        self.assertEqual(cit.get("resolution_reason"), "metadata")

    def test_CIT_BARE_MARKER_NOT_BOUND(self):
        """§21.4: a marker appearing as BARE text (no [cite:...] token) is NOT
        bound — citation_marker_status stays absent; an explicit-token sibling
        on a DIFFERENT chunk is the positive control."""
        self.case_id = "CIT-BARE-MARKER-NOT-BOUND"
        self.expected_pre_fix = "PASS"
        meta = _active_embedding_meta()
        extra = Text.objects.create(
            paper=self.paper_own, docname="own bare-test chunk", chunk_index=2,
            content="OWN_EXTRA_SENTINEL extra content",
            embedding=_e1024(7.0), embedding_model=meta["embedding_model"],
            embedding_dim=1024, embedding_version=meta["embedding_version"],
            content_hash="h_own_extra", citation_key="pqac-own-extra",
            search_vector="own extra content")
        own_item = self._fulltext_evidence(self.text_own.paper_id,
                                           self.text_own.id, self.text_own.chunk_index,
                                           "h_own", marker="pqac-own")[0]
        valid_item = self._fulltext_evidence(extra.paper_id,
                                             extra.id, extra.chunk_index,
                                             "h_own_extra", marker="pqac-valid")[0]
        q = self._craft_and_check(
            [own_item, valid_item],
            "pqac-own appears as bare text but only pqac-valid is cited [cite:pqac-valid]")
        bare = self._citation_for_marker(q, "pqac-own")
        self.assertEqual(bare.get("citation_marker_status"), "absent",
                         "bare marker must not bind a citation")
        self.assertEqual(q.get("citation_binding_status"), "fully_bound",
                         "only the explicit-token citation binds")
        bound = self._citation_for_marker(q, "pqac-valid")
        self.assertEqual(bound.get("citation_marker_status"), "present",
                         "explicit token must bind")

    def test_CIT_SUBSTRING_NOT_BOUND(self):
        """§21.4: natural-language substring occurrences of a marker must NOT
        bind; the marker is only bound by an explicit [cite:...] token."""
        self.case_id = "CIT-SUBSTRING-NOT-BOUND"
        self.expected_pre_fix = "PASS"
        short_marker = self._fulltext_evidence(self.text_own.paper_id,
                                               self.text_own.id, self.text_own.chunk_index,
                                               "h_own", marker="state")[0]
        q = self._craft_and_check(
            [short_marker],
            "state of the art models rely on selective state space processing")
        cit = self._citation_for_marker(q, "state")
        self.assertEqual(cit.get("citation_marker_status"), "absent",
                         "substring occurrence must not bind")
        self.assertEqual(q.get("citation_binding_status"), "unbound")

    def test_CIT_EXPLICIT_TOKEN_BOUND(self):
        """§21.4: only an explicit [cite:<marker>] token binds — trim and case
        normalization are applied."""
        self.case_id = "CIT-EXPLICIT-TOKEN-BOUND"
        self.expected_pre_fix = "PASS"
        item = self._fulltext_evidence(self.text_own.paper_id,
                                       self.text_own.id, self.text_own.chunk_index,
                                       "h_own", marker="pqac-own")[0]
        q = self._craft_and_check([item], "answer [cite:  PQAC-OWN  ]")
        cit = self._citation_for_marker(q, "pqac-own")
        self.assertEqual(cit.get("citation_marker_status"), "present",
                         "explicit token binds after trim/case normalization")
        self.assertEqual(q.get("citation_binding_status"), "fully_bound")
        self.assertTrue(cit.get("reference_resolved", False))

    def test_COLLECT_LEGACY_DOWNGRADE(self):
        """§20.4: a migration-period fulltext structure (paper_id + marker but
        no envelope binding) is kept for UI/audit as legacy, is NOT counted as
        full-text availability, and its citation is legacy_unresolved."""
        self.case_id = "COLLECT-LEGACY-DOWNGRADE"
        self.expected_pre_fix = "PASS"
        legacy = [{
            "source_marker": "pqac-legacy", "citation": "pqac-legacy",
            "paper_id": self.paper_own.id, "title": "T", "summary": "s",
            "evidence_type": "fulltext",
        }]
        q = self._craft_and_check(legacy, "answer [cite:pqac-legacy]")
        self.assertGreaterEqual(q.get("evidence_count", 0), 1,
                                "legacy structure is kept for UI/audit")
        self.assertNotEqual(q.get("retrieval_status"), "fulltext",
                            "legacy must not count as full-text availability")
        self.assertGreaterEqual(q.get("legacy_unresolved_count", 0), 1)
        cit = self._citation_for_marker(q, "pqac-legacy")
        self.assertEqual(cit.get("resolution_reason"), "legacy_unresolved")


# =====================================================================
# Task 1.6: Metadata bypass (§9, §12.11)
# =====================================================================

class MetadataBypassTest(ScopeFixtureMixin, NetworkGuardTestCaseMixin, TransactionTestCase):
    """§9/§13.6: metadata-only evidence must NOT satisfy factual/compare/report
    fulltext policy (answer_mode abstained/needs_more, evidence_status not
    sufficient fulltext, safety_replaced=True, final != raw unsupported answer,
    metadata actually collected). Action capability cases (list/search/export/
    graph) MUST still produce action_result without being replaced."""

    META_EVIDENCE = [{
        "title": "Own", "summary": "OWN_ABSTRACT selective state spaces.",
        "citation": "pqac-meta", "source_marker": "pqac-meta",
        "paper_id": None, "project_id": None, "chunk_index": None,
        "evidence_type": "metadata",
        "page_start": None, "page_end": None, "section": "",
    }]

    def setUp(self):
        super().setUp()
        self.setUpScopeFixture()
        self.META_EVIDENCE[0]["paper_id"] = self.paper_own.id
        self.META_EVIDENCE[0]["project_id"] = self.proj_a.id

    def _run_react_harness(self, message: str, tool_name: str, tool_result: dict,
                           answer_text: str, tool_arguments: dict | None = None,
                           tool_call_name: str | None = None,
                           multi_tool_calls: list | None = None,
                           no_tool_calls: bool = False) -> tuple[dict, str, list[str]]:
        """Drive the formal harness react path: (optionally one or more tool
        calls), then final answer. `multi_tool_calls` is a list of
        (name, arguments, result) tuples executed in one round."""
        from agent.harness import ProjectAgentHarness
        a_id = self.proj_a.id
        calls: list[str] = []
        call_tool = tool_call_name or tool_name
        if multi_tool_calls is not None:
            round_calls = [{"id": f"c{i}", "name": name,
                            "arguments": json.dumps(arguments or {})}
                           for i, (name, arguments, _result) in enumerate(multi_tool_calls)]
            # results are consumed IN ORDER so the same tool may be called
            # multiple times (e.g. failed search then a successful retry)
            result_queue = [result for _name, _args, result in multi_tool_calls]
            result_index = 0

            async def fake_exec(context, name, args):
                nonlocal result_index
                calls.append(name)
                if result_index < len(result_queue):
                    result = result_queue[result_index]
                    result_index += 1
                    return result
                return {}
        else:
            round_calls = [{"id": "c1", "name": call_tool,
                            "arguments": json.dumps(tool_arguments or {})}]
            results_by_name = {call_tool: tool_result}

            async def fake_exec(context, name, args):
                calls.append(name)
                return results_by_name.get(name, {})

        class FC:
            def __init__(self):
                self.calls = 0

            def complete_with_tools(self, messages, tools, **kwargs):
                self.calls += 1
                if no_tool_calls:
                    return {"content": answer_text, "reasoning_content": "",
                            "tool_calls": []}
                if self.calls == 1:
                    return {"content": "", "reasoning_content": "",
                            "tool_calls": round_calls}
                return {"content": answer_text, "reasoning_content": "",
                        "tool_calls": []}

            def complete(self, *a, **k):
                return {"content": answer_text, "usage": {}}

        with mock.patch("llm.deepseek.DeepSeekClient") as mc:
            mc.return_value = FC()
            captured_raw: list[str] = []

            def _raw_cb(raw_answer: str) -> None:
                captured_raw.append(raw_answer)

            h = ProjectAgentHarness(a_id, use_llm=True, tool_executor=fake_exec,
                                    raw_answer_callback=_raw_cb)
            result = asyncio.run(h.run(message))
        events = result.get("events", [])
        quality = next((e["data"] for e in events if e.get("event") == "quality_check"), {})
        self.assertTrue(quality, "quality_check event must be emitted")
        if captured_raw:
            # Tasks 5.x: the raw model answer is delivered via the explicit
            # eval hook (it is intentionally absent from the sanitized
            # quality_check event) — restore it in-memory for assertions.
            quality["raw_model_answer"] = captured_raw[0]
        return quality, result.get("answer", ""), calls

    def _assert_metadata_policy(self, quality, answer, raw_answer, case_id):
        """§13.6 five-part contract for factual/compare/report metadata cases."""
        # 1. metadata evidence was actually collected (non-vacuous)
        self.assertGreaterEqual(quality.get("evidence_count", 0), 1,
                                "metadata evidence must be collected")
        # 2. answer_mode must not be a normal answered
        self.assertIn(quality.get("answer_mode"),
                      ("abstained", "needs_more_evidence"),
                      "metadata-only must abstain or request more evidence")
        # 3. evidence_status must not be sufficient fulltext
        self.assertNotEqual(quality.get("evidence_status"), "sufficient",
                            "metadata-only must not satisfy the fulltext gate")
        # 4. the user-visible answer must be replaced by the safety gate
        self.assertIs(quality.get("safety_replaced"), True,
                      "safety gate must replace the unsupported answer")
        # 5. final answer != raw unsupported answer
        self.assertNotEqual(answer, raw_answer,
                            "user must not receive the raw unsupported answer")
        self.assertNotIn("selective state space", answer.lower(),
                         "domain-knowledge filler must not reach the user")

    def test_metadata_factual_rejected(self):
        """Factual: metadata-only evidence must not satisfy the fulltext gate."""
        self.case_id = "META-FACTUAL"
        self.expected_pre_fix = "PASS"
        raw_answer = "This paper proposes selective state spaces. [cite:pqac-meta]"
        q, answer, calls = self._run_react_harness(
            "What method does this paper propose?", "query_project_rag",
            {"evidence": list(self.META_EVIDENCE)}, raw_answer,
            tool_arguments={"question": "What method does this paper propose?"})
        self.assertEqual(calls, ["query_project_rag"], "tool must be called")
        self._assert_metadata_policy(q, answer, raw_answer, "META-FACTUAL")
        # auxiliary: Chinese abstention copy (NOT the primary assertion)
        self.assertIn("暂无相关证据", answer,
                      "user-visible abstention should carry the standard copy")

    def test_metadata_compare_rejected(self):
        """Compare: metadata-only compare result must not satisfy the gate.

        Pre-fix the compare result is NOT even collected by _collect_evidence
        (whitelist gap) → red on assertion #1; once typed collection lands
        (Task 3.6) the policy assertions #2-#5 govern."""
        self.case_id = "META-COMPARE"
        self.expected_pre_fix = "PASS"
        raw_answer = "Mamba 用选择性状态空间，Transformer 用注意力。 [cite:pqac-meta]"
        compare_result = {
            "papers": [{
                "paper_id": self.paper_own.id, "title": "Own Included Paper",
                "evidence_source": "metadata_fallback", "chunks": [],
                "evidence": [dict(self.META_EVIDENCE[0])],
            }],
            "evidence_gaps": [{"paper_id": self.paper_own.id, "reason": "no fulltext chunks"}],
            "note": "metadata-only comparison",
        }
        q, answer, calls = self._run_react_harness(
            "对比 Mamba 和 Transformer 的方法", "compare_papers",
            compare_result, raw_answer,
            tool_arguments={"paper_ids": [self.paper_own.id, 999999]})
        self.assertEqual(calls, ["compare_papers"], "tool must be called")
        self._assert_metadata_policy(q, answer, raw_answer, "META-COMPARE")

    def test_metadata_report_rejected(self):
        """Report: metadata-only report evidence must not satisfy the gate."""
        self.case_id = "META-REPORT"
        self.expected_pre_fix = "PASS"
        raw_answer = "## 状态空间模型\n\n该方向基于选择性状态空间。 [cite:pqac-meta]"
        report_result = {
            "section": raw_answer,
            "evidence": list(self.META_EVIDENCE),
        }
        q, answer, calls = self._run_react_harness(
            "写一份关于状态空间模型的报告章节", "draft_report_section",
            report_result, raw_answer,
            tool_arguments={"question": "状态空间模型报告"})
        self.assertEqual(calls, ["draft_report_section"], "tool must be called")
        self._assert_metadata_policy(q, answer, raw_answer, "META-REPORT")

    def _assert_action_result(self, q, answer, calls, tool_name, case_id,
                              expected_calls=None):
        """Action capability positive: metadata/structured artifact may form
        action_result and must NOT be replaced by the safety gate."""
        self.assertEqual(calls, expected_calls or [tool_name],
                         "expected tool trajectory")
        self.assertEqual(q.get("answer_mode"), "action_result",
                         f"{case_id}: action must be action_result")
        self.assertIs(q.get("safety_replaced"), False,
                      f"{case_id}: action output must not be replaced")
        self.assertEqual(answer, q.get("raw_model_answer"),
                         f"{case_id}: action answer must reach the user unchanged")
        self.assertNotEqual(q.get("answer_mode"), "abstained",
                            f"{case_id}: action must not be abstained")

    def test_metadata_action_list_allowed(self):
        self.case_id = "META-ACTION-LIST"
        self.expected_pre_fix = "PASS"
        q, answer, calls = self._run_react_harness(
            "列出项目论文库", "list_project_papers",
            {"papers": [{"title": "Own Included Paper"}], "count": 1},
            "项目库现有 1 篇论文。")
        self._assert_action_result(q, answer, calls, "list_project_papers", "META-ACTION-LIST")

    def test_metadata_action_search_allowed(self):
        self.case_id = "META-ACTION-SEARCH"
        self.expected_pre_fix = "PASS"
        q, answer, calls = self._run_react_harness(
            "搜索相关论文", "search_papers",
            {"papers": [{"title": "Paper A", "year": 2025}], "count": 1},
            "找到 1 篇候选论文。",
            multi_tool_calls=[
                ("search_papers", {"query": "x"},
                 {"papers": [{"title": "Paper A", "year": 2025}], "count": 1}),
                ("add_papers_to_project", {"papers": [{"title": "Paper A", "arxiv_id": "p1"}], "reason": "t"},
                 {"added": [{"title": "Paper A", "created": True}], "count": 1}),
            ])
        self._assert_action_result(q, answer, calls, "search_papers", "META-ACTION-SEARCH",
                                   expected_calls=["search_papers", "add_papers_to_project"])

    def test_metadata_action_export_allowed(self):
        self.case_id = "META-ACTION-EXPORT"
        self.expected_pre_fix = "PASS"
        q, answer, calls = self._run_react_harness(
            "导出 BibTeX", "export_bibtex",
            {"format": "bibtex", "count": 1, "content": "@article{own2024}"},
            "已导出 1 条 BibTeX。")
        self._assert_action_result(q, answer, calls, "export_bibtex", "META-ACTION-EXPORT")

    def test_metadata_action_graph_allowed(self):
        self.case_id = "META-ACTION-GRAPH"
        self.expected_pre_fix = "PASS"
        q, answer, calls = self._run_react_harness(
            "刷新引用关系图谱", "get_project_citation_graph",
            {"graph": {"nodes": [{"id": 1, "title": "Own"}], "edges": []}},
            "图谱已构建，共 1 个节点。")
        self._assert_action_result(q, answer, calls, "get_project_citation_graph",
                                   "META-ACTION-GRAPH")

    # =====================================================================
    # Task 4.x: Capability Evidence Policy (structured contract)
    # =====================================================================

    def _fulltext_env(self, paper_id, chunk_id, chunk_index, content_hash,
                      marker="pqac-own", answer_token="pqac-own"):
        """Fulltext EvidenceEnvelope for a REAL active chunk (shared factory)."""
        active = _active_embedding_meta()
        return {
            "source_marker": marker, "citation": marker,
            "project_id": self.proj_a.id,
            "paper_id": paper_id, "chunk_id": chunk_id, "content_hash": content_hash,
            "evidence_id": make_evidence_id(
                self.proj_a.id, paper_id, chunk_id, content_hash,
                str(active["embedding_version"])),
            "evidence_type": "fulltext",
            "embedding_version": str(active["embedding_version"]),
            "excerpt": "s", "section": "x", "page_start": 1, "page_end": 2,
            "retrieval_sources": ["hybrid_rag"], "retrieval_scores": {"rcs": 8.0},
            "title": "T", "summary": "s", "chunk_index": chunk_index,
        }

    def test_POLICY_FACTUAL_BOUND(self):
        """factual: resolved + answer-bound fulltext → answered, not replaced."""
        self.case_id = "POLICY-FACTUAL-BOUND"
        self.expected_pre_fix = "PASS"
        ev = [self._fulltext_env(self.text_own.paper_id, self.text_own.id,
                                 self.text_own.chunk_index, "h_own")]
        raw = "Mamba 使用选择性状态空间。 [cite:pqac-own]"
        q, answer, calls = self._run_react_harness(
            "What method does this paper propose?", "query_project_rag",
            {"evidence": ev}, raw, tool_arguments={"question": "q"})
        self.assertEqual(q.get("answer_mode"), "answered")
        self.assertIs(q.get("safety_replaced"), False)
        self.assertEqual(answer, raw, "bound factual answer must reach the user")

    def test_POLICY_FACTUAL_UNBOUND(self):
        """factual: retrieved fulltext but NOT bound by the answer → fail closed."""
        self.case_id = "POLICY-FACTUAL-UNBOUND"
        self.expected_pre_fix = "PASS"
        ev = [self._fulltext_env(self.text_own.paper_id, self.text_own.id,
                                 self.text_own.chunk_index, "h_own")]
        raw = "Mamba 使用选择性状态空间，但没有任何引用标记。"
        q, answer, calls = self._run_react_harness(
            "What method does this paper propose?", "query_project_rag",
            {"evidence": ev}, raw, tool_arguments={"question": "q"})
        self.assertEqual(q.get("answer_mode"), "abstained",
                         "retrieved-but-unbound must not satisfy factual")
        self.assertIs(q.get("safety_replaced"), True)
        self.assertNotEqual(answer, raw)
        self.assertEqual(q.get("raw_model_answer"), raw, "raw answer must be kept")

    def test_POLICY_FACTUAL_LEGACY(self):
        """factual: legacy evidence never satisfies the fulltext contract."""
        self.case_id = "POLICY-FACTUAL-LEGACY"
        self.expected_pre_fix = "PASS"
        legacy = [{
            "source_marker": "pqac-legacy", "citation": "pqac-legacy",
            "paper_id": self.paper_own.id, "title": "T", "summary": "s",
            "evidence_type": "fulltext",
        }]
        raw = "Mamba 使用选择性状态空间。 [cite:pqac-legacy]"
        q, answer, calls = self._run_react_harness(
            "What method does this paper propose?", "query_project_rag",
            {"evidence": legacy}, raw, tool_arguments={"question": "q"})
        self.assertEqual(q.get("answer_mode"), "abstained")
        self.assertIs(q.get("safety_replaced"), True)

    def test_POLICY_FACTUAL_UNRESOLVED(self):
        """factual: an unresolved citation (wrong hash) never satisfies."""
        self.case_id = "POLICY-FACTUAL-UNRESOLVED"
        self.expected_pre_fix = "PASS"
        ev = [self._fulltext_env(self.text_own.paper_id, self.text_own.id,
                                 self.text_own.chunk_index, "WRONG_HASH")]
        raw = "Mamba 使用选择性状态空间。 [cite:pqac-own]"
        q, answer, calls = self._run_react_harness(
            "What method does this paper propose?", "query_project_rag",
            {"evidence": ev}, raw, tool_arguments={"question": "q"})
        self.assertEqual(q.get("answer_mode"), "abstained")
        self.assertIs(q.get("safety_replaced"), True)

    def test_POLICY_COMPARE_FULL(self):
        """compare: every compared paper resolved + bound → answered."""
        self.case_id = "POLICY-COMPARE-FULL"
        self.expected_pre_fix = "PASS"
        meta = _active_embedding_meta()
        own2 = Paper.objects.create(
            title="Own Second Paper", abstract="OWN2_ABSTRACT transformers.",
            year=2017, arxiv_id="own-policy-2")
        ProjectPaper.objects.create(project=self.proj_a, paper=own2, status="included")
        own2_chunk = Text.objects.create(
            paper=own2, docname="own2 policy chunk", chunk_index=0,
            content="OWN2_POLICY_SENTINEL transformer self-attention",
            embedding=_e1024(6.0), embedding_model=meta["embedding_model"],
            embedding_dim=1024, embedding_version=meta["embedding_version"],
            content_hash="h_own2_policy", citation_key="pqac-own2-policy",
            search_vector="Own Second Paper transformer")
        own_env = self._fulltext_env(self.text_own.paper_id, self.text_own.id,
                                     self.text_own.chunk_index, "h_own",
                                     marker="pqac-own")
        own2_env = self._fulltext_env(own2.id, own2_chunk.id, own2_chunk.chunk_index,
                                      "h_own2_policy", marker="pqac-own2-policy")
        compare_result = {
            "papers": [
                {"paper_id": self.paper_own.id, "title": "Own Included Paper",
                 "chunks": [own_env], "evidence_source": "fulltext_hybrid_rag"},
                {"paper_id": own2.id, "title": "Own Second Paper",
                 "chunks": [own2_env], "evidence_source": "fulltext_hybrid_rag"},
            ],
            "paper_coverage": 1.0,
            "note": "full coverage",
        }
        raw = "Mamba 用选择性状态空间，Transformer 用注意力。 [cite:pqac-own] [cite:pqac-own2-policy]"
        q, answer, calls = self._run_react_harness(
            "对比 Mamba 和 Transformer 的方法", "compare_papers", compare_result, raw,
            tool_arguments={"paper_ids": [self.paper_own.id, own2.id]})
        self.assertEqual(q.get("answer_mode"), "answered",
                         "both sides resolved+bound must satisfy compare")
        self.assertIs(q.get("safety_replaced"), False)
        self.assertEqual(answer, raw)

    def test_POLICY_COMPARE_ONE_SIDE(self):
        """compare: only one side covered → fail closed and disclose the gap."""
        self.case_id = "POLICY-COMPARE-ONE-SIDE"
        self.expected_pre_fix = "PASS"
        meta = _active_embedding_meta()
        own2 = Paper.objects.create(
            title="Own Second Paper", abstract="OWN2_ABSTRACT transformers.",
            year=2017, arxiv_id="own-policy-3")
        ProjectPaper.objects.create(project=self.proj_a, paper=own2, status="included")
        own_env = self._fulltext_env(self.text_own.paper_id, self.text_own.id,
                                     self.text_own.chunk_index, "h_own",
                                     marker="pqac-own")
        compare_result = {
            "papers": [
                {"paper_id": self.paper_own.id, "title": "Own Included Paper",
                 "chunks": [own_env], "evidence_source": "fulltext_hybrid_rag"},
                {"paper_id": own2.id, "title": "Own Second Paper",
                 "chunks": [], "evidence_source": "metadata_fallback",
                 "evidence": [dict(self.META_EVIDENCE[0])]},
            ],
            "paper_coverage": 0.5,
            "evidence_gaps": [{"paper_id": own2.id, "reason": "no fulltext chunks"}],
            "note": "one side missing",
        }
        raw = "Mamba 用选择性状态空间。 [cite:pqac-own]"
        q, answer, calls = self._run_react_harness(
            "对比 Mamba 和 Transformer 的方法", "compare_papers", compare_result, raw,
            tool_arguments={"paper_ids": [self.paper_own.id, own2.id]})
        self.assertEqual(q.get("answer_mode"), "abstained",
                         "one-sided coverage must not satisfy compare")
        self.assertIs(q.get("safety_replaced"), True)
        self.assertIn("对比证据不足", answer, "the gap must be disclosed")
        self.assertIn(own2.id, q.get("compare_missing_paper_ids", []),
                      "missing side must be reported structurally")

    def test_POLICY_REPORT_BOUND(self):
        """report: resolved + bound → answered."""
        self.case_id = "POLICY-REPORT-BOUND"
        self.expected_pre_fix = "PASS"
        ev = [self._fulltext_env(self.text_own.paper_id, self.text_own.id,
                                 self.text_own.chunk_index, "h_own")]
        raw = "## 状态空间模型\n\n该方向基于选择性状态空间。 [cite:pqac-own]"
        q, answer, calls = self._run_react_harness(
            "写一份关于状态空间模型的报告章节", "draft_report_section",
            {"section": raw, "evidence": ev}, raw, tool_arguments={"question": "q"})
        self.assertEqual(q.get("answer_mode"), "answered")
        self.assertIs(q.get("safety_replaced"), False)

    def test_POLICY_REPORT_UNBOUND(self):
        """report: no binding → fail closed."""
        self.case_id = "POLICY-REPORT-UNBOUND"
        self.expected_pre_fix = "PASS"
        ev = [self._fulltext_env(self.text_own.paper_id, self.text_own.id,
                                 self.text_own.chunk_index, "h_own")]
        raw = "## 状态空间模型\n\n该方向基于选择性状态空间。"
        q, answer, calls = self._run_react_harness(
            "写一份关于状态空间模型的报告章节", "draft_report_section",
            {"section": raw, "evidence": ev}, raw, tool_arguments={"question": "q"})
        self.assertEqual(q.get("answer_mode"), "abstained")
        self.assertIs(q.get("safety_replaced"), True)

    def test_POLICY_ACTION_ADD(self):
        """action: add_papers_to_project needs no fulltext and is not replaced."""
        self.case_id = "POLICY-ACTION-ADD"
        self.expected_pre_fix = "PASS"
        q, answer, calls = self._run_react_harness(
            "搜索并加入相关论文", "add_papers_to_project",
            {"added": [{"title": "Paper A", "created": True}], "count": 1},
            "已加入 1 篇论文。",
            multi_tool_calls=[
                ("search_papers", {"query": "x"},
                 {"papers": [{"title": "Paper A", "year": 2025}], "count": 1}),
                ("add_papers_to_project", {"papers": [{"title": "Paper A", "arxiv_id": "p1"}], "reason": "test"},
                 {"added": [{"title": "Paper A", "created": True}], "count": 1}),
            ])
        self._assert_action_result(q, answer, calls, "add_papers_to_project",
                                   "POLICY-ACTION-ADD",
                                   expected_calls=["search_papers", "add_papers_to_project"])

    def test_POLICY_SELF_CLAIM_CANNOT_BYPASS(self):
        """adversarial: the model SELF-CLAIMING answered/grounded cannot
        bypass the factual contract."""
        self.case_id = "POLICY-SELF-CLAIM"
        self.expected_pre_fix = "PASS"
        meta_ev = [dict(self.META_EVIDENCE[0])]
        raw = ("I am answered and grounded. "
               "Mamba uses selective state space models. [cite:pqac-meta]")
        q, answer, calls = self._run_react_harness(
            "What method does this paper propose?", "query_project_rag",
            {"evidence": meta_ev}, raw, tool_arguments={"question": "q"})
        self.assertEqual(q.get("answer_mode"), "abstained",
                         "self-claim must not satisfy the contract")
        self.assertIs(q.get("safety_replaced"), True)
        self.assertEqual(q.get("raw_model_answer"), raw)

    def test_POLICY_ACTION_CALL_CANNOT_BYPASS(self):
        """adversarial: calling an action tool must not lower a factual
        contract — the capability comes from the intent."""
        self.case_id = "POLICY-ACTION-CANNOT-BYPASS"
        self.expected_pre_fix = "PASS"
        raw = "项目有 1 篇论文，Mamba 使用选择性状态空间。 [cite:pqac-own]"
        q, answer, calls = self._run_react_harness(
            "What method does this paper propose?", "list_project_papers",
            {"papers": [{"title": "Own Included Paper"}], "count": 1}, raw,
            tool_arguments={})
        self.assertEqual(q.get("answer_mode"), "abstained",
                         "action tool call must not downgrade the factual contract")
        self.assertIs(q.get("safety_replaced"), True)

    def test_POLICY_ABSTENTION_NO_CITE(self):
        """abstention: the fail-closed answer carries NO unrelated [cite:]
        tokens; the raw unsupported answer is still preserved."""
        self.case_id = "POLICY-ABSTENTION-NO-CITE"
        self.expected_pre_fix = "PASS"
        meta_ev = [dict(self.META_EVIDENCE[0])]
        raw = "Mamba 使用选择性状态空间。 [cite:pqac-meta]"
        q, answer, calls = self._run_react_harness(
            "What method does this paper propose?", "query_project_rag",
            {"evidence": meta_ev}, raw, tool_arguments={"question": "q"})
        self.assertEqual(q.get("answer_mode"), "abstained")
        self.assertIs(q.get("safety_replaced"), True)
        self.assertNotIn("[cite:", answer,
                         "abstention must not carry unrelated citations")
        self.assertIn("暂无相关证据", answer)
        self.assertEqual(q.get("raw_model_answer"), raw,
                         "raw answer must be preserved for internal evaluation")

    # =====================================================================
    # §24: library reasoning / compare obligation / action error
    # =====================================================================

    def test_POLICY_LIBRARY_REASONING_BOUND(self):
        """§24.1: library reasoning (core/recommendation/why) is FACTUAL —
        resolved+bound fulltext satisfies it."""
        self.case_id = "POLICY-LIBRARY-REASONING-BOUND"
        self.expected_pre_fix = "PASS"
        ev = [self._fulltext_env(self.text_own.paper_id, self.text_own.id,
                                 self.text_own.chunk_index, "h_own")]
        raw = "Own Included Paper 适合作为核心论文。 [cite:pqac-own]"
        q, answer, calls = self._run_react_harness(
            "列出当前项目论文，并说明哪些更适合作为核心论文。",
            "query_project_rag", {"evidence": ev}, raw,
            tool_arguments={"question": "哪些论文适合作为核心"})
        self.assertEqual(q.get("answer_mode"), "answered",
                         "library reasoning with resolved+bound must answer")
        self.assertIs(q.get("safety_replaced"), False)

    def test_POLICY_LIBRARY_REASONING_UNBOUND(self):
        """§24.1: library reasoning WITHOUT resolved+bound fulltext fails closed."""
        self.case_id = "POLICY-LIBRARY-REASONING-UNBOUND"
        self.expected_pre_fix = "PASS"
        meta_ev = [dict(self.META_EVIDENCE[0])]
        raw = "Own Included Paper 适合作为核心论文。 [cite:pqac-meta]"
        q, answer, calls = self._run_react_harness(
            "列出当前项目论文，并说明哪些更适合作为核心论文。",
            "query_project_rag", {"evidence": meta_ev}, raw,
            tool_arguments={"question": "哪些论文适合作为核心"})
        self.assertEqual(q.get("answer_mode"), "abstained",
                         "library reasoning needs resolved fulltext")
        self.assertIs(q.get("safety_replaced"), True)

    def test_POLICY_LIBRARY_INVENTORY_ACTION(self):
        """§24.1: pure inventory stays ACTION — empty list is a legal result."""
        self.case_id = "POLICY-LIBRARY-INVENTORY"
        self.expected_pre_fix = "PASS"
        q, answer, calls = self._run_react_harness(
            "查看当前项目论文库", "list_project_papers",
            {"papers": [], "count": 0}, "当前项目论文库为空。")
        self._assert_action_result(q, answer, calls, "list_project_papers",
                                   "POLICY-LIBRARY-INVENTORY")

    def test_POLICY_COMPARE_EMPTY_RESULT(self):
        """§24.2: compare obligation comes from the CALL arguments — an empty
        tool result must still abstain (obligation cannot shrink to zero)."""
        self.case_id = "POLICY-COMPARE-EMPTY-RESULT"
        self.expected_pre_fix = "PASS"
        compare_result = {"papers": [], "paper_coverage": 0.0, "note": "empty result"}
        raw = "Mamba 用选择性状态空间，Transformer 用注意力。 [cite:pqac-own]"
        q, answer, calls = self._run_react_harness(
            "对比 Mamba 和 Transformer 的方法", "compare_papers", compare_result, raw,
            tool_arguments={"paper_ids": [self.paper_own.id, 999999]})
        self.assertEqual(q.get("answer_mode"), "abstained",
                         "empty result must not satisfy a two-target obligation")
        self.assertIs(q.get("safety_replaced"), True)
        self.assertEqual(set(q.get("compare_missing_paper_ids") or []),
                         {self.paper_own.id, 999999})

    def test_POLICY_COMPARE_OMITTED_TARGET(self):
        """§24.2: when the tool result omits one requested target, the omitted
        side is disclosed and the compare abstains."""
        self.case_id = "POLICY-COMPARE-OMITTED-TARGET"
        self.expected_pre_fix = "PASS"
        meta = _active_embedding_meta()
        own2 = Paper.objects.create(
            title="Own Second Paper", abstract="OWN2_ABSTRACT transformers.",
            year=2017, arxiv_id="own-policy-5")
        ProjectPaper.objects.create(project=self.proj_a, paper=own2, status="included")
        own2_chunk = Text.objects.create(
            paper=own2, docname="own2 omitted chunk", chunk_index=0,
            content="OWN2_OMIT_SENTINEL transformer self-attention",
            embedding=_e1024(6.0), embedding_model=meta["embedding_model"],
            embedding_dim=1024, embedding_version=meta["embedding_version"],
            content_hash="h_own2_omit", citation_key="pqac-own2-omit",
            search_vector="Own Second Paper transformer")
        own3 = Paper.objects.create(
            title="Own Third Paper", abstract="OWN3_ABSTRACT.", year=2016,
            arxiv_id="own-policy-4")
        ProjectPaper.objects.create(project=self.proj_a, paper=own3, status="included")
        own_env = self._fulltext_env(self.text_own.paper_id, self.text_own.id,
                                     self.text_own.chunk_index, "h_own",
                                     marker="pqac-own")
        own2_env = self._fulltext_env(own2.id, own2_chunk.id, own2_chunk.chunk_index,
                                      "h_own2_omit", marker="pqac-own2")
        compare_result = {
            "papers": [
                {"paper_id": self.paper_own.id, "title": "Own Included Paper",
                 "chunks": [own_env], "evidence_source": "fulltext_hybrid_rag"},
                {"paper_id": own2.id, "title": "Own Second Paper",
                 "chunks": [own2_env], "evidence_source": "fulltext_hybrid_rag"},
            ],
            "paper_coverage": 1.0,
            "note": "omitted own3",
        }
        raw = "Mamba 用选择性状态空间，Transformer 用注意力。 [cite:pqac-own] [cite:pqac-own2]"
        q, answer, calls = self._run_react_harness(
            "对比三篇论文的方法", "compare_papers", compare_result, raw,
            tool_arguments={"paper_ids": [self.paper_own.id, own2.id, own3.id]})
        self.assertEqual(q.get("answer_mode"), "abstained",
                         "omitted target must abstain")
        self.assertIn(own3.id, q.get("compare_missing_paper_ids") or [],
                      "the omitted side must be disclosed")
        self.assertIn("对比证据不足", answer)

    def test_POLICY_ACTION_ERROR_CANNOT_CLAIM_SUCCESS(self):
        """§24.3: an action tool ERROR must not be presented as a success —
        the model's success claim is replaced with a deterministic failure
        note, raw answer preserved, failure mode recorded."""
        self.case_id = "POLICY-ACTION-ERROR-CANNOT-CLAIM-SUCCESS"
        self.expected_pre_fix = "PASS"
        raw = "已加入 1 篇论文。"
        q, answer, calls = self._run_react_harness(
            "搜索并加入相关论文", "add_papers_to_project",
            {"error": "add_failed", "message": "integrity error", "count": 0}, raw,
            multi_tool_calls=[
                ("search_papers", {"query": "x"},
                 {"papers": [{"title": "Paper A", "year": 2025}], "count": 1}),
                ("add_papers_to_project", {"papers": [{"title": "Paper A", "arxiv_id": "p1"}], "reason": "test"},
                 {"error": "add_failed", "message": "integrity error", "count": 0}),
            ])
        self.assertEqual(q.get("answer_mode"), "action_failed",
                         "tool error must not be action_result success")
        self.assertTrue(q.get("action_failed"))
        self.assertIn("未能成功完成", answer,
                      "user must receive the deterministic failure note")
        self.assertNotIn("已加入 1 篇论文", answer,
                         "the model's success claim must not reach the user")
        self.assertEqual(q.get("raw_model_answer"), raw)
        mode = q.get("action_failure_mode") or {}
        self.assertEqual(mode.get("tool"), "add_papers_to_project")

    def test_POLICY_ACTION_EMPTY_OK(self):
        """§24.3: a LEGAL EMPTY action result is still a success artifact —
        it must not be treated as a failure."""
        self.case_id = "POLICY-ACTION-EMPTY-OK"
        self.expected_pre_fix = "PASS"
        raw = "没有新的论文需要加入。"
        q, answer, calls = self._run_react_harness(
            "搜索并加入相关论文", "add_papers_to_project",
            {"added": [], "count": 0}, raw,
            multi_tool_calls=[
                ("search_papers", {"query": "x"},
                 {"papers": [], "count": 0}),
                ("add_papers_to_project", {"papers": [], "reason": "test"},
                 {"added": [], "count": 0}),
            ])
        self.assertEqual(q.get("answer_mode"), "action_result",
                         "legal empty action result is a success")
        self.assertIs(q.get("safety_replaced"), False)
        self.assertEqual(answer, raw)

    # =====================================================================
    # §25: schema validation / clarify-blocked / terminal action outcome
    # =====================================================================

    def _assert_validation_failed(self, q, answer, calls, expected_error):
        self.assertEqual(calls, [], "executor must NOT be called on invalid arguments")
        self.assertEqual(q.get("answer_mode"), "abstained",
                         "invalid arguments must fail closed")
        self.assertIs(q.get("safety_replaced"), True)
        tool_errors = q.get("tool_errors") or []
        self.assertTrue(any(e.get("error") == expected_error for e in tool_errors),
                        f"expected validation error {expected_error}")

    def test_POLICY_ARG_COMPARE_WRONG_TYPE(self):
        """§25.1: compare paper_ids with the WRONG TYPE → executor not called,
        stable validation error, fail-closed abstention."""
        self.case_id = "POLICY-ARG-COMPARE-WRONG-TYPE"
        self.expected_pre_fix = "PASS"
        raw = "Mamba 用选择性状态空间，Transformer 用注意力。 [cite:pqac-own]"
        q, answer, calls = self._run_react_harness(
            "对比 Mamba 和 Transformer 的方法", "compare_papers",
            {"papers": [], "paper_coverage": 0.0}, raw,
            tool_arguments={"paper_ids": "not-a-list"})
        self._assert_validation_failed(q, answer, calls, "invalid_arguments")

    def test_POLICY_ARG_MISSING_REQUIRED(self):
        """§25.1: a MISSING required argument → executor not called."""
        self.case_id = "POLICY-ARG-MISSING-REQUIRED"
        self.expected_pre_fix = "PASS"
        raw = "Mamba 使用选择性状态空间。 [cite:pqac-own]"
        q, answer, calls = self._run_react_harness(
            "What method does this paper propose?", "query_project_rag",
            {"evidence": []}, raw, tool_arguments={})
        self._assert_validation_failed(q, answer, calls, "invalid_arguments")

    def test_POLICY_ARG_UNEXPECTED_FIELD(self):
        """§25.1: additionalProperties=false — an undeclared field is rejected."""
        self.case_id = "POLICY-ARG-UNEXPECTED-FIELD"
        self.expected_pre_fix = "PASS"
        raw = "Mamba 使用选择性状态空间。 [cite:pqac-own]"
        q, answer, calls = self._run_react_harness(
            "What method does this paper propose?", "query_project_rag",
            {"evidence": []}, raw,
            tool_arguments={"question": "q", "foo": 1})
        self._assert_validation_failed(q, answer, calls, "invalid_arguments")

    def test_POLICY_ARG_UNKNOWN_TOOL(self):
        """§25.1: an UNKNOWN tool name is rejected without execution."""
        self.case_id = "POLICY-ARG-UNKNOWN-TOOL"
        self.expected_pre_fix = "PASS"
        raw = "Mamba 使用选择性状态空间。 [cite:pqac-own]"
        q, answer, calls = self._run_react_harness(
            "What method does this paper propose?", "nonexistent_tool",
            {}, raw, tool_arguments={"question": "q"})
        self._assert_validation_failed(q, answer, calls, "unknown_tool")

    def test_POLICY_ARG_UNKNOWN_TOOL_NO_ECHO(self):
        """§26.1: the unknown-tool error must NOT echo the model-provided name."""
        self.case_id = "POLICY-ARG-UNKNOWN-TOOL-NO-ECHO"
        self.expected_pre_fix = "PASS"
        forged_name = "zqn9sneaky_tool_93x"
        raw = "Mamba 使用选择性状态空间。 [cite:pqac-own]"
        q, answer, calls = self._run_react_harness(
            "What method does this paper propose?", forged_name,
            {}, raw, tool_arguments={"question": "q"})
        self.assertEqual(calls, [], "executor must NOT be called")
        tool_errors = q.get("tool_errors") or []
        unknown = next((e for e in tool_errors if e.get("error") == "unknown_tool"), None)
        self.assertIsNotNone(unknown, "an unknown_tool error must be recorded")
        self.assertNotIn(forged_name, unknown.get("message", ""),
                         "unknown-tool MESSAGE must not echo the tool name")

    def test_POLICY_ARG_NESTED_ITEM_WRONG_TYPE(self):
        """§26.1: an array ITEM with the wrong type is rejected by the full
        schema (items), not just the top level."""
        self.case_id = "POLICY-ARG-NESTED-ITEM-WRONG-TYPE"
        self.expected_pre_fix = "PASS"
        raw = "Mamba 用选择性状态空间，Transformer 用注意力。 [cite:pqac-own]"
        q, answer, calls = self._run_react_harness(
            "对比 Mamba 和 Transformer 的方法", "compare_papers",
            {"papers": [], "paper_coverage": 0.0}, raw,
            tool_arguments={"paper_ids": [self.paper_own.id, "not-an-int"]})
        self._assert_validation_failed(q, answer, calls, "invalid_arguments")

    def test_POLICY_ARG_BOOL_AS_INTEGER(self):
        """§26.1: bool is not an integer for JSON Schema validation."""
        self.case_id = "POLICY-ARG-BOOL-AS-INTEGER"
        self.expected_pre_fix = "PASS"
        raw = "Mamba 使用选择性状态空间。 [cite:pqac-own]"
        q, answer, calls = self._run_react_harness(
            "What method does this paper propose?", "query_project_rag",
            {"evidence": []}, raw, tool_arguments={"question": "q", "k": True})
        self._assert_validation_failed(q, answer, calls, "invalid_arguments")

    def test_POLICY_ARG_COMPARE_TOO_FEW(self):
        """§26.1: compare minItems=2 — a single target is rejected."""
        self.case_id = "POLICY-ARG-COMPARE-TOO-FEW"
        self.expected_pre_fix = "PASS"
        raw = "Mamba 用选择性状态空间。 [cite:pqac-own]"
        q, answer, calls = self._run_react_harness(
            "对比 Mamba 和 Transformer 的方法", "compare_papers",
            {"papers": [], "paper_coverage": 0.0}, raw,
            tool_arguments={"paper_ids": [self.paper_own.id]})
        self._assert_validation_failed(q, answer, calls, "invalid_arguments")

    def test_POLICY_ARG_COMPARE_TOO_MANY(self):
        """§26.1: compare maxItems=5 — six targets are rejected."""
        self.case_id = "POLICY-ARG-COMPARE-TOO-MANY"
        self.expected_pre_fix = "PASS"
        raw = "Mamba 用选择性状态空间。 [cite:pqac-own]"
        q, answer, calls = self._run_react_harness(
            "对比多篇论文的方法", "compare_papers",
            {"papers": [], "paper_coverage": 0.0}, raw,
            tool_arguments={"paper_ids": [1, 2, 3, 4, 5, 6]})
        self._assert_validation_failed(q, answer, calls, "invalid_arguments")

    def test_POLICY_ARG_ROOT_NOT_OBJECT(self):
        """§26.1: a non-object ROOT argument (JSON array) is a stable
        invalid_arguments error — no crash, no execution, no auth stripping."""
        self.case_id = "POLICY-ARG-ROOT-NOT-OBJECT"
        self.expected_pre_fix = "PASS"
        raw = "Mamba 使用选择性状态空间。 [cite:pqac-own]"
        q, answer, calls = self._run_react_harness(
            "What method does this paper propose?", "query_project_rag",
            {"evidence": []}, raw, tool_arguments=[1])
        self.assertEqual(calls, [], "executor must NOT be called")
        self.assertEqual(q.get("answer_mode"), "abstained")
        tool_errors = q.get("tool_errors") or []
        self.assertTrue(any(e.get("error") == "invalid_arguments" for e in tool_errors))

    def test_POLICY_ARG_OUT_OF_RANGE(self):
        """§26.1: numeric range (k maximum) is enforced."""
        self.case_id = "POLICY-ARG-OUT-OF-RANGE"
        self.expected_pre_fix = "PASS"
        raw = "Mamba 使用选择性状态空间。 [cite:pqac-own]"
        q, answer, calls = self._run_react_harness(
            "What method does this paper propose?", "query_project_rag",
            {"evidence": []}, raw, tool_arguments={"question": "q", "k": 100})
        self._assert_validation_failed(q, answer, calls, "invalid_arguments")

    def test_POLICY_ARG_COMPARE_VALID(self):
        """§25.1 positive: only VALIDATED arguments form the compare obligation —
        with valid two-target args and both sides bound, the compare answers."""
        self.case_id = "POLICY-ARG-COMPARE-VALID"
        self.expected_pre_fix = "PASS"
        meta = _active_embedding_meta()
        own2 = Paper.objects.create(
            title="Own Second Paper", abstract="OWN2_ABSTRACT transformers.",
            year=2017, arxiv_id="own-policy-arg")
        ProjectPaper.objects.create(project=self.proj_a, paper=own2, status="included")
        own2_chunk = Text.objects.create(
            paper=own2, docname="own2 arg chunk", chunk_index=0,
            content="OWN2_ARG_SENTINEL transformer self-attention",
            embedding=_e1024(6.0), embedding_model=meta["embedding_model"],
            embedding_dim=1024, embedding_version=meta["embedding_version"],
            content_hash="h_own2_arg", citation_key="pqac-own2-arg",
            search_vector="Own Second Paper transformer")
        own_env = self._fulltext_env(self.text_own.paper_id, self.text_own.id,
                                     self.text_own.chunk_index, "h_own",
                                     marker="pqac-own")
        own2_env = self._fulltext_env(own2.id, own2_chunk.id, own2_chunk.chunk_index,
                                      "h_own2_arg", marker="pqac-own2")
        compare_result = {
            "papers": [
                {"paper_id": self.paper_own.id, "title": "Own",
                 "chunks": [own_env], "evidence_source": "fulltext_hybrid_rag"},
                {"paper_id": own2.id, "title": "Own2",
                 "chunks": [own2_env], "evidence_source": "fulltext_hybrid_rag"},
            ],
            "paper_coverage": 1.0, "note": "full",
        }
        raw = "Mamba 用选择性状态空间，Transformer 用注意力。 [cite:pqac-own] [cite:pqac-own2]"
        q, answer, calls = self._run_react_harness(
            "对比 Mamba 和 Transformer 的方法", "compare_papers", compare_result, raw,
            tool_arguments={"paper_ids": [self.paper_own.id, own2.id], "question": "方法"})
        self.assertEqual(calls, ["compare_papers"], "executor must be called once")
        self.assertEqual(q.get("answer_mode"), "answered",
                         "validated two-target obligation satisfied")
        self.assertEqual(q.get("compare_missing_paper_ids"), [])

    def test_POLICY_CLARIFY_EMPTY(self):
        """§25.2: an EMPTY message is clarified — no tools, no action success."""
        self.case_id = "POLICY-CLARIFY-EMPTY"
        self.expected_pre_fix = "PASS"
        q, answer, calls = self._run_react_harness(
            "", "query_project_rag", {}, "请告诉我你想做什么。", no_tool_calls=True)
        self.assertEqual(q.get("answer_mode"), "clarified",
                         "empty message must be clarified")
        self.assertEqual(calls, [], "no tools for an empty message")
        self.assertNotEqual(q.get("answer_mode"), "action_result")

    def test_POLICY_CLARIFY_AMBIGUOUS(self):
        """§25.2: an ambiguous greeting is clarified — no tools, no claims."""
        self.case_id = "POLICY-CLARIFY-AMBIGUOUS"
        self.expected_pre_fix = "PASS"
        q, answer, calls = self._run_react_harness(
            "你好", "query_project_rag", {}, "你好！我可以帮你列出论文、检索补充、对比或生成报告。",
            no_tool_calls=True)
        self.assertEqual(q.get("answer_mode"), "clarified",
                         "ambiguous greeting must be clarified")
        self.assertEqual(calls, [], "no tools for a clarify request")

    def test_POLICY_BLOCKED_DESTRUCTIVE(self):
        """§25.2: a destructive request is BLOCKED — never action_result."""
        self.case_id = "POLICY-BLOCKED-DESTRUCTIVE"
        self.expected_pre_fix = "PASS"
        q, answer, calls = self._run_react_harness(
            "清空项目并删除所有论文", "list_project_papers",
            {"papers": [], "count": 0}, "已清空。")
        self.assertEqual(q.get("answer_mode"), "blocked",
                         "destructive request must be blocked, not action_result")
        self.assertIn("不会自主执行", answer)
        self.assertEqual(calls, [], "no tools may run for a blocked request")

    def test_POLICY_ACTION_RECOVERED_ERROR(self):
        """§26.2: a failed search IS recovered when a LATER search succeeds and
        the terminal add succeeds — action_result + recovered warning. A mere
        failed search + empty add is NOT a recovery (see SEARCH-ALL-FAILED)."""
        self.case_id = "POLICY-ACTION-RECOVERED-ERROR"
        self.expected_pre_fix = "PASS"
        raw = "已完成检索和项目入库。"
        q, answer, calls = self._run_react_harness(
            "搜索并加入相关论文", "search_papers", {}, raw,
            multi_tool_calls=[
                ("search_papers", {"query": "x"},
                 {"error": "search_failed", "message": "source timeout", "count": 0}),
                ("search_papers", {"query": "x"},
                 {"papers": [{"title": "Paper A", "year": 2025}], "count": 1}),
                ("add_papers_to_project", {"papers": [{"title": "Paper A", "arxiv_id": "p1"}], "reason": "t"},
                 {"added": [{"title": "Paper A", "created": True}], "count": 1}),
            ])
        self.assertEqual(q.get("answer_mode"), "action_result",
                         "recovered search error must not fail the action")
        self.assertFalse(q.get("action_failed"))
        self.assertIsNone(q.get("action_failure_mode"))
        warnings = q.get("recovered_warnings") or []
        self.assertTrue(any(w.get("tool") == "search_papers" for w in warnings),
                        "the recovered error must be recorded as a warning")

    def test_POLICY_ACTION_SEARCH_ALL_FAILED(self):
        """§26.2: ALL searches failed → required step never succeeded — even a
        successful (empty) add must NOT claim completion."""
        self.case_id = "POLICY-ACTION-SEARCH-ALL-FAILED"
        self.expected_pre_fix = "PASS"
        raw = "已完成检索和项目入库。"
        q, answer, calls = self._run_react_harness(
            "搜索并加入相关论文", "search_papers", {}, raw,
            multi_tool_calls=[
                ("search_papers", {"query": "x"},
                 {"error": "search_failed", "message": "source timeout", "count": 0}),
                ("add_papers_to_project", {"papers": [], "reason": "t"},
                 {"added": [], "count": 0}),
            ])
        self.assertEqual(q.get("answer_mode"), "action_failed",
                         "all-failed search must not claim completion")
        self.assertTrue(q.get("action_failed"))
        mode = q.get("action_failure_mode") or {}
        self.assertEqual(mode.get("mode"), "required_step_failed")
        self.assertEqual(mode.get("tool"), "search_papers")
        self.assertIn("未能成功完成", answer)
        self.assertEqual(q.get("raw_model_answer"), raw)

    def test_POLICY_ACTION_SEARCH_EMPTY_OK(self):
        """§26.2: a successful EMPTY search plus a legal empty add is a
        legitimate no-op — action_result."""
        self.case_id = "POLICY-ACTION-SEARCH-EMPTY-OK"
        self.expected_pre_fix = "PASS"
        raw = "没有找到新的论文需要加入。"
        q, answer, calls = self._run_react_harness(
            "搜索并加入相关论文", "search_papers", {}, raw,
            multi_tool_calls=[
                ("search_papers", {"query": "x"},
                 {"papers": [], "count": 0}),
                ("add_papers_to_project", {"papers": [], "reason": "t"},
                 {"added": [], "count": 0}),
            ])
        self.assertEqual(q.get("answer_mode"), "action_result",
                         "empty search + empty add is a legal no-op")
        self.assertEqual(answer, raw)

    def test_POLICY_ACTION_TERMINAL_RECOVERED(self):
        """§26.2: the TERMINAL add tool failing once then succeeding is
        recovered — action_result with the early terminal error as a warning."""
        self.case_id = "POLICY-ACTION-TERMINAL-RECOVERED"
        self.expected_pre_fix = "PASS"
        raw = "已加入 1 篇论文。"
        q, answer, calls = self._run_react_harness(
            "搜索并加入相关论文", "search_papers", {}, raw,
            multi_tool_calls=[
                ("search_papers", {"query": "x"},
                 {"papers": [{"title": "Paper A", "year": 2025}], "count": 1}),
                ("add_papers_to_project", {"papers": [{"title": "Paper A", "arxiv_id": "p1"}], "reason": "t"},
                 {"error": "add_failed", "message": "transient", "count": 0}),
                ("add_papers_to_project", {"papers": [{"title": "Paper A", "arxiv_id": "p1"}], "reason": "t"},
                 {"added": [{"title": "Paper A", "created": True}], "count": 1}),
            ])
        self.assertEqual(q.get("answer_mode"), "action_result",
                         "terminal failure recovered by later success must pass")
        self.assertFalse(q.get("action_failed"))
        warnings = q.get("recovered_warnings") or []
        self.assertTrue(any(w.get("tool") == "add_papers_to_project" for w in warnings),
                        "the recovered terminal error must be a warning")

    def test_POLICY_ACTION_TERMINAL_LAST_FAILED(self):
        """§26.2: the LAST terminal outcome is an error → action_failed even
        though an earlier add attempt succeeded."""
        self.case_id = "POLICY-ACTION-TERMINAL-LAST-FAILED"
        self.expected_pre_fix = "PASS"
        raw = "已加入 1 篇论文。"
        q, answer, calls = self._run_react_harness(
            "搜索并加入相关论文", "search_papers", {}, raw,
            multi_tool_calls=[
                ("search_papers", {"query": "x"},
                 {"papers": [{"title": "Paper A", "year": 2025}], "count": 1}),
                ("add_papers_to_project", {"papers": [{"title": "Paper A", "arxiv_id": "p1"}], "reason": "t"},
                 {"added": [{"title": "Paper A", "created": True}], "count": 1}),
                ("add_papers_to_project", {"papers": [{"title": "Paper A", "arxiv_id": "p1"}], "reason": "t"},
                 {"error": "add_failed", "message": "integrity", "count": 0}),
            ])
        self.assertEqual(q.get("answer_mode"), "action_failed",
                         "the last terminal failure must fail the action")
        self.assertTrue(q.get("action_failed"))
        mode = q.get("action_failure_mode") or {}
        self.assertEqual(mode.get("mode"), "terminal_failure")
        self.assertEqual(mode.get("tool"), "add_papers_to_project")

    def test_POLICY_ACTION_REQUIRED_STEP_FAILED(self):
        """§25.3: the terminal add step FAILED → action_failed, even though
        search succeeded."""
        self.case_id = "POLICY-ACTION-REQUIRED-STEP-FAILED"
        self.expected_pre_fix = "PASS"
        raw = "已加入 1 篇论文。"
        q, answer, calls = self._run_react_harness(
            "搜索并加入相关论文", "search_papers", {}, raw,
            multi_tool_calls=[
                ("search_papers", {"query": "x"},
                 {"papers": [{"title": "Paper A", "year": 2025}], "count": 1}),
                ("add_papers_to_project", {"papers": [{"title": "Paper A", "arxiv_id": "p1"}], "reason": "t"},
                 {"error": "add_failed", "message": "integrity error", "count": 0}),
            ])
        self.assertEqual(q.get("answer_mode"), "action_failed",
                         "terminal add failure must fail the action")
        self.assertTrue(q.get("action_failed"))
        self.assertIn("未能成功完成", answer)
        mode = q.get("action_failure_mode") or {}
        self.assertEqual(mode.get("tool"), "add_papers_to_project")
        self.assertEqual(q.get("raw_model_answer"), raw)

    def test_POLICY_SEARCH_ADD_TERMINAL_ADD(self):
        """§25.3: search-add COMPLETES on the add terminal outcome — search
        success + add success (even empty) is action_result."""
        self.case_id = "POLICY-SEARCH-ADD-TERMINAL-ADD"
        self.expected_pre_fix = "PASS"
        raw = "没有新的论文需要加入。"
        q, answer, calls = self._run_react_harness(
            "搜索并加入相关论文", "search_papers", {}, raw,
            multi_tool_calls=[
                ("search_papers", {"query": "x"},
                 {"papers": [{"title": "Paper A", "year": 2025}], "count": 1}),
                ("add_papers_to_project", {"papers": [{"title": "Paper A", "arxiv_id": "p1"}], "reason": "t"},
                 {"added": [], "count": 0}),
            ])
        self.assertEqual(q.get("answer_mode"), "action_result",
                         "add terminal success completes search-add")
        self.assertEqual(answer, raw)


# =====================================================================
# Tasks 5.x (§28): event/log/SSE/API payload separation — allowlist
# sanitizer, raw-answer gating, correlation IDs, compat, verified deprecation
#
# Red-suite expectations:
# - EVENT-LEAK-REACT / EVENT-RAW-NOT-PERSISTED / EVENT-ALLOWLIST-SCHEMA /
#   EVENT-CORRELATION-IDS are FAIL before the fix (raw answer and full
#   arguments/query/papers payloads currently reach ProjectRunEvent, SSE
#   stream events and API response events).
# - EVENT-LEAK-LOGS / EVENT-COMPAT-FIELDS / EVENT-VERIFIED-DEPRECATED are
#   PASS baselines that must stay green (regression lock).
# =====================================================================

class EventObservabilityTest(NetworkGuardTestCaseMixin, ScopeFixtureMixin, TransactionTestCase):
    """Tasks 5.x observability contract (directive §28)."""

    def setUp(self):
        super().setUp()
        self.setUpScopeFixture()

    # ---------------------------------------------------------------
    # Stream helper: full harness.stream() with scripted FC rounds.
    # Returns (events, run_id, log_text, user_answer, raw_captured).
    # tool_rounds: list of (name, arguments, result) executed in round 1.
    # ---------------------------------------------------------------
    def _run_react_stream(self, message, tool_rounds, answer_text,
                          raw_callback=None, tool_round_id="c1"):
        import logging

        from agent.harness import ProjectAgentHarness

        round_calls = [{"id": f"c{i}", "name": name,
                        "arguments": json.dumps(args or {})}
                       for i, (name, args, _r) in enumerate(tool_rounds)]
        result_queue = [r for _n, _a, r in tool_rounds]
        result_index = [0]
        calls = []

        async def fake_exec(context, name, args):
            calls.append(name)
            if result_index[0] < len(result_queue):
                r = result_queue[result_index[0]]
                result_index[0] += 1
                return r
            return {}

        class FC:
            def __init__(self):
                self.n = 0

            def complete_with_tools(self, messages, tools, **kwargs):
                self.n += 1
                if self.n == 1 and tool_rounds:
                    return {"content": "", "reasoning_content": "",
                            "tool_calls": round_calls}
                return {"content": answer_text, "reasoning_content": "",
                        "tool_calls": []}

            def complete(self, *a, **k):
                return {"content": answer_text, "usage": {}}

        class _CaptureHandler(logging.Handler):
            def __init__(self):
                super().__init__()
                self.records = []

            def emit(self, record):
                self.records.append(record)

        handler = _CaptureHandler()
        root = logging.getLogger()
        root.addHandler(handler)
        try:
            with mock.patch("llm.deepseek.DeepSeekClient") as mc:
                mc.return_value = FC()
                try:
                    h = ProjectAgentHarness(
                        self.proj_a.id, use_llm=True, tool_executor=fake_exec,
                        raw_answer_callback=raw_callback)
                except TypeError:
                    # pre-fix harness has no raw_answer_callback hook
                    h = ProjectAgentHarness(
                        self.proj_a.id, use_llm=True, tool_executor=fake_exec)
                events = []

                async def drive():
                    async for ev in h.stream(message):
                        events.append(ev)
                    return h

                hh = asyncio.run(drive())
                run_id = events[0]["data"].get("run_id") if events else None
        finally:
            root.removeHandler(handler)
        log_lines = [json.dumps(r.__dict__, ensure_ascii=False, default=str)
                     for r in handler.records]
        answer = "".join(e["data"].get("text", "")
                         for e in events if e["event"] == "token")
        return events, run_id, "\n".join(log_lines), answer

    def _run_react_deterministic(self, message):
        """Deterministic plan path (no LLM) for allowlist/correlation checks."""
        from agent.harness import ProjectAgentHarness

        async def fake_exec(context, name, args):
            if name == "search_papers":
                return {"papers": [{"title": "Paper A", "year": 2025}], "count": 1}
            if name == "add_papers_to_project":
                return {"added": [{"title": "Paper A", "created": True}], "count": 1}
            if name == "query_project_rag":
                return {"evidence": [], "fallback": ""}
            return {"status": "ok"}

        h = ProjectAgentHarness(self.proj_a.id, use_llm=False,
                                tool_executor=fake_exec)
        events = []

        async def drive():
            async for ev in h.stream(message):
                events.append(ev)
            return h

        hh = asyncio.run(drive())
        run_id = events[0]["data"].get("run_id") if events else None
        return events, run_id

    def _own_fulltext_envelope(self, chunk_id=None, content_hash=None):
        meta = _active_embedding_meta()
        return {
            "evidence_id": make_evidence_id(
                self.proj_a.id, self.paper_own.id,
                chunk_id or self.text_own.id,
                content_hash or "h_own",
                str(meta["embedding_version"])),
            "project_id": self.proj_a.id,
            "paper_id": self.paper_own.id,
            "chunk_id": chunk_id or self.text_own.id,
            "content_hash": content_hash or "h_own",
            "excerpt": "OWN_SENTINEL selective state space model SSM",
            "page_start": None, "page_end": None, "section": "3.2",
            "retrieval_sources": ["hybrid"], "retrieval_scores": [1.0],
            "embedding_version": str(meta["embedding_version"]),
            "evidence_type": "fulltext",
            "citation": "pqac-own",
        }

    # ---------------------------------------------------------------
    # §28.4: end-to-end sentinel scan over SSE events, ProjectRunEvent
    # JSON, API-response events and logs.
    # ---------------------------------------------------------------
    def test_EVENT_LEAK_REACT(self):
        """§28.1/28.4: a full ReAct case with a forged tool name, secret query/
        question/papers payload, fulltext excerpt and sk- secret in the raw
        answer must NOT leak into stream/SSE events, ProjectRunEvent payloads,
        API-response events or logs; the raw answer reaches only the explicit
        eval hook after the safety gate."""
        self.case_id = "EVENT-LEAK-REACT"
        self.expected_pre_fix = "PASS"  # red→green in Tasks 5.x fix round (red baseline in run-tasks5-red)
        forged_name = "zqn9sneaky_tool_93x"
        secret_raw = "结论：sk-live-A1B2C3D4E5F6。 [cite:pqac-own]"
        sentinels = [
            "SECRET_QUERY_MARKER", "SECRET_QUESTION_MARKER",
            "SECRET_PAPER_BODY_MARKER", "sk-live-A1B2C3D4E5F6", forged_name,
        ]
        captured_raw = []
        events, run_id, logs, answer = self._run_react_stream(
            "查询论文相关内容",
            [
                (forged_name, {"query": "SECRET_QUERY_MARKER",
                               "papers": [{"body": "SECRET_PAPER_BODY_MARKER"}]}, {}),
                ("query_project_rag", {"question": "SECRET_QUESTION_MARKER"},
                 {"evidence": [self._own_fulltext_envelope(
                     chunk_id=999999, content_hash="h_bad")], "fallback": ""}),
            ],
            secret_raw,
            raw_callback=captured_raw.append,
        )
        quality = next((e["data"] for e in events
                        if e["event"] == "quality_check"), {})
        self.assertTrue(quality, "quality_check must be emitted (non-vacuous)")
        self.assertEqual(quality.get("answer_mode"), "abstained",
                         "unresolved evidence must fail closed so the raw "
                         "answer is replaced before it reaches the user")

        # 1. the explicit eval hook receives the raw model answer
        self.assertEqual(captured_raw, [secret_raw],
                         "raw answer must reach the explicit eval hook")
        # 2. no final_answer_raw event is ever streamed
        self.assertNotIn("final_answer_raw", [e["event"] for e in events])
        # 3. stream/SSE + API-response events carry no sentinel
        event_blob = json.dumps(
            [e for e in events if e["event"] != "token"],
            ensure_ascii=False, default=str)
        for s in sentinels:
            self.assertNotIn(s, event_blob, f"LEAK in stream events: {s}")
        # 4. ProjectRunEvent JSON carries no sentinel
        from api.models import ProjectRunEvent
        rows = list(ProjectRunEvent.objects.filter(run_id=run_id)
                    .values_list("event_type", "payload"))
        self.assertTrue(rows, "ProjectRunEvent rows must exist (non-vacuous)")
        db_blob = json.dumps(rows, ensure_ascii=False, default=str)
        for s in sentinels:
            self.assertNotIn(s, db_blob, f"LEAK in ProjectRunEvent: {s}")
        # 5. the user-visible answer is the post-gate answer, never the raw
        self.assertNotIn("sk-live-A1B2C3D4E5F6", answer)
        # 6. logs carry no body/secret/forged-name
        for s in ("SECRET_PAPER_BODY_MARKER", "sk-live-A1B2C3D4E5F6", forged_name):
            self.assertNotIn(s, logs, f"LEAK in logs: {s}")

    def test_EVENT_LEAK_LOGS(self):
        """§28.2: logs share the safe summary contract — no body excerpt,
        secret or forged tool name in any log record (PASS baseline)."""
        self.case_id = "EVENT-LEAK-LOGS"
        self.expected_pre_fix = "PASS"
        forged_name = "zqn9sneaky_tool_93x"
        events, run_id, logs, answer = self._run_react_stream(
            "查询论文相关内容",
            [
                (forged_name, {"query": "SECRET_QUERY_MARKER"}, {}),
                ("query_project_rag", {"question": "SECRET_QUESTION_MARKER"},
                 {"evidence": [self._own_fulltext_envelope()], "fallback": ""}),
            ],
            "结论。 [cite:pqac-own]",
        )
        for s in ("SECRET_PAPER_BODY_MARKER", "sk-live-", forged_name,
                  "SECRET_QUERY_MARKER", "SECRET_QUESTION_MARKER"):
            self.assertNotIn(s, logs, f"LEAK in logs: {s}")

    # ---------------------------------------------------------------
    # §28.1: raw model answer is memory-only (safety gate + eval hook).
    # ---------------------------------------------------------------
    def test_EVENT_RAW_NOT_PERSISTED(self):
        """§28.1/28.4: an unsupported raw model answer is NEVER streamed or
        persisted before/after the safety replacement — the user only ever
        sees the replaced answer; the eval hook still receives the raw text."""
        self.case_id = "EVENT-RAW-NOT-PERSISTED"
        self.expected_pre_fix = "PASS"  # red→green in Tasks 5.x fix round (red baseline in run-tasks5-red)
        secret_raw = "Mamba 使用选择性状态空间 SECRET_DOMAIN_KNOWLEDGE_MARKER 提升效率。"
        captured_raw = []
        events, run_id, logs, answer = self._run_react_stream(
            "Mamba 用什么方法？", [], secret_raw,
            raw_callback=captured_raw.append,
        )
        quality = next((e["data"] for e in events
                        if e["event"] == "quality_check"), {})
        self.assertTrue(quality, "quality_check must be emitted (non-vacuous)")

        self.assertEqual(captured_raw, [secret_raw],
                         "raw answer must reach the explicit eval hook")
        self.assertNotIn("final_answer_raw", [e["event"] for e in events])
        self.assertNotIn(
            "SECRET_DOMAIN_KNOWLEDGE_MARKER",
            json.dumps(events, ensure_ascii=False, default=str),
            "raw answer must never appear in stream/SSE/API events")
        from api.models import ProjectRunEvent
        rows = list(ProjectRunEvent.objects.filter(run_id=run_id)
                    .values_list("payload", flat=True))
        self.assertTrue(rows, "ProjectRunEvent rows must exist (non-vacuous)")
        self.assertNotIn(
            "SECRET_DOMAIN_KNOWLEDGE_MARKER",
            json.dumps(rows, ensure_ascii=False, default=str),
            "raw answer must never be persisted")
        self.assertNotIn("SECRET_DOMAIN_KNOWLEDGE_MARKER", answer,
                         "user sees only the replaced answer")
        self.assertIn("暂无相关证据", answer,
                      "fail-closed abstention replaces the unsupported raw")

    # ---------------------------------------------------------------
    # §28.1: per-event-type allowlist schema.
    # ---------------------------------------------------------------
    def test_EVENT_ALLOWLIST_SCHEMA(self):
        """§28.1: every persisted event type is limited to its allowlist —
        tool_call never carries arguments/question/query/papers, quality_check
        never carries raw_model_answer, evidence events never carry excerpts."""
        self.case_id = "EVENT-ALLOWLIST-SCHEMA"
        self.expected_pre_fix = "PASS"  # red→green in Tasks 5.x fix round (red baseline in run-tasks5-red)
        events, run_id = self._run_react_deterministic("搜索并加入相关论文")
        from api.models import ProjectRunEvent
        rows = list(ProjectRunEvent.objects.filter(run_id=run_id))
        types = [r.event_type for r in rows]
        self.assertIn("tool_call", types, "non-vacuous: tool_call must exist")
        self.assertIn("quality_check", types, "non-vacuous: quality_check must exist")

        allowed = {
            "harness_started": {"session_id", "run_id", "project_id", "request_id"},
            "intent_detected": {"intent", "rationale", "blocked", "planned_tools",
                                "project_id", "run_id", "session_id", "request_id"},
            "agent_mode": {"mode", "max_iterations",
                           "project_id", "run_id", "session_id", "request_id"},
            "tool_call": {"name", "tool_call_id", "iteration", "status",
                          "ui_label", "summary", "arguments",
                          "model_supplied_project_id",
                          "project_id", "run_id", "session_id", "request_id"},
            "tool_result": {"name", "tool", "status", "count", "nodes", "edges", "length",
                            "error", "error_message", "retryable", "fallback",
                            "project_id", "run_id", "session_id", "request_id"},
            "evidence": {"evidence_count", "evidence", "fallback",
                         "project_id", "run_id", "session_id", "request_id"},
            "search_results": {"count", "papers",
                               "project_id", "run_id", "session_id", "request_id"},
            "paper_added": {"count", "added_titles", "added",
                            "project_id", "run_id", "session_id", "request_id"},
            "graph": {"nodes", "edges", "node_count", "edge_count",
                      "project_id", "run_id", "session_id", "request_id"},
            "llm_call": {"phase", "iteration",
                         "project_id", "run_id", "session_id", "request_id"},
            "llm_result": {"phase", "iteration", "usage", "status",
                           "answer_chars", "duration_ms",
                           "project_id", "run_id", "session_id", "request_id"},
            "tool_scope_violation": {"project_id", "run_id", "session_id",
                                     "request_id", "tool", "rejected_fields",
                                     "attempted_project_id"},
            "quality_check": {
                "verdict", "evidence_count", "source_marker_count",
                "resolved_citation_count", "citations", "verified_count",
                "unverified_count", "tool_errors", "answer_mode",
                "evidence_status", "citation_presence", "retrieval_status",
                "reference_resolution_status", "citation_binding_status",
                "claim_support_status", "legacy_unresolved_count",
                "compare_missing_paper_ids", "action_failure_mode",
                "recovered_warnings", "raw_model_answer_chars", "model_cited",
                "postprocessed_added_markers", "safety_replaced", "action_failed",
                "project_id", "run_id", "session_id", "request_id"},
            "done": {"session_id", "run_id", "project_id", "request_id"},
            "error": {"message", "project_id", "run_id", "session_id", "request_id"},
            "token": {"text", "project_id", "run_id", "session_id", "request_id"},
        }
        for row in rows:
            keys = set(row.payload.keys())
            extra = keys - allowed.get(row.event_type, set())
            self.assertFalse(extra,
                             f"{row.event_type} has non-allowlisted keys: {sorted(extra)}")
        # tool_call must never carry query/question/papers or the raw argument
        # payload — `arguments` is the controlled numeric-only view model and
        # `summary` is the pre-defined tool label (§30.4).
        tc = next(r.payload for r in rows if r.event_type == "tool_call")
        for forbidden in ("query", "question", "papers"):
            self.assertNotIn(forbidden, tc, f"tool_call must not carry '{forbidden}'")
        self.assertNotIn("SECRET", json.dumps(tc, default=str))
        args = tc.get("arguments") or {}
        self.assertIsInstance(args, dict)
        self.assertTrue(
            all(isinstance(v, (int, float, bool)) for v in args.values()),
            "tool_call.arguments must be the numeric-only view model")
        self.assertFalse(
            any(isinstance(v, str) for v in args.values()),
            "tool_call.arguments must not carry string/free-text values")
        self.assertIsInstance(tc.get("summary"), str)
        self.assertTrue(tc["summary"])
        # quality_check must not carry the raw model answer
        qc = next(r.payload for r in rows if r.event_type == "quality_check")
        self.assertNotIn("raw_model_answer", qc,
                         "quality_check must not persist the raw answer")
        # evidence rows carry the count and the safe location-only view model
        for row in rows:
            if row.event_type == "evidence":
                self.assertNotIn("excerpt", json.dumps(row.payload, default=str))
                self.assertNotIn("content", json.dumps(row.payload, default=str))
                self.assertNotIn("summary", json.dumps(row.payload, default=str))

    # ---------------------------------------------------------------
    # §28.2: correlation IDs on every structured event.
    # ---------------------------------------------------------------
    def test_EVENT_CORRELATION_IDS(self):
        """§28.2: request/project/run/session ids are present on every
        persisted event (null when unknown, never fabricated); tool_call
        carries its tool_call_id."""
        self.case_id = "EVENT-CORRELATION-IDS"
        self.expected_pre_fix = "PASS"  # red→green in Tasks 5.x fix round (red baseline in run-tasks5-red)
        events, run_id, logs, answer = self._run_react_stream(
            "查询论文相关内容",
            [("query_project_rag", {"question": "q"},
              {"evidence": [self._own_fulltext_envelope()], "fallback": ""})],
            "结论。 [cite:pqac-own]",
        )
        from api.models import ProjectRunEvent
        rows = list(ProjectRunEvent.objects.filter(run_id=run_id))
        self.assertTrue(rows, "events must exist (non-vacuous)")
        for row in rows:
            p = row.payload
            self.assertEqual(p.get("project_id"), self.proj_a.id,
                             f"{row.event_type}: project_id missing")
            self.assertEqual(p.get("run_id"), run_id,
                             f"{row.event_type}: run_id mismatch")
            self.assertGreater(p.get("session_id") or 0, 0,
                               f"{row.event_type}: session_id missing")
            self.assertTrue(p.get("request_id"),
                            f"{row.event_type}: request_id missing")
        tc = next(r.payload for r in rows if r.event_type == "tool_call")
        self.assertEqual(tc.get("tool_call_id"), "c0",
                         "tool_call must carry its tool_call_id")

    # ---------------------------------------------------------------
    # §28.3: API/SSE compatibility + deprecated verified fields.
    # ---------------------------------------------------------------
    def test_EVENT_COMPAT_FIELDS(self):
        """§28.3: the required SSE event names and the frontend/quality
        compatibility fields (including deprecated verified/verified_count)
        still exist after sanitization (PASS baseline)."""
        self.case_id = "EVENT-COMPAT-FIELDS"
        self.expected_pre_fix = "PASS"
        events, run_id, logs, answer = self._run_react_stream(
            "Mamba 用什么方法？",
            [("query_project_rag", {"question": "Mamba 特点"},
              {"evidence": [self._own_fulltext_envelope()], "fallback": ""})],
            "Mamba 使用选择性状态空间。 [cite:pqac-own]",
        )
        types = [e["event"] for e in events]
        for required in ("harness_started", "intent_detected", "agent_mode",
                         "tool_call", "tool_result", "quality_check", "done"):
            self.assertIn(required, types, f"required SSE event {required} missing")
        quality = next(e["data"] for e in events
                       if e["event"] == "quality_check")
        for field in ("answer_mode", "evidence_status", "citation_presence",
                      "verified_count", "reference_resolution_status",
                      "citation_binding_status", "resolved_citation_count"):
            self.assertIn(field, quality, f"compat field {field} missing")
        self.assertTrue(quality["citations"])
        self.assertTrue(all("verified" in c and "reference_resolved" in c
                            for c in quality["citations"]),
                        "per-citation verified (deprecated) + reference fields kept")

    def test_EVENT_VERIFIED_DEPRECATED(self):
        """§28.3: deprecated `verified` reflects marker occurrence only and is
        NEVER read by any gate — a marker-present but DB-unresolved citation
        must still abstain (PASS baseline, locks the gate migration)."""
        self.case_id = "EVENT-VERIFIED-DEPRECATED"
        self.expected_pre_fix = "PASS"
        from agent.harness import ProjectAgentHarness
        from agent.intent import classify_project_intent

        context = {"query_project_rag": {"evidence": [{
            "evidence_id": make_evidence_id(
                self.proj_a.id, self.paper_own.id, 999999, "h_bad", "v_bad"),
            "project_id": self.proj_a.id,
            "paper_id": self.paper_own.id,
            "chunk_id": 999999,
            "content_hash": "h_bad",
            "excerpt": "x",
            "page_start": None, "page_end": None, "section": "1",
            "retrieval_sources": ["hybrid"], "retrieval_scores": [1.0],
            "embedding_version": "v_bad",
            "evidence_type": "fulltext",
            "citation": "pqac-own",
        }]}}
        intent = classify_project_intent("Mamba 用什么方法？", self.proj_a.id)
        h = ProjectAgentHarness(self.proj_a.id)
        q = asyncio.run(h._quality_check(
            "Mamba 使用选择性状态空间。 [cite:pqac-own]", intent, context))
        self.assertTrue(q["citations"], "citation must exist (non-vacuous)")
        cited = next(c for c in q["citations"] if c["marker"] == "pqac-own")
        self.assertIs(cited["verified"], True,
                      "deprecated verified = marker occurrence")
        self.assertIs(cited["reference_resolved"], False,
                      "DB-unresolved citation must not resolve")
        self.assertEqual(q["resolved_citation_count"], 0)
        self.assertEqual(q["answer_mode"], "abstained",
                         "verified must never satisfy the factual gate")
        self.assertEqual(q["reference_resolution_status"], "unresolved")


# =====================================================================
# §31.1/§31.2: opaque-sentinel exception tests + real LLM metrics
#
# The opaque sentinel deliberately contains NO secret/key/token/password
# keyword so regex-based redaction could never catch it — only stable error
# codes + fixed user copy satisfy §31.1.
# =====================================================================

class Tasks5OpaqueErrorTest(NetworkGuardTestCaseMixin, ScopeFixtureMixin, TransactionTestCase):
    """§31.1: every production error surface rejects opaque exception bodies.
    §31.2: llm_result metrics are real measurements or explicit null."""

    OPAQUE = "Qz7xT9mK2pL4vR8wHn5c"

    def setUp(self):
        super().setUp()
        self.setUpScopeFixture()

    def _capture_logs(self):
        class _CaptureHandler(logging.Handler):
            def __init__(self):
                super().__init__()
                self.records = []

            def emit(self, record):
                self.records.append(record)

        handler = _CaptureHandler()
        logging.getLogger().addHandler(handler)
        return handler

    def _log_blob(self, handler):
        return "\n".join(
            json.dumps(r.__dict__, ensure_ascii=False, default=str)
            for r in handler.records)

    def test_AUDIT_OPAQUE_ERROR_HARNESS(self):
        """Harness: an exception whose body contains the opaque sentinel must
        not reach SSE error events, run.error_message or logs."""
        self.case_id = "AUDIT-OPAQUE-ERROR-HARNESS"
        self.expected_pre_fix = "PASS"
        from agent.harness import ProjectAgentHarness

        async def broken_execute(context, name, args):
            raise RuntimeError("boom %s" % self.OPAQUE)

        handler = self._capture_logs()
        try:
            h = ProjectAgentHarness(self.proj_a.id, tool_executor=broken_execute)
            events = []

            async def drive():
                async for ev in h.stream("Mamba 用什么方法？"):
                    events.append(ev)
                return h

            hh = asyncio.run(drive())
        finally:
            logging.getLogger().removeHandler(handler)
        event_blob = json.dumps(events, ensure_ascii=False, default=str)
        log_blob = self._log_blob(handler)
        from api.models import ProjectRun
        run = ProjectRun.objects.filter(project_id=self.proj_a.id).order_by("-id").first()
        error_message = run.error_message or ""
        self.assertNotIn(self.OPAQUE, event_blob,
                         "opaque exception body leaked into SSE error event")
        self.assertNotIn(self.OPAQUE, error_message,
                         "opaque exception body leaked into run.error_message")
        self.assertNotIn(self.OPAQUE, log_blob,
                         "opaque exception body leaked into logs")
        error_ev = next((e["data"] for e in events if e["event"] == "error"), {})
        self.assertEqual(error_ev.get("error"), "RuntimeError",
                         "stable error code must be present")
        self.assertTrue(error_ev.get("error_hash"),
                        "error hash must be present")
        self.assertEqual(error_ev.get("message"),
                         "服务暂时不可用，请稍后重试。",
                         "error message must be the fixed user copy")
        self.assertIn("RuntimeError", log_blob,
                      "exception type must survive in logs (no wipe-out)")
        self.assertIn("project_agent_run_failed", log_blob,
                      "event code must survive in logs")

    def test_AUDIT_OPAQUE_ERROR_CELERY(self):
        """Celery ingestion + workflow: opaque exception bodies must not reach
        failed events, run.error_message or logs."""
        self.case_id = "AUDIT-OPAQUE-ERROR-CELERY"
        self.expected_pre_fix = "PASS"
        from api.models import PaperIngestionJob, ProjectRun, ProjectRunEvent
        from api.tasks import ingest_paper_pdf_task, run_research_expand_workflow_task

        handler = self._capture_logs()
        try:
            job = PaperIngestionJob.objects.create(
                project=self.proj_a, paper=self.paper_own,
                status="pending", source_url="https://example.invalid/pdf.pdf")
            with mock.patch("api.tasks._load_pdf_bytes",
                            side_effect=RuntimeError("download %s" % self.OPAQUE)):
                with self.assertRaises(RuntimeError):
                    ingest_paper_pdf_task.run(job.id)
            ingest_run = ProjectRun.objects.filter(
                project_id=self.proj_a.id, kind="ingestion").order_by("-id").first()
            self.assertTrue(ingest_run)
            failed = ProjectRunEvent.objects.filter(
                event_type="ingestion_failed").order_by("-id").first()
            self.assertTrue(failed)

            wf_run = ProjectRun.objects.create(
                project=self.proj_a, kind="workflow", status="pending",
                question="扩展检索 Mamba")
            with mock.patch("agent.project_workflow.run_project_research_expand",
                            side_effect=RuntimeError("wf %s" % self.OPAQUE)):
                with self.assertRaises(RuntimeError):
                    run_research_expand_workflow_task.run(wf_run.id)
            wf_run.refresh_from_db()
            wf_failed = ProjectRunEvent.objects.filter(
                run_id=wf_run.id, event_type="workflow_failed").order_by("-id").first()
            self.assertTrue(wf_failed)
        finally:
            logging.getLogger().removeHandler(handler)
        log_blob = self._log_blob(handler)
        surfaces = [
            ("ingestion run.error_message", ingest_run.error_message or ""),
            ("ingestion_failed event", json.dumps(failed.payload, default=str)),
            ("workflow run.error_message", wf_run.error_message or ""),
            ("workflow_failed event", json.dumps(wf_failed.payload, default=str)),
            ("logs", log_blob),
        ]
        for surface, blob in surfaces:
            self.assertNotIn(self.OPAQUE, blob,
                             "opaque exception body leaked on %s" % surface)
        self.assertIn("RuntimeError",
                      json.dumps(failed.payload, default=str),
                      "stable error code must be present on ingestion_failed")
        self.assertIn("error_hash",
                      json.dumps(failed.payload, default=str),
                      "error hash must be present on ingestion_failed")
        self.assertIn("ingestion_failed", log_blob,
                      "event code must survive in logs")

    def test_AUDIT_OPAQUE_ERROR_MCP(self):
        """MCP tools/call: an opaque exception body must not reach the
        CallToolResult payload or logs — only the stable code + fixed copy."""
        self.case_id = "AUDIT-OPAQUE-ERROR-MCP"
        self.expected_pre_fix = "PASS"
        from mcp_server import server as mcp_server

        handler = self._capture_logs()
        try:
            # avoid a second django.setup() (it would re-apply logging config
            # and drop our capture handler)
            mcp_server._django_ready = True
            with mock.patch("mcp_server.server.execute_project_tool",
                            side_effect=RuntimeError("mcp %s" % self.OPAQUE)):
                params = mcp_server.types.CallToolRequestParams(
                    name="query_project_rag",
                    arguments={"project_id": self.proj_a.id},
                )
                result = asyncio.run(mcp_server._handle_call_tool(None, params))
        finally:
            logging.getLogger().removeHandler(handler)
        log_blob = self._log_blob(handler)
        payload = "".join(
            (c.text if hasattr(c, "text") else str(c))
            for c in result.content)
        self.assertTrue(result.is_error)
        self.assertNotIn(self.OPAQUE, payload,
                         "opaque exception body leaked into CallToolResult")
        self.assertNotIn(self.OPAQUE, log_blob,
                         "opaque exception body leaked into MCP logs")
        self.assertIn("工具调用失败", payload,
                      "fixed user copy must be present")
        self.assertIn("RuntimeError", payload,
                      "stable error code must be present")
        self.assertIn("mcp_call_tool_failed", log_blob,
                      "event code must survive in logs")

    def test_AUDIT_LLM_METRIC_REAL(self):
        """§31.2: llm_result.status/answer_chars/duration_ms are REAL
        measurements from the call site (duration_ms > 0 for a slow mock)."""
        self.case_id = "AUDIT-LLM-METRIC-REAL"
        self.expected_pre_fix = "PASS"
        from agent.harness import ProjectAgentHarness

        class SlowClient:
            def __init__(self):
                self.n = 0

            def complete_with_tools(self, messages, tools, **kwargs):
                self.n += 1
                if self.n == 1:
                    return {"content": "", "tool_calls": [
                        {"id": "c1", "name": "query_project_rag",
                         "arguments": '{"question": "q"}'}]}
                return {"content": "最终答案内容。", "tool_calls": []}

            def complete(self, *a, **k):
                return {"content": "最终答案内容。", "usage": {}}

        import time as _time

        async def fake_exec(context, name, args):
            return {"evidence": [], "fallback": ""}

        with mock.patch("llm.deepseek.DeepSeekClient") as mc:
            client = SlowClient()
            _orig_call = SlowClient.complete_with_tools

            def _slow_call(self2, messages, tools, **kwargs):
                _time.sleep(0.05)
                return _orig_call(self2, messages, tools, **kwargs)

            mc.return_value = client
            with mock.patch.object(SlowClient, "complete_with_tools", _slow_call):
                h = ProjectAgentHarness(self.proj_a.id, use_llm=True,
                                        tool_executor=fake_exec)
                events = []

                async def drive():
                    async for ev in h.stream("Mamba 用什么方法？"):
                        events.append(ev)

                asyncio.run(drive())
        results = [e["data"] for e in events if e["event"] == "llm_result"]
        self.assertTrue(len(results) >= 1, "llm_result events must exist")
        for data in results:
            self.assertEqual(data.get("status"), "ok")
            self.assertIsInstance(data.get("answer_chars"), int)
            self.assertGreaterEqual(data.get("answer_chars"), 0)
            self.assertIsInstance(data.get("duration_ms"), (int, float))
            self.assertGreaterEqual(data.get("duration_ms"), 0)
        # the measured call must have taken real time (>0 ms)
        self.assertTrue(any(data.get("duration_ms", 0) > 0 for data in results),
                        "duration_ms must reflect real measured time")

    def test_AUDIT_LLM_METRIC_UNAVAILABLE(self):
        """§31.2: when the producer does not provide metrics, the serializer
        must surface null (unavailable) — never a fabricated constant 0."""
        self.case_id = "AUDIT-LLM-METRIC-UNAVAILABLE"
        self.expected_pre_fix = "PASS"
        from agent.event_publisher import EventPublisher
        from api.models import ProjectRun

        run = ProjectRun.objects.create(
            project=self.proj_a, kind="chat", status="running",
            question="q")
        publisher = EventPublisher(run=run)
        out = publisher.publish("llm_result", {
            "phase": "react", "iteration": 1, "usage": {},
        })
        data = out["data"]
        self.assertIsNone(data.get("status"),
                          "missing status must be null (unavailable)")
        self.assertIsNone(data.get("answer_chars"),
                          "missing answer_chars must be null (unavailable)")
        self.assertIsNone(data.get("duration_ms"),
                          "missing duration_ms must be null (unavailable)")
        self.assertIn("status", data,
                      "the field must exist (null), not be dropped")
        self.assertIn("answer_chars", data)
        self.assertIn("duration_ms", data)
        # usage keeps only known numeric fields
        self.assertIn("usage", data)
        self.assertEqual(data["usage"], {})

    def test_AUDIT_OPAQUE_ERROR_EVAL_ARTIFACT(self):
        """§31.1: eval-only artifacts never carry the raw exception text."""
        self.case_id = "AUDIT-OPAQUE-ERROR-EVAL-ARTIFACT"
        self.expected_pre_fix = "PASS"
        from eval.run_eval import eval_one

        item = type("Item", (), {"id": 1, "type": "factual",
                                 "question": "q", "gold_titles": [], "gold_topics": []})()

        async def broken_paperlens(question):
            raise RuntimeError("eval %s" % self.OPAQUE)

        handler = self._capture_logs()
        try:
            with mock.patch("llm.deepseek.DeepSeekClient") as _mc, \
                 mock.patch("eval.variants.paperlens_research",
                            side_effect=broken_paperlens), \
                 mock.patch("eval.variants.baseline_research",
                            return_value={"retrieved_titles": [], "report": "",
                                          "sources": []}), \
                 mock.patch("eval.metrics.faithfulness", return_value=1.0):
                result = asyncio.run(eval_one(item, run_baseline=True))
        finally:
            logging.getLogger().removeHandler(handler)
        blob = json.dumps(result, ensure_ascii=False, default=str)
        log_blob = self._log_blob(handler)
        self.assertNotIn(self.OPAQUE, blob,
                         "opaque exception body leaked into eval artifact")
        self.assertNotIn(self.OPAQUE, log_blob,
                         "opaque exception body leaked into eval logs")
        self.assertEqual(result["paperlens"]["error"], "RuntimeError",
                         "eval artifact must carry the stable error code")
        self.assertIn("error_hash", result["paperlens"],
                      "eval artifact must carry the error hash")

    # ------------------------------------------------------------------
    # §32 minimal regressions: legacy SSE DB, tool exception model context,
    # MCP unknown name, eval artifact opaque exception.
    # ------------------------------------------------------------------
    def test_AUDIT_LEGACY_SSE_ERROR_DB(self):
        """§32.1: legacy research SSE failure must not write the opaque
        exception body into ResearchTask.error_message or the SSE frames."""
        self.case_id = "AUDIT-LEGACY-SSE-ERROR-DB"
        self.expected_pre_fix = "PASS"
        from types import SimpleNamespace

        from api.models import ResearchTask
        from realtime.views import research_stream

        task = ResearchTask.objects.create(
            project=self.proj_a, question="q", status="pending")
        request = SimpleNamespace(paperlens_request_id="req-legacy-1")
        with mock.patch("agent.graph.build_graph",
                        side_effect=RuntimeError("legacy %s" % self.OPAQUE)):
            resp = asyncio.run(research_stream(request, task.id))
            frames = []

            async def _drain():
                async for chunk in resp.streaming_content:
                    frames.append(chunk.decode("utf-8", errors="replace"))

            asyncio.run(_drain())
        blob = "\n".join(frames)
        task.refresh_from_db()
        self.assertNotIn(self.OPAQUE, task.error_message or "",
                         "opaque exception body leaked into ResearchTask.error_message")
        self.assertNotIn(self.OPAQUE, blob,
                         "opaque exception body leaked into research SSE frames")
        self.assertIn("RuntimeError", task.error_message or "",
                      "stable error code must be persisted")
        self.assertIn("服务暂时不可用", blob,
                      "fixed user copy must reach the SSE error frame")

    def test_AUDIT_TOOL_EXCEPTION_MODEL_CONTEXT(self):
        """§32.2: a tool exception puts ONLY the fixed failure copy + type +
        hash into the LLM context — the opaque exception body must never be
        fed back to the model."""
        self.case_id = "AUDIT-TOOL-EXCEPTION-MODEL-CONTEXT"
        self.expected_pre_fix = "PASS"
        from agent.chat_loop import ChatAgentLoop

        captured_messages: list[list[dict]] = []

        class FC:
            def __init__(self):
                self.n = 0

            def complete_with_tools(self, messages, tools, **kwargs):
                self.n += 1
                captured_messages.append([dict(m) for m in messages])
                if self.n == 1:
                    return {"content": "", "reasoning_content": "",
                            "tool_calls": [{"id": "c1", "name": "query_project_rag",
                                            "arguments": '{"question": "q"}'}]}
                return {"content": "没有证据。", "reasoning_content": "",
                        "tool_calls": []}

            def complete(self, *a, **k):
                return {"content": "没有证据。", "usage": {}}

        async def raising_exec(context, name, args):
            raise RuntimeError("tool boom %s" % self.OPAQUE)

        with mock.patch("llm.deepseek.DeepSeekClient") as mc:
            mc.return_value = FC()
            loop = ChatAgentLoop(self.proj_a.id, tool_executor=raising_exec)

            async def drive():
                events = []
                async for ev in loop.run("Mamba 用什么方法？"):
                    events.append(ev)
                return events

            events = asyncio.run(drive())
        tool_results = [e for e in events if e["event"] == "tool_result"]
        self.assertTrue(tool_results, "tool_result must exist")
        self.assertTrue(len(captured_messages) >= 2,
                        "the model must get a second round after the tool error")
        second_round = captured_messages[1]
        tool_msgs = [m for m in second_round if m.get("role") == "tool"]
        self.assertTrue(tool_msgs, "the tool message must be in the context")
        blob = json.dumps(tool_msgs, ensure_ascii=False, default=str)
        self.assertNotIn(self.OPAQUE, blob,
                         "opaque exception body leaked into the model context")
        self.assertIn("工具执行失败", blob,
                      "fixed failure copy must be in the model context")
        self.assertIn("RuntimeError", blob,
                      "exception type must be in the model context")
        self.assertIn("error_hash", blob,
                      "error hash must be in the model context")

    def test_AUDIT_MCP_UNKNOWN_NAME(self):
        """§32.3: an unknown MCP tool name returns stable unknown_tool + hash
        — the original name never reaches the CallToolResult or logs."""
        self.case_id = "AUDIT-MCP-UNKNOWN-NAME"
        self.expected_pre_fix = "PASS"
        from mcp_server.testing import call_tool_via_client

        forged = "zqn9sneaky_tool_93x"
        result = call_tool_via_client(forged, {})
        payload = json.loads(result.content[0].text)
        self.assertTrue(result.is_error)
        self.assertEqual(payload["error"], "unknown_tool",
                         "stable code must be returned")
        self.assertIn("tool_hash", payload,
                      "digest must be present")
        self.assertNotIn(forged, result.content[0].text,
                         "forged name leaked into CallToolResult")
        from agent.events import sanitize_tool_name
        code, digest = sanitize_tool_name(forged)
        self.assertEqual(payload["tool_hash"], digest,
                         "digest must match the canonical sanitize_tool_name")

    def test_AUDIT_EVAL_SAFE_ERROR_HELPER(self):
        """§32.4: the shared safe eval error helper never exposes the opaque
        exception body in artifacts; eval artifact paths use it."""
        self.case_id = "AUDIT-EVAL-SAFE-ERROR-HELPER"
        self.expected_pre_fix = "PASS"
        from eval.safe_error import exception_message, exception_record

        exc = RuntimeError("helper %s" % self.OPAQUE)
        record = exception_record(exc)
        self.assertEqual(record["error"], "RuntimeError")
        self.assertTrue(record["error_hash"])
        self.assertNotIn(self.OPAQUE, json.dumps(record, default=str),
                         "helper record leaked the exception body")
        message = exception_message(exc)
        self.assertNotIn(self.OPAQUE, message)
        self.assertIn("RuntimeError", message)
        # the eval artifact path (run_eval) uses the helper
        from eval.run_eval import eval_one

        item = type("Item", (), {"id": 1, "type": "factual",
                                 "question": "q", "gold_titles": [],
                                 "gold_topics": []})()

        async def broken_paperlens(question):
            raise RuntimeError("helper %s" % self.OPAQUE)

        with mock.patch("llm.deepseek.DeepSeekClient") as _mc, \
             mock.patch("eval.variants.paperlens_research",
                        side_effect=broken_paperlens), \
             mock.patch("eval.variants.baseline_research",
                        return_value={"retrieved_titles": [], "report": "",
                                      "sources": []}), \
             mock.patch("eval.metrics.faithfulness", return_value=1.0):
            result = asyncio.run(eval_one(item, run_baseline=False))
        blob = json.dumps(result, ensure_ascii=False, default=str)
        self.assertNotIn(self.OPAQUE, blob,
                         "opaque exception body leaked into eval artifact")
        self.assertEqual(result["paperlens"]["error"], "RuntimeError")
        self.assertIn("error_hash", result["paperlens"])
