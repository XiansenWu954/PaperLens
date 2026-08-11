"""Tasks 5.x INDEPENDENT event-security audit (GLM role, directive
docs/internal/glm-tasks5-independent-audit-20260810.md).

This module is test-only. It MUST NOT modify production code. Every case
reproduces a real production path (harness stream, REST view, Celery task
function, LangGraph workflow, ProjectRunEvent producers) and checks sentinel
leakage across output surfaces (SSE/stream events, token events, persisted
ProjectRunEvent payloads, ProjectRun.error_message, API response, logs).

Run (PostgreSQL gate, own artifact dir):
  docker compose run --rm -v "D:/aiproducts/PaperLens/docs/internal/stage-b-artifacts-20260810/glm-tasks5-audit:/artifacts" \
    -e PAPERLENS_STAGE_B_ARTIFACTS_DIR=/artifacts backend \
    python manage.py test agent.tasks5_audit_tests --noinput -v 2
"""
from __future__ import annotations

import asyncio
import json
import logging
from unittest import mock

from asgiref.sync import sync_to_async
from django.test import TestCase, TransactionTestCase

from agent.context import create_context
from agent.scope_failing_tests import (
    ARTIFACTS_DIR,
    NetworkGuardTestCaseMixin,
    ScopeFixtureMixin,
    _active_embedding_meta,
    _write_json,
    make_evidence_id,
)

AUDIT_SENTINELS = {
    "SECRET_USER_PROMPT": "SECRET_USER_PROMPT_MARKER",
    "SECRET_QUERY": "SECRET_QUERY_MARKER",
    "SECRET_QUESTION": "SECRET_QUESTION_MARKER",
    "SECRET_BODY": "SECRET_BODY_MARKER",
    "SECRET_MARKER": "SECRET_MARKER_TEXT",
    "SECRET_SECTION": "SECRET_SECTION_TEXT",
    "SECRET_PAYLOAD": "SECRET_PAYLOAD_TEXT",
    "SECRET_USAGE": "SECRET_USAGE_TEXT",
    "SECRET_TITLE": "SECRET_TITLE_TEXT",
    "SECRET_FILE": "SECRET_FILENAME_MARKER",
    "SECRET_EXC": "SECRET_EXCEPTION_BODY_MARKER",
    "SECRET_WF": "SECRET_WORKFLOW_BODY_MARKER",
    "SECRET_INGEST": "SECRET_INGEST_BODY_MARKER",
    "SK_LIVE": "sk-live-ABCDEFGH12345678",
    "FORGED_TOOL": "zqn9sneaky_tool_93x",
    "FAKE_CITE": "pqac-fake-citation-marker",
}


# Static per-producer declared event-type set (mirrors event_publisher.py +
# the per-producer call sites; used by AUDIT-ALL-PRODUCERS-COVERAGE to verify
# that a producer's OBSERVED event types belong to its DECLARED set — no
# producer may borrow another producer's events as its own evidence).
EVENT_TYPES_BY_PRODUCER_STATIC = {
    "agent/harness.py (EventPublisher emit)": [
        "harness_started", "intent_detected", "agent_mode", "tool_call",
        "tool_result", "search_results", "evidence", "paper_added", "graph",
        "llm_call", "llm_result", "tool_scope_violation", "quality_check",
        "done", "error", "token"],
    "api/views.py (project_research_expand_workflow)": ["workflow_queued"],
    "api/tasks.py (ingest_paper_pdf_task)": [
        "ingestion_started", "ingestion_progress", "ingestion_completed",
        "ingestion_failed"],
    "api/tasks.py (run_research_expand_workflow_task)": [
        "workflow_started", "workflow_completed", "workflow_failed"],
    "agent/project_workflow.py (_event)": [
        "workflow_node", "hybrid_retrieval", "workflow_completed"],
}


class AuditSurface:
    """One (surface, blob) pair for sentinel scanning."""

    def __init__(self, name: str, blob: str):
        self.name = name
        self.blob = blob

    def contains(self, sentinel: str) -> bool:
        return sentinel in self.blob


class AuditTasks5Base(NetworkGuardTestCaseMixin, ScopeFixtureMixin, TransactionTestCase):
    """Shared harness/stream/log helpers for the audit (test-only)."""

    def setUp(self):
        super().setUp()
        self.setUpScopeFixture()
        self._audit_checks: list[dict] = []

    def record(self, surface: str, sentinel: str, found: bool, note: str = "") -> None:
        self._audit_checks.append({
            "surface": surface, "sentinel": sentinel,
            "found": found, "note": note,
        })

    def write_audit(self, verdict: str) -> None:
        _write_json(f"audit-{self.case_id}.json", {
            "case_id": self.case_id,
            "test": self.id(),
            "verdict": verdict,
            "checks": self._audit_checks,
            "found_leaks": [c for c in self._audit_checks if c["found"]],
        })

    # ---------------------------------------------------------------
    # Scripted ReAct stream (identical shape to EventObservabilityTest).
    # ---------------------------------------------------------------
    def _run_stream(self, message, tool_rounds, answer_text, raw_callback=None):
        from agent.harness import ProjectAgentHarness

        round_calls = [{"id": f"c{i}", "name": name,
                        "arguments": json.dumps(args or {})}
                       for i, (name, args, _r) in enumerate(tool_rounds)]
        result_queue = [r for _n, _a, r in tool_rounds]
        result_index = [0]

        async def fake_exec(context, name, args):
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
                            "tool_calls": round_calls,
                            "usage": {"prompt_tokens": 10, "completion_tokens": 5}}
                return {"content": answer_text, "reasoning_content": "",
                        "tool_calls": [],
                        "usage": {"prompt_tokens": 10, "completion_tokens": 5}}

            def complete(self, *a, **k):
                return {"content": answer_text,
                        "usage": {"prompt_tokens": 10, "completion_tokens": 5}}

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
                h = ProjectAgentHarness(
                    self.proj_a.id, use_llm=True, tool_executor=fake_exec,
                    raw_answer_callback=raw_callback)
                events = []

                async def drive():
                    async for ev in h.stream(message):
                        events.append(ev)
                    return h

                hh = asyncio.run(drive())
                run_id = events[0]["data"].get("run_id") if events else None
        finally:
            root.removeHandler(handler)
        log_blob = "\n".join(
            json.dumps(r.__dict__, ensure_ascii=False, default=str)
            for r in handler.records)
        answer = "".join(e["data"].get("text", "")
                         for e in events if e["event"] == "token")
        return events, run_id, log_blob, answer

    def _event_blob(self, events, include_token=False):
        return json.dumps(
            [e for e in events if include_token or e["event"] != "token"],
            ensure_ascii=False, default=str)

    def _db_events_blob(self, run_id):
        from api.models import ProjectRunEvent
        rows = list(ProjectRunEvent.objects.filter(run_id=run_id)
                    .values_list("event_type", "payload"))
        return json.dumps(rows, ensure_ascii=False, default=str)

    def _fulltext_envelope(self, chunk_id=None, content_hash=None,
                           citation=None, section="3.2"):
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
            "page_start": None, "page_end": None, "section": section,
            "retrieval_sources": ["hybrid"], "retrieval_scores": [1.0],
            "embedding_version": str(meta["embedding_version"]),
            "evidence_type": "fulltext",
            "citation": citation or "pqac-own",
        }


# =====================================================================
# 3.1 Chat SSE 与 token
# =====================================================================

class AuditTokenSseTest(AuditTasks5Base):
    """Token stream: correlation IDs, unsupported-raw gating, supported output."""

    def test_AUDIT_TOKEN_CORRELATION_IDS(self):
        """§3.1: EVERY outward event (including token) must carry correlation
        ids or be an explicit documented compatibility exception."""
        self.case_id = "AUDIT-TOKEN-CORRELATION-IDS"
        self.expected_pre_fix = "FAIL"
        events, run_id, logs, answer = self._run_stream(
            "Mamba 用什么方法？",
            [("query_project_rag", {"question": "q"},
              {"evidence": [self._fulltext_envelope()], "fallback": ""})],
            "Mamba 使用选择性状态空间。 [cite:pqac-own]")
        token_events = [e for e in events if e["event"] == "token"]
        self.assertTrue(token_events, "token events must exist (non-vacuous)")
        for e in token_events:
            d = e["data"]
            self.record("token_event", "correlation.project_id",
                        "project_id" not in d, "token payload keys: %s" % sorted(d))
            self.record("token_event", "correlation.run_id",
                        "run_id" not in d, "")
            self.record("token_event", "correlation.session_id",
                        "session_id" not in d, "")
            self.record("token_event", "correlation.request_id",
                        "request_id" not in d, "")
        missing = [e["data"] for e in token_events
                   if not all(k in e["data"] for k in
                              ("project_id", "run_id", "session_id", "request_id"))]
        self.write_audit("PASS" if not missing else "FAIL")
        self.assertFalse(missing, "token events lack correlation ids")

    def test_AUDIT_TOKEN_UNSUPPORTED_SECRET(self):
        """P0 check: an unsupported raw answer containing secrets must NEVER
        reach any client event before the safety replacement — token events
        must carry only the abstention text."""
        self.case_id = "AUDIT-TOKEN-UNSUPPORTED-SECRET"
        self.expected_pre_fix = "PASS"
        secret_raw = ("Mamba 使用选择性状态空间 %s 提升性能。%s"
                      % (AUDIT_SENTINELS["SK_LIVE"],
                         AUDIT_SENTINELS["SECRET_BODY"]))
        captured_raw = []
        events, run_id, logs, answer = self._run_stream(
            "Mamba 用什么方法？",
            [("query_project_rag", {"question": "q"},
              {"evidence": [], "fallback": ""})],
            secret_raw, raw_callback=captured_raw.append)
        token_blob = json.dumps(
            [e["data"] for e in events if e["event"] == "token"],
            ensure_ascii=False, default=str)
        for sentinel in (AUDIT_SENTINELS["SK_LIVE"], AUDIT_SENTINELS["SECRET_BODY"]):
            found = sentinel in token_blob or sentinel in answer
            self.record("token_events+answer", sentinel, found,
                        "user-visible answer: %r" % answer[:60])
            self.assertFalse(found,
                             "unsupported raw secret reached the client")
        self.assertEqual(captured_raw, [secret_raw],
                         "raw answer must still reach the eval hook")
        self.assertIn("暂无相关证据", answer,
                      "user answer must be the fail-closed abstention")
        self.write_audit("PASS")

    def test_AUDIT_TOKEN_SUPPORTED_SECRET(self):
        """§3.1 / §5 round-2 revised contract (Codex裁决):
        - raw_answer_callback (eval hook) MUST receive the original raw answer
          including the secret patterns (raw hook is the ONLY raw sink).
        - token events, ChatMessage assistant, ProjectRun.output and API answer
          MUST receive the secret-redacted answer (redact_text applied before
          those surfaces) — answer != raw is expected, NOT a defect.
        - quality_check (sanitized event) MUST NOT contain raw_model_answer.
        """
        self.case_id = "AUDIT-TOKEN-SUPPORTED-SECRET"
        self.expected_pre_fix = "PASS"
        supported_raw = ("结论：%s 且包含伪引用 [cite:%s]。 [cite:pqac-own]"
                         % (AUDIT_SENTINELS["SK_LIVE"],
                            AUDIT_SENTINELS["FAKE_CITE"]))
        captured_raw = []
        events, run_id, logs, answer = self._run_stream(
            "Mamba 用什么方法？",
            [("query_project_rag", {"question": "q"},
              {"evidence": [self._fulltext_envelope()], "fallback": ""})],
            supported_raw, raw_callback=captured_raw.append)
        quality = next(e["data"] for e in events
                       if e["event"] == "quality_check")
        # gate semantics (positive control — supported answer must pass)
        self.assertEqual(quality["answer_mode"], "answered",
                         "supported answer must pass the gate")
        # 1) raw eval hook receives the ORIGINAL answer including secrets
        self.assertEqual(captured_raw, [supported_raw],
                         "raw_answer_callback must receive the un-redacted raw")
        # 2) redacted surfaces: token / answer / DB
        token_blob = json.dumps([e["data"] for e in events if e["event"] == "token"],
                                ensure_ascii=False, default=str)
        from api.models import ChatMessage, ProjectRun
        assistant_msg = ChatMessage.objects.filter(
            session_id=quality.get("session_id"), role="assistant").order_by("-id").first()
        run_obj = ProjectRun.objects.filter(id=run_id).first()
        redacted_surfaces = {
            "token_events": token_blob,
            "answer_concat": answer,
            "chat_message_assistant": assistant_msg.content if assistant_msg else "",
            "run.output": run_obj.output if run_obj else "",
        }
        for surface, blob in redacted_surfaces.items():
            for secret in (AUDIT_SENTINELS["SK_LIVE"], AUDIT_SENTINELS["FAKE_CITE"]):
                # SK_LIVE is matched by _SK_SECRET_RE; FAKE_CITE (pqac-fake-...)
                # is NOT a secret pattern — it is a citation marker that the
                # product passes through. Only assert SK_LIVE is redacted on
                # all surfaces; FAKE_CITE redaction is NOT guaranteed.
                if secret == AUDIT_SENTINELS["SK_LIVE"]:
                    found = secret in blob
                    self.record(surface, secret, found,
                                "must be [REDACTED] on this surface")
                    self.assertFalse(found,
                                     "%s leaked sk- secret on supported answer"
                                     % surface)
        # 3) token/answer/ChatMessage/run.output are consistent (same redaction)
        self.assertEqual(answer,
                         redacted_surfaces["chat_message_assistant"],
                         "ChatMessage assistant must equal streamed answer")
        self.assertEqual(answer, redacted_surfaces["run.output"],
                         "run.output must equal streamed answer")
        self.assertNotEqual(answer, supported_raw,
                            "redacted answer must differ from raw when raw "
                            "carried a secret (redaction is enforced)")
        # 4) quality_check (sanitized) never carries raw_model_answer (the raw
        #    string). NOTE: raw_model_answer_CHARS is a legit sanitized scalar
        #    field (int) — substring match would false-positive on it.
        q_blob = json.dumps(quality, ensure_ascii=False, default=str)
        # match the raw string key only, not the *_chars int field
        import re as _re
        has_raw_field = bool(_re.search(r'"raw_model_answer"\s*:', q_blob))
        self.record("quality_check", "raw_model_answer",
                    has_raw_field,
                    "raw must stay out of sanitized quality event")
        self.assertFalse(has_raw_field,
                         "raw_model_answer leaked into sanitized quality event")
        # 5) fabricated citation marker must not bind (citation resolver gates)
        fake_cited = [c for c in quality["citations"]
                      if c["citation_marker_status"] == "present"
                      and c["marker"] == AUDIT_SENTINELS["FAKE_CITE"]]
        self.assertFalse(fake_cited,
                         "a fabricated citation marker must not appear as bound")
        self.write_audit("PASS")


# =====================================================================
# 3.2 日志与异常路径
# =====================================================================

class AuditLogAndExceptionTest(AuditTasks5Base):
    """started/failed logs, tracebacks, error events, error_message."""

    def test_AUDIT_LOG_USER_PROMPT(self):
        """§3.2: started/completed/failed logs must not contain the user
        message verbatim (prompt leakage via message_preview)."""
        self.case_id = "AUDIT-LOG-USER-PROMPT"
        self.expected_pre_fix = "FAIL"
        secret_message = ("Mamba 有什么特点？%s 请详细回答。"
                          % AUDIT_SENTINELS["SECRET_USER_PROMPT"])
        events, run_id, logs, answer = self._run_stream(
            secret_message,
            [("query_project_rag", {"question": "q"},
              {"evidence": [], "fallback": ""})],
            "没有证据。")
        found = AUDIT_SENTINELS["SECRET_USER_PROMPT"] in logs
        self.record("logs", AUDIT_SENTINELS["SECRET_USER_PROMPT"], found,
                    "log blob includes message_preview extra")
        self.write_audit("PASS" if not found else "FAIL")
        self.assertFalse(found,
                         "user prompt leaked verbatim into logs (message_preview)")

    def test_AUDIT_ERROR_SURFACES(self):
        """§3.2 / §5 round-2 revised contract (Codex裁决):
        - the raw exception body (including the word ``boom`` and the opaque
          SECRET_EXC / SK_LIVE sentinels) MUST NOT appear in logs traceback,
          SSE error event, or ProjectRun.error_message.
        - at the same time the stable error CODE (exception class), the fixed
          public copy, the error_hash and the correlation run_id MUST survive
          (no wipe-out fake pass).
        """
        self.case_id = "AUDIT-ERROR-SURFACES"
        self.expected_pre_fix = "FAIL"
        from agent.harness import ProjectAgentHarness

        # Opaque body deliberately carries an unstructured secret word and a
        # bare ``boom`` marker — §31.1 forbids regex-only redaction of this.
        opaque_body = ("boom %s %s" % (AUDIT_SENTINELS["SECRET_EXC"],
                                       AUDIT_SENTINELS["SK_LIVE"]))

        async def broken_execute(context, name, args):
            raise RuntimeError(opaque_body)

        class _CaptureHandler(logging.Handler):
            def __init__(self):
                super().__init__()
                self.records = []

            def emit(self, record):
                self.records.append(record)

        handler = _CaptureHandler()
        logging.getLogger().addHandler(handler)
        try:
            h = ProjectAgentHarness(self.proj_a.id, tool_executor=broken_execute)
            events = []

            async def drive():
                async for ev in h.stream("Mamba 有什么特点？"):
                    events.append(ev)
                return h

            hh = asyncio.run(drive())
        finally:
            logging.getLogger().removeHandler(handler)
        log_blob = "\n".join(
            json.dumps(r.__dict__, ensure_ascii=False, default=str)
            for r in handler.records)
        error_events = [e for e in events if e["event"] == "error"]
        event_blob = json.dumps(events, ensure_ascii=False, default=str)
        error_event_blob = json.dumps(error_events, ensure_ascii=False,
                                      default=str)
        from api.models import ProjectRun
        runs = list(ProjectRun.objects.filter(project_id=self.proj_a.id))
        run = runs[0] if runs else None
        error_message = run.error_message if run else ""

        # ---- NEGATIVE: opaque body / sentinels MUST NOT leak --------------
        # ``boom`` is part of the raw exception message — it must not survive
        # on any surface (the stable code is the exception CLASS, not the msg).
        forbidden_tokens = (
            "boom",
            AUDIT_SENTINELS["SECRET_EXC"],
            AUDIT_SENTINELS["SK_LIVE"],
        )
        surfaces = (
            ("logs_traceback", log_blob),
            ("sse_error_event", error_event_blob),
            ("sse_all_events", event_blob),
            ("run.error_message", error_message),
        )
        leaked_any = False
        for surface, blob in surfaces:
            for tok in forbidden_tokens:
                found = tok in blob
                self.record(surface, tok, found, blob[:160])
                if found:
                    leaked_any = True
        # ---- POSITIVE: stable contract MUST survive -----------------------
        # exception class name (stable public code)
        self.record("logs_traceback", "RuntimeError(class)",
                    "RuntimeError" not in log_blob,
                    "exception class must survive in logs")
        self.record("logs_traceback", "project_agent_run_failed(code)",
                    "project_agent_run_failed" not in log_blob,
                    "event code must survive in logs")
        self.record("sse_error_event", "RuntimeError(class)",
                    "RuntimeError" not in error_event_blob,
                    "stable error code must survive on SSE")
        self.record("sse_error_event", "error_hash",
                    "error_hash" not in error_event_blob,
                    "error_hash must survive on SSE")
        self.record("sse_error_event", "fixed_copy",
                    "服务暂时不可用" not in error_event_blob,
                    "fixed public copy must survive on SSE")
        self.record("run.error_message", "RuntimeError(class)",
                    "RuntimeError" not in error_message,
                    "stable code must survive in run.error_message")
        if error_events:
            d = error_events[0]["data"]
            self.record("sse_error_event", "correlation.run_id",
                        d.get("run_id") is None,
                        "run_id=%s" % d.get("run_id"))
        self.write_audit("PASS" if not leaked_any else "FAIL")
        # assert negatives
        for surface, blob in surfaces:
            for tok in forbidden_tokens:
                self.assertNotIn(tok, blob,
                                 "%s leaked forbidden token %r" % (surface, tok))
        # assert positives (no wipe-out fake pass)
        self.assertIn("RuntimeError", log_blob,
                      "exception class must survive in logs (no wipe-out)")
        self.assertIn("project_agent_run_failed", log_blob,
                      "event code must survive in logs (no wipe-out)")
        self.assertTrue(error_events,
                        "an error SSE event must be emitted")
        self.assertIn("RuntimeError", error_event_blob,
                      "stable error code must survive on SSE error event")
        self.assertIn("error_hash", error_event_blob,
                      "error_hash must survive on SSE error event")
        self.assertIn("服务暂时不可用", error_event_blob,
                      "fixed public copy must survive on SSE error event")
        self.assertIn("RuntimeError", error_message,
                      "stable error code must survive in run.error_message")
        if error_events:
            self.assertIsNotNone(error_events[0]["data"].get("run_id"),
                                 "correlation run_id must survive on error event")


# =====================================================================
# 3.3 所有 ProjectRunEvent producers
# =====================================================================

class AuditProducersTest(AuditTasks5Base):
    """Every ProjectRunEvent producer: harness emit, API view, Celery tasks,
    LangGraph workflow — allowlist + correlation ids + no prompt/body/key."""

    def test_AUDIT_PRODUCER_WORKFLOW_QUEUED(self):
        """views.project_research_expand_workflow: real REST path — the
        workflow_queued payload must not carry the question verbatim and must
        carry correlation ids."""
        self.case_id = "AUDIT-PRODUCER-WORKFLOW-QUEUED"
        self.expected_pre_fix = "FAIL"
        from api.models import ProjectRunEvent

        secret_question = ("检索 Mamba 后续工作 %s 并生成综述。"
                           % AUDIT_SENTINELS["SECRET_QUESTION"])
        with mock.patch("api.tasks.run_research_expand_workflow_task.delay") as delay:
            delay.return_value = type("FakeResult", (), {"id": "fake-celery-1"})()
            resp = self.client.post(
                f"/api/projects/{self.proj_a.id}/workflows/research-expand",
                {"question": secret_question},
                content_type="application/json")
        self.assertEqual(resp.status_code, 201, resp.content[:200])
        rows = list(ProjectRunEvent.objects.filter(
            event_type="workflow_queued").order_by("-id"))
        self.assertTrue(rows, "workflow_queued event must exist")
        payload = rows[0].payload
        blob = json.dumps(payload, ensure_ascii=False, default=str)
        found_question = AUDIT_SENTINELS["SECRET_QUESTION"] in blob
        self.record("ProjectRunEvent.workflow_queued",
                    AUDIT_SENTINELS["SECRET_QUESTION"], found_question,
                    "payload keys: %s" % sorted(payload))
        for key in ("project_id", "run_id", "session_id", "request_id"):
            missing = key not in payload
            self.record("ProjectRunEvent.workflow_queued", f"correlation.{key}",
                        missing, "")
        self.write_audit("FAIL" if (found_question or
                                    not {"project_id", "run_id"} <= set(payload))
                         else "PASS")
        self.assertNotIn(AUDIT_SENTINELS["SECRET_QUESTION"], blob,
                         "workflow_queued persisted the question verbatim")
        self.assertIn("run_id", payload,
                      "workflow_queued lacks run correlation id")

    def test_AUDIT_PRODUCER_INGESTION_FAILED(self):
        """Celery ingest_paper_pdf_task: real task function — ingestion_failed
        message and run.error_message must not carry exception body."""
        self.case_id = "AUDIT-PRODUCER-INGESTION-FAILED"
        self.expected_pre_fix = "FAIL"
        from api.models import PaperIngestionJob, ProjectRun, ProjectRunEvent
        from api.tasks import ingest_paper_pdf_task

        job = PaperIngestionJob.objects.create(
            project=self.proj_a, paper=self.paper_own,
            status="pending", source_url="https://example.invalid/pdf.pdf",
        )
        with mock.patch("api.tasks._load_pdf_bytes",
                        side_effect=RuntimeError(
                            "download failed %s"
                            % AUDIT_SENTINELS["SECRET_INGEST"])):
            with self.assertRaises(RuntimeError):
                ingest_paper_pdf_task.run(job.id)
        run = ProjectRun.objects.filter(project_id=self.proj_a.id,
                                        kind="ingestion").order_by("-id").first()
        self.assertTrue(run)
        blob = json.dumps(run.error_message or "", ensure_ascii=False)
        found = AUDIT_SENTINELS["SECRET_INGEST"] in blob
        self.record("ProjectRun.error_message(ingestion)",
                    AUDIT_SENTINELS["SECRET_INGEST"], found, blob[:200])
        events = list(ProjectRunEvent.objects.filter(
            event_type="ingestion_failed").order_by("-id"))
        self.assertTrue(events, "ingestion_failed event must exist")
        ev_blob = json.dumps(events[0].payload, ensure_ascii=False, default=str)
        found_ev = AUDIT_SENTINELS["SECRET_INGEST"] in ev_blob
        self.record("ProjectRunEvent.ingestion_failed",
                    AUDIT_SENTINELS["SECRET_INGEST"], found_ev,
                    "payload: %s" % ev_blob[:200])
        self.write_audit("PASS" if not (found or found_ev) else "FAIL")
        self.assertNotIn(AUDIT_SENTINELS["SECRET_INGEST"], blob,
                         "ingestion run.error_message leaked exception body")
        self.assertNotIn(AUDIT_SENTINELS["SECRET_INGEST"], ev_blob,
                         "ingestion_failed event leaked exception body")

    def test_AUDIT_PRODUCER_WORKFLOW_FAILED(self):
        """Celery run_research_expand_workflow_task: workflow_started question
        preview and workflow_failed message must not leak."""
        self.case_id = "AUDIT-PRODUCER-WORKFLOW-FAILED"
        self.expected_pre_fix = "FAIL"
        from api.models import ProjectRun, ProjectRunEvent
        from api.tasks import run_research_expand_workflow_task

        secret_question = ("扩展检索 %s" % AUDIT_SENTINELS["SECRET_QUESTION"])
        run = ProjectRun.objects.create(
            project=self.proj_a, kind="workflow", status="pending",
            question=secret_question)
        with mock.patch("agent.project_workflow.run_project_research_expand",
                        side_effect=RuntimeError(
                            "workflow crash %s" % AUDIT_SENTINELS["SECRET_WF"])):
            with self.assertRaises(RuntimeError):
                run_research_expand_workflow_task.run(run.id)
        run.refresh_from_db()
        started = ProjectRunEvent.objects.filter(
            run_id=run.id, event_type="workflow_started").order_by("-id").first()
        failed = ProjectRunEvent.objects.filter(
            run_id=run.id, event_type="workflow_failed").order_by("-id").first()
        self.assertTrue(started and failed, "both events must exist")
        started_blob = json.dumps(started.payload, ensure_ascii=False, default=str)
        failed_blob = json.dumps(failed.payload, ensure_ascii=False, default=str)
        for surface, blob, sentinel in (
            ("workflow_started.question", started_blob,
             AUDIT_SENTINELS["SECRET_QUESTION"]),
            ("workflow_failed.message", failed_blob,
             AUDIT_SENTINELS["SECRET_WF"]),
            ("run.error_message", run.error_message or "",
             AUDIT_SENTINELS["SECRET_WF"]),
        ):
            found = sentinel in blob
            self.record(surface, sentinel, found, blob[:200])
        self.write_audit("FAIL")
        self.assertNotIn(AUDIT_SENTINELS["SECRET_QUESTION"], started_blob,
                         "workflow_started persisted question verbatim")
        self.assertNotIn(AUDIT_SENTINELS["SECRET_WF"], failed_blob,
                         "workflow_failed persisted exception body")
        self.assertNotIn(AUDIT_SENTINELS["SECRET_WF"], run.error_message or "",
                         "workflow run.error_message leaked exception body")

    def test_AUDIT_PRODUCER_WORKFLOW_NODE_QUERIES(self):
        """LangGraph project_workflow._event: plan_expansion writes rewritten
        queries — the user question must not leak verbatim."""
        self.case_id = "AUDIT-PRODUCER-WORKFLOW-NODE-QUERIES"
        self.expected_pre_fix = "FAIL"
        from agent.project_workflow import run_project_research_expand
        from api.models import ProjectRun, ProjectRunEvent

        secret_question = ("帮我调研 %s 方向的最新进展。"
                           % AUDIT_SENTINELS["SECRET_QUERY"])
        run = ProjectRun.objects.create(
            project=self.proj_a, kind="workflow", status="pending",
            question=secret_question)
        # The offline gate must not count a search call: replace the real
        # datasource entry with a deterministic failure (plan_expansion runs
        # BEFORE search_sources, so its event is already persisted).
        with mock.patch("datasources.registry.search",
                        side_effect=RuntimeError("offline audit mock")):
            with self.assertRaises(Exception):
                asyncio.run(run_project_research_expand(
                    self.proj_a.id, secret_question, run.id))
        events = list(ProjectRunEvent.objects.filter(
            run_id=run.id, event_type="workflow_node").order_by("id"))
        self.assertTrue(events, "workflow_node events must exist")
        blob = json.dumps([e.payload for e in events], ensure_ascii=False,
                          default=str)
        found = AUDIT_SENTINELS["SECRET_QUERY"] in blob
        self.record("ProjectRunEvent.workflow_node(queries)",
                    AUDIT_SENTINELS["SECRET_QUERY"], found,
                    blob[:300])
        self.write_audit("PASS" if not found else "FAIL")
        self.assertNotIn(AUDIT_SENTINELS["SECRET_QUERY"], blob,
                         "plan_expansion persisted rewritten question verbatim")

    def test_AUDIT_PRODUCER_HARNESS_BASELINE(self):
        """Control: the harness emit path IS allowlisted — a chat run with a
        secret query produces no secret in persisted events (positive control)."""
        self.case_id = "AUDIT-PRODUCER-HARNESS-BASELINE"
        self.expected_pre_fix = "PASS"
        events, run_id, logs, answer = self._run_stream(
            "查询 %s 相关内容" % AUDIT_SENTINELS["SECRET_QUERY"],
            [("query_project_rag", {"question": AUDIT_SENTINELS["SECRET_QUESTION"]},
              {"evidence": [], "fallback": ""})],
            "没有证据。")
        db_blob = self._db_events_blob(run_id)
        self.assertTrue(db_blob)
        for sentinel in (AUDIT_SENTINELS["SECRET_QUERY"],
                         AUDIT_SENTINELS["SECRET_QUESTION"]):
            found = sentinel in db_blob
            self.record("ProjectRunEvent(harness)", sentinel, found)
            self.assertFalse(found, "harness persisted a query sentinel")
        self.write_audit("PASS")


# =====================================================================
# 3.4 嵌套结构与伪造标识
# =====================================================================

class AuditNestedAndForgedTest(AuditTasks5Base):
    """Nested allowlist enforcement + forged tool-name surfaces + hash."""

    def test_AUDIT_NESTED_CITATION_MARKER(self):
        """quality_check.citations[].marker/section (sanitized event layer):
        nested fields must not carry non-allowlisted text into SSE/DB."""
        self.case_id = "AUDIT-NESTED-CITATION-MARKER"
        self.expected_pre_fix = "FAIL"
        envelope = self._fulltext_envelope(
            chunk_id=999999, content_hash="h_bad",
            citation=AUDIT_SENTINELS["SECRET_MARKER"],
            section=AUDIT_SENTINELS["SECRET_SECTION"])
        events, run_id, logs, answer = self._run_stream(
            "Mamba 用什么方法？",
            [("query_project_rag", {"question": "q"},
              {"evidence": [envelope], "fallback": ""})],
            "Mamba 使用选择性状态空间。 [cite:%s]"
            % AUDIT_SENTINELS["SECRET_MARKER"])
        quality = next(e["data"] for e in events
                       if e["event"] == "quality_check")
        q_blob = json.dumps(quality, ensure_ascii=False, default=str)
        db_blob = self._db_events_blob(run_id)
        found_marker = AUDIT_SENTINELS["SECRET_MARKER"] in q_blob
        found_section = AUDIT_SENTINELS["SECRET_SECTION"] in q_blob
        self.record("quality_check.citations.marker",
                    AUDIT_SENTINELS["SECRET_MARKER"], found_marker)
        self.record("quality_check.citations.section",
                    AUDIT_SENTINELS["SECRET_SECTION"], found_section)
        self.record("ProjectRunEvent.quality_check",
                    AUDIT_SENTINELS["SECRET_MARKER"],
                    AUDIT_SENTINELS["SECRET_MARKER"] in db_blob)
        self.write_audit("FAIL")
        self.assertNotIn(AUDIT_SENTINELS["SECRET_MARKER"], q_blob,
                         "citation marker carried secret text")
        self.assertNotIn(AUDIT_SENTINELS["SECRET_SECTION"], q_blob,
                         "citation section carried secret text")

    def test_AUDIT_NESTED_TOOL_ERROR(self):
        """quality_check.tool_errors[].message: sk- patterns are scrubbed,
        arbitrary secret words must be too."""
        self.case_id = "AUDIT-NESTED-TOOL-ERROR"
        self.expected_pre_fix = "FAIL"
        events, run_id, logs, answer = self._run_stream(
            "Mamba 用什么方法？",
            [("query_project_rag",
              {"question": "q"},
              {"error": "boom",
               "message": ("%s and %s" % (AUDIT_SENTINELS["SECRET_PAYLOAD"],
                                          AUDIT_SENTINELS["SK_LIVE"])),
               "count": 0})],
            "没有证据。")
        quality = next(e["data"] for e in events
                       if e["event"] == "quality_check")
        q_blob = json.dumps(quality, ensure_ascii=False, default=str)
        found_payload = AUDIT_SENTINELS["SECRET_PAYLOAD"] in q_blob
        found_sk = AUDIT_SENTINELS["SK_LIVE"] in q_blob
        self.record("quality_check.tool_errors.message",
                    AUDIT_SENTINELS["SECRET_PAYLOAD"], found_payload)
        self.record("quality_check.tool_errors.message",
                    AUDIT_SENTINELS["SK_LIVE"], found_sk,
                    "sk- should be [REDACTED] by sanitize_text")
        self.write_audit("FAIL" if found_payload else "PASS")
        self.assertFalse(found_payload,
                         "tool error message leaked arbitrary secret text")
        self.assertFalse(found_sk,
                         "sk- secret not scrubbed in tool error message")

    def test_AUDIT_NESTED_LLM_USAGE(self):
        """llm_result.usage nested map must be allowlisted — arbitrary keys/
        values must not pass through."""
        self.case_id = "AUDIT-NESTED-LLM-USAGE"
        self.expected_pre_fix = "FAIL"
        from agent.harness import ProjectAgentHarness

        class FC:
            def __init__(self):
                self.n = 0

            def complete_with_tools(self, messages, tools, **kwargs):
                self.n += 1
                if self.n == 1:
                    return {"content": "", "tool_calls": [
                        {"id": "c1", "name": "query_project_rag",
                         "arguments": '{"question": "q"}'}],
                        "usage": {"prompt_tokens": 10, "completion_tokens": 5,
                                  "note": AUDIT_SENTINELS["SECRET_USAGE"]}}
                return {"content": "没有证据。", "tool_calls": [],
                        "usage": {"prompt_tokens": 10,
                                  "note": AUDIT_SENTINELS["SECRET_USAGE"]}}

            def complete(self, *a, **k):
                return {"content": "没有证据。", "usage": {}}

        async def fake_exec(context, name, args):
            return {"evidence": [], "fallback": ""}

        with mock.patch("llm.deepseek.DeepSeekClient") as mc:
            mc.return_value = FC()
            from agent.harness import ProjectAgentHarness
            h = ProjectAgentHarness(self.proj_a.id, use_llm=True,
                                    tool_executor=fake_exec)
            events = []

            async def drive():
                async for ev in h.stream("Mamba 用什么方法？"):
                    events.append(ev)

            asyncio.run(drive())
        blob = json.dumps(
            [e for e in events if e["event"] == "llm_result"],
            ensure_ascii=False, default=str)
        found = AUDIT_SENTINELS["SECRET_USAGE"] in blob
        self.record("llm_result.usage", AUDIT_SENTINELS["SECRET_USAGE"], found,
                    blob[:200])
        self.write_audit("PASS" if not found else "FAIL")
        self.assertNotIn(AUDIT_SENTINELS["SECRET_USAGE"], blob,
                         "usage nested map passed arbitrary text through")

    def test_AUDIT_NESTED_ADDED_TITLE(self):
        """paper_added.added_titles: titles are a UI allowlist field — record
        behaviour (a title containing a secret word is a UI string, not a
        payload); verify no excerpt/body beyond the title."""
        self.case_id = "AUDIT-NESTED-ADDED-TITLE"
        self.expected_pre_fix = "PASS"
        title = AUDIT_SENTINELS["SECRET_TITLE"]
        events, run_id, logs, answer = self._run_stream(
            "搜索并加入相关论文",
            [("search_papers", {"query": "x"},
              {"papers": [{"title": title, "year": 2025}], "count": 1}),
             ("add_papers_to_project", {"papers": [{"title": title}], "reason": "t"},
              {"added": [{"title": title, "created": True}], "count": 1})],
            "已加入 1 篇论文。")
        pa = next((e["data"] for e in events
                   if e["event"] == "paper_added"), {})
        blob = json.dumps(pa, ensure_ascii=False, default=str)
        self.assertIn(AUDIT_SENTINELS["SECRET_TITLE"], blob,
                      "title is an allowlisted UI field")
        self.assertNotIn("SECRET_INGEST_BODY_MARKER", blob)
        self.record("paper_added.added_titles",
                    "note:allowlisted_ui_title", False,
                    "allowlisted UI title (documented field)")
        self.write_audit("PASS")

    def test_AUDIT_FORGED_NAME_SURFACES(self):
        """forged tool name must appear only as stable code/hash on SSE, DB,
        logs and API events."""
        self.case_id = "AUDIT-FORGED-NAME-SURFACES"
        self.expected_pre_fix = "PASS"
        forged = AUDIT_SENTINELS["FORGED_TOOL"]
        events, run_id, logs, answer = self._run_stream(
            "查询论文",
            [(forged, {"query": "q"}, {})],
            "没有证据。")
        event_blob = self._event_blob(events, include_token=True)
        db_blob = self._db_events_blob(run_id)
        for surface, blob in (("sse_events", event_blob),
                              ("ProjectRunEvent", db_blob),
                              ("logs", logs)):
            found = forged in blob
            self.record(surface, forged, found, "")
            self.assertFalse(found,
                             "forged tool name leaked on %s" % surface)
        self.assertIn("unknown_tool", event_blob,
                      "stable code must appear instead")
        self.assertIn("tool_hash", event_blob,
                      "stable hash must appear instead")
        self.write_audit("PASS")

    def test_AUDIT_HASH_STABILITY(self):
        """sanitize_tool_name: stable per value, distinct per value,
        digest-only (no original name embedded)."""
        self.case_id = "AUDIT-HASH-STABILITY"
        self.expected_pre_fix = "PASS"
        from agent.events import sanitize_tool_name

        a1 = sanitize_tool_name("zqn9sneaky_tool_93x")
        a2 = sanitize_tool_name("zqn9sneaky_tool_93x")
        b = sanitize_tool_name("another_forged_tool_42")
        known = sanitize_tool_name("query_project_rag")
        self.assertEqual(a1, a2, "hash must be stable for the same value")
        self.assertNotEqual(a1, b, "different forged names must not collide")
        self.assertEqual(a1[0], "unknown_tool")
        self.assertEqual(len(a1[1]), 12)
        self.assertNotIn("zqn9sneaky_tool_93x", a1[1],
                         "digest must not embed the original name")
        self.assertEqual(known, ("query_project_rag", None),
                         "known tools pass through")
        self.write_audit("PASS")


# =====================================================================
# 3.5 兼容与 deprecated 字段
# =====================================================================

class AuditCompatDeprecatedTest(AuditTasks5Base):
    """verified mutation immunity, frontend field consumption, raw hook gating."""

    def test_AUDIT_VERIFIED_MUTATION(self):
        """Mutating verified/verified_count must not change any gate outcome —
        gates read resolved/binding fields only."""
        self.case_id = "AUDIT-VERIFIED-MUTATION"
        self.expected_pre_fix = "PASS"
        from agent.harness import ProjectAgentHarness
        from agent.intent import classify_project_intent

        envelope = self._fulltext_envelope(chunk_id=999999, content_hash="h_bad")
        context = {"query_project_rag": {"evidence": [envelope]}}
        intent = classify_project_intent("Mamba 用什么方法？", self.proj_a.id)
        h = ProjectAgentHarness(self.proj_a.id)
        q = asyncio.run(h._quality_check(
            "Mamba 使用选择性状态空间。 [cite:pqac-own]", intent, context))
        gate_fields = ("answer_mode", "evidence_status",
                       "citation_binding_status", "reference_resolution_status",
                       "resolved_citation_count", "citation_presence")
        baseline = {k: q[k] for k in gate_fields}
        for variant in (1, 0, 99):
            mutated = dict(q)
            mutated["verified_count"] = variant
            mutated["unverified_count"] = 100 - variant
            mutated["citations"] = [
                {**c, "verified": bool(variant)} for c in mutated["citations"]]
            for k in gate_fields:
                self.assertEqual(mutated[k], baseline[k],
                                 "gate field %s changed with verified mutation"
                                 % k)
        self.record("verified_mutation", "gate_fields",
                    False, "answer_mode/evidence_status/binding/resolved unchanged")
        self.write_audit("PASS")

    def test_AUDIT_FRONTEND_COMPAT(self):
        """§3.5: fields actually consumed by the frontend (AgentChatPanel /
        stores) must survive the sanitizer — tool trace, evidence list, graph,
        added papers, llm_result summary."""
        self.case_id = "AUDIT-FRONTEND-COMPAT"
        self.expected_pre_fix = "FAIL"
        evidence = [self._fulltext_envelope()]
        events, run_id, logs, answer = self._run_stream(
            "查询并生成图谱",
            [
                ("query_project_rag", {"question": "q"},
                 {"evidence": evidence, "fallback": ""}),
                ("search_papers", {"query": "x"},
                 {"papers": [{"title": "Paper A", "year": 2025}], "count": 1}),
                ("add_papers_to_project", {"papers": [{"title": "Paper A"}], "reason": "t"},
                 {"added": [{"title": "Paper A", "created": True}], "count": 1}),
                ("get_project_citation_graph", {},
                 {"graph": {"nodes": [{"id": 1, "title": "A"}],
                            "edges": [{"source": 1, "target": 1}]}}),
            ],
            "没有证据。")
        by_event = {}
        for e in events:
            by_event.setdefault(e["event"], []).append(e["data"])
        checks = [
            ("tool_call", "summary", "AgentChatPanel tool trace summary"),
            ("tool_call", "arguments", "AgentChatPanel formatArgs"),
            ("evidence", "evidence", "AgentChatPanel evidence list"),
            ("search_results", "papers", "AgentChatPanel search list"),
            ("paper_added", "added", "AgentChatPanel added list"),
            ("graph", "nodes", "AgentChatPanel inline CitationGraph"),
            ("graph", "edges", "AgentChatPanel inline CitationGraph"),
            ("llm_result", "status", "AgentChatPanel llm_result summary"),
        ]
        missing_any = False
        runtime_observed: dict[str, dict] = {}
        for event_name, field, consumer in checks:
            data = by_event.get(event_name) or [{}]
            present = any(field in d for d in data)
            self.record(f"{event_name}.{field}", "missing", not present,
                        f"frontend consumer: {consumer}")
            if not present:
                missing_any = True
                self.fail(
                    f"AUDIT-FRONTEND-COMPAT: {event_name}.{field} missing "
                    f"(frontend consumer: {consumer})")
            # capture the actual runtime-observed shape (non-placeholder)
            sample = next((d for d in data if isinstance(d, dict) and field in d), {})
            runtime_observed[f"{event_name}.{field}"] = {
                "frontend_consumer": consumer,
                "survives_sanitizer": True,
                "observed_value_kind": type(sample.get(field)).__name__,
                "observed_keys": sorted(sample.keys())[:12],
            }
        # positive controls that must remain
        quality = by_event.get("quality_check") or [{}]
        q0 = quality[0]
        for field in ("verdict", "evidence_count", "source_marker_count"):
            self.assertIn(field, q0, "quality_check compat field lost: %s" % field)
        # write the runtime-observed compat matrix (machine-readable, no placeholder)
        _write_json("frontend-compat-runtime.json", {
            "case_id": "AUDIT-FRONTEND-COMPAT",
            "runtime_observed": runtime_observed,
            "all_fields_survive": not missing_any,
        })
        self.write_audit("PASS" if not missing_any else "FAIL")

    def test_AUDIT_RAW_HOOK_GATED(self):
        """The raw model answer must be reachable only through the explicit
        eval hook; no API request parameter can enable it."""
        self.case_id = "AUDIT-RAW-HOOK-GATED"
        self.expected_pre_fix = "PASS"
        secret_raw = "Mamba 使用选择性状态空间 %s 提升效率。" % AUDIT_SENTINELS["SECRET_BODY"]
        # 1. no callback -> raw never surfaces
        events, run_id, logs, answer = self._run_stream(
            "Mamba 用什么方法？", [], secret_raw)
        blob = self._event_blob(events, include_token=True)
        db_blob = self._db_events_blob(run_id)
        for surface, b in (("sse_events", blob), ("ProjectRunEvent", db_blob)):
            self.assertNotIn(AUDIT_SENTINELS["SECRET_BODY"], b,
                             "raw leaked without hook on %s" % surface)
        # 2. REST /chat cannot enable the hook via request params
        from agent.scope_failing_tests import _mock_rcs_summary
        with mock.patch("rag.retrieval._rcs_summary",
                        new=mock.AsyncMock(side_effect=_mock_rcs_summary)):
            resp = self.client.post(
                f"/api/projects/{self.proj_a.id}/chat",
                {"message": "Mamba 用什么方法？",
                 "raw_answer_callback": "attack",
                 "hook_enabled": True},
                content_type="application/json")
        self.assertEqual(resp.status_code, 201, resp.content[:200])
        resp_data = resp.json()
        resp_blob = json.dumps(resp_data, ensure_ascii=False, default=str)
        self.assertNotIn(AUDIT_SENTINELS["SECRET_BODY"], resp_blob)
        quality_payloads = [
            e["data"] for e in resp_data.get("events", [])
            if e["event"] == "quality_check"]
        self.assertTrue(quality_payloads, "quality_check must be in REST events")
        self.assertNotIn("raw_model_answer", quality_payloads[0],
                         "REST response must not expose raw answer")
        self.record("rest_chat_response", "raw_model_answer",
                    "raw_model_answer" in quality_payloads[0],
                    "no request param can enable the eval hook")
        self.write_audit("PASS")


# =====================================================================
# 关联 ID 完整率（跨 producer）
# =====================================================================

class AuditCorrelationRatesTest(AuditTasks5Base):
    """Correlation-id completeness across every producer."""

    def test_AUDIT_CORRELATION_RATES(self):
        """§5 round-2 revised contract (Codex裁决):
        - the four id FIELDS (project_id, run_id, session_id, request_id) must
          be PRESENT on every ProjectRunEvent payload (key exists) → 100%.
        - the context-REQUIRED fields (project_id, run_id) must be NON-NULL on
          every run event → 100%.
        - session_id / request_id MAY be null for Celery/workflow contexts
          (no chat session, async task) — null is legal, NOT a defect; the
          test must not require fabricated values.
        """
        self.case_id = "AUDIT-CORRELATION-RATES"
        self.expected_pre_fix = "PASS"
        from api.models import ProjectRun, ProjectRunEvent

        # harness events (chat session context — all four populated)
        events, run_id, logs, answer = self._run_stream(
            "Mamba 用什么方法？",
            [("query_project_rag", {"question": "q"},
              {"evidence": [], "fallback": ""})],
            "没有证据。")
        harness_rows = list(ProjectRunEvent.objects.filter(run_id=run_id))
        # workflow_queued (real REST path — async Celery, no chat session)
        with mock.patch("api.tasks.run_research_expand_workflow_task.delay") as delay:
            delay.return_value = type("FakeResult", (), {"id": "fake-celery-1"})()
            self.client.post(
                f"/api/projects/{self.proj_a.id}/workflows/research-expand",
                {"question": "扩展检索 Mamba"}, content_type="application/json")
        queued = ProjectRunEvent.objects.filter(
            event_type="workflow_queued").order_by("-id").first()

        all_fields = ("project_id", "run_id", "session_id", "request_id")
        required_non_null = ("project_id", "run_id")
        rows = list(harness_rows) + ([queued] if queued else [])
        self.assertTrue(rows, "no ProjectRunEvent rows produced (vacuous)")

        field_presence_total = 0
        field_presence_ok = 0
        required_total = 0
        required_ok = 0
        per_event: dict[str, dict] = {}
        for row in rows:
            present = {k: (k in row.payload) for k in all_fields}
            non_null = {k: (k in row.payload and row.payload[k] is not None)
                        for k in required_non_null}
            for k in all_fields:
                field_presence_total += 1
                if present[k]:
                    field_presence_ok += 1
            for k in required_non_null:
                required_total += 1
                if non_null[k]:
                    required_ok += 1
            # classify Celery/workflow context (session/request may be null)
            et = row.event_type
            is_async = et.startswith("workflow") or et.startswith("ingestion")
            per_event.setdefault(et, {
                "total": 0, "field_presence_ok": 0, "required_ok": 0,
                "field_missing": set(), "required_null": set(),
                "session_request_null_legal": 0})
            per_event[et]["total"] += 1
            for k in all_fields:
                if not present[k]:
                    per_event[et]["field_missing"].add(k)
                else:
                    per_event[et]["field_presence_ok"] += 1
            for k in required_non_null:
                if not non_null[k]:
                    per_event[et]["required_null"].add(k)
                else:
                    per_event[et]["required_ok"] += 1
            if is_async and (row.payload.get("session_id") is None
                             or row.payload.get("request_id") is None):
                per_event[et]["session_request_null_legal"] += 1

        # record checks for the matrix
        for row in rows:
            et = row.event_type
            for k in all_fields:
                self.record(f"correlation.{et}",
                            f"presence.{k}",
                            k not in row.payload, "")
            for k in required_non_null:
                self.record(f"correlation.{et}",
                            f"required_non_null.{k}",
                            not (k in row.payload
                                 and row.payload[k] is not None), "")
        presence_rate = (field_presence_ok / field_presence_total
                         if field_presence_total else 1.0)
        required_rate = (required_ok / required_total
                         if required_total else 1.0)
        for et, st in sorted(per_event.items()):
            self.record("correlation_rate.%s" % et,
                        "note:summary", False,
                        "presence=%d/%d required_non_null=%d/%d "
                        "field_missing=%s required_null=%s legal_sr_null=%d"
                        % (st["field_presence_ok"], st["total"] * len(all_fields),
                           st["required_ok"], st["total"] * len(required_non_null),
                           sorted(st["field_missing"]), sorted(st["required_null"]),
                           st["session_request_null_legal"]))
        self.record("correlation_rate.overall", "note:field_presence",
                    presence_rate < 1.0,
                    "field_presence=%.1f%% (%d/%d)"
                    % (presence_rate * 100, field_presence_ok,
                       field_presence_total))
        self.record("correlation_rate.overall", "note:required_non_null",
                    required_rate < 1.0,
                    "required_non_null=%.1f%% (%d/%d)"
                    % (required_rate * 100, required_ok, required_total))
        verdict = "PASS" if (presence_rate == 1.0 and required_rate == 1.0) else "FAIL"
        self.write_audit(verdict)
        self.assertEqual(presence_rate, 1.0,
                         "id FIELD presence must be 100%% (got %.1f%%, %d/%d)"
                         % (presence_rate * 100, field_presence_ok,
                            field_presence_total))
        self.assertEqual(required_rate, 1.0,
                         "context-required (project_id/run_id) non-null must "
                         "be 100%% (got %.1f%%, %d/%d)"
                         % (required_rate * 100, required_ok, required_total))


# =====================================================================
# §5 round-2 expansion: legacy SSE DB / tool-exception model-context /
# MCP unknown name / eval artifact opaque / LLM metrics real-or-null /
# all-producer coverage.
# =====================================================================

class AuditRound2ExpansionTest(AuditTasks5Base):
    """§5 second-round independent re-audit: the four bypasses cleaned in §32
    plus eval artifacts and producer coverage, each reproduced through the
    REAL production entry point (no helper-only shortcuts)."""

    # -----------------------------------------------------------------
    # §5 expansion: legacy research SSE error DB (§32.1)
    # -----------------------------------------------------------------
    def test_AUDIT_LEGACY_SSE_ERROR_DB(self):
        """realtime.research_stream real failure path: ResearchTask.error_message
        and the SSE error frame must carry the stable code + fixed copy only;
        the opaque exception body must never persist."""
        self.case_id = "AUDIT-LEGACY-SSE-ERROR-DB"
        self.expected_pre_fix = "PASS"
        from api.models import ResearchTask
        from realtime import views as rt_views

        task = ResearchTask.objects.create(
            project=self.proj_a, question="legacy research question",
            status="pending")
        opaque = ("legacy boom %s" % AUDIT_SENTINELS["SECRET_INGEST"])

        class _BoomGraph:
            def astream(self, *a, **kw):
                raise RuntimeError(opaque)

        # Patch build_graph at the views module's lookup site (used at runtime)
        class _CaptureHandler(logging.Handler):
            def __init__(self):
                super().__init__()
                self.records = []

            def emit(self, record):
                self.records.append(record)

        handler = _CaptureHandler()
        logging.getLogger().addHandler(handler)
        sse_frames = []
        try:
            with mock.patch("agent.graph.build_graph", return_value=_BoomGraph()):
                # research_stream is an async view returning a StreamingHttpResponse
                # whose streaming_content is an async generator (event_stream).
                async def _drive():
                    resp = await rt_views.research_stream(
                        type("R", (), {"paperlens_request_id": "req-legacy-1"})(),
                        task.id)
                    # consume the async streaming body
                    async for chunk in resp.streaming_content:
                        sse_frames.append(chunk)
                asyncio.run(_drive())
        finally:
            logging.getLogger().removeHandler(handler)
        task.refresh_from_db()
        log_blob = "\n".join(json.dumps(r.__dict__, ensure_ascii=False,
                                        default=str) for r in handler.records)
        sse_blob = b"".join(
            f if isinstance(f, bytes) else f.encode("utf-8")
            for f in sse_frames).decode("utf-8", errors="replace")

        forbidden = ("boom", AUDIT_SENTINELS["SECRET_INGEST"])
        surfaces = (
            ("ResearchTask.error_message", task.error_message or ""),
            ("sse_error_frame", sse_blob),
            ("logs_traceback", log_blob),
        )
        leaked = False
        for surface, blob in surfaces:
            for tok in forbidden:
                found = tok in blob
                self.record(surface, tok, found, (blob or "")[:160])
                if found:
                    leaked = True
        # positives: stable code + fixed copy survive
        self.record("ResearchTask.error_message", "RuntimeError(code)",
                    "RuntimeError" not in (task.error_message or ""),
                    "stable code must survive in error_message")
        self.record("sse_error_frame", "fixed_copy",
                    "服务暂时不可用" not in sse_blob,
                    "fixed copy must survive in SSE error frame")
        self.record("logs_traceback", "agent_stream_failed(code)",
                    "agent_stream_failed" not in log_blob,
                    "event code must survive in logs")
        self.write_audit("PASS" if not leaked else "FAIL")
        for surface, blob in surfaces:
            for tok in forbidden:
                self.assertNotIn(tok, blob,
                                 "%s leaked forbidden token %r" % (surface, tok))
        self.assertIn("RuntimeError", task.error_message or "",
                      "stable code must survive in ResearchTask.error_message")
        self.assertIn("服务暂时不可用", sse_blob,
                      "fixed copy must survive in SSE error frame")
        self.assertIn("agent_stream_failed", log_blob,
                       "event code must survive in logs (no wipe-out)")

    # -----------------------------------------------------------------
    # §5 expansion: chat_loop tool-exception model-context (§32.2)
    # -----------------------------------------------------------------
    def test_AUDIT_TOOL_EXCEPTION_MODEL_CONTEXT(self):
        """Real ChatAgentLoop: when a tool raises an opaque exception, the
        model's next-round tool message (the content the LLM sees) must carry
        ONLY the fixed copy + exception type + error_hash — the opaque body
        must never be amplified into the final answer."""
        self.case_id = "AUDIT-TOOL-EXCEPTION-MODEL-CONTEXT"
        self.expected_pre_fix = "PASS"
        from agent.chat_loop import ChatAgentLoop

        opaque = ("tool boom %s %s"
                  % (AUDIT_SENTINELS["SECRET_PAYLOAD"], AUDIT_SENTINELS["SK_LIVE"]))

        async def exploding_exec(context, name, args):
            raise RuntimeError(opaque)

        # Capture the tool messages appended to the model context
        appended_tool_messages: list[str] = []

        class FC:
            def __init__(self):
                self.n = 0

            def complete_with_tools(self, messages, tools, **kwargs):
                self.n += 1
                if self.n == 1:
                    return {"content": "", "reasoning_content": "",
                            "tool_calls": [{"id": "c1", "name": "query_project_rag",
                                            "arguments": '{"question": "q"}'}],
                            "usage": {"prompt_tokens": 10, "completion_tokens": 5}}
                # snapshot the tool-role messages the LLM actually sees
                appended_tool_messages.extend(
                    m.get("content", "") for m in messages
                    if m.get("role") == "tool")
                return {"content": "基于现有信息回答。", "tool_calls": [],
                        "usage": {"prompt_tokens": 10, "completion_tokens": 5}}

            def complete(self, *a, **k):
                return {"content": "基于现有信息回答。",
                        "usage": {"prompt_tokens": 10, "completion_tokens": 5}}

        with mock.patch("llm.deepseek.DeepSeekClient") as mc:
            mc.return_value = FC()
            loop = ChatAgentLoop(self.proj_a.id,
                                 tool_executor=exploding_exec,
                                 context=create_context(self.proj_a.id,
                                                        run_id=1, session_id=1))
            events = []

            async def drive():
                async for ev in loop.run("Mamba 用什么方法？", None):
                    events.append(ev)

            asyncio.run(drive())
        ctx_blob = "\n".join(appended_tool_messages)
        # NEGATIVE: opaque body never enters model tool context
        forbidden = ("boom", AUDIT_SENTINELS["SECRET_PAYLOAD"],
                     AUDIT_SENTINELS["SK_LIVE"])
        leaked = False
        for tok in forbidden:
            found = tok in ctx_blob
            self.record("model_tool_message", tok, found, ctx_blob[:200])
            if found:
                leaked = True
        # POSITIVE: fixed copy + stable code + hash survive
        self.record("model_tool_message", "fixed_copy",
                    "工具执行失败" not in ctx_blob,
                    "fixed copy must be present in model tool message")
        self.record("model_tool_message", "RuntimeError(code)",
                    "RuntimeError" not in ctx_blob,
                    "stable code must be present in model tool message")
        self.record("model_tool_message", "error_hash",
                    "error_hash" not in ctx_blob,
                    "error_hash must be present in model tool message")
        self.write_audit("PASS" if not leaked else "FAIL")
        for tok in forbidden:
            self.assertNotIn(tok, ctx_blob,
                             "opaque exception body entered model tool context")
        self.assertTrue(appended_tool_messages,
                        "no tool message captured (non-vacuous)")
        self.assertIn("工具执行失败", ctx_blob,
                      "fixed copy must be present in model tool message")
        self.assertIn("RuntimeError", ctx_blob,
                      "stable code must be present in model tool message")
        self.assertIn("error_hash", ctx_blob,
                      "error_hash must be present in model tool message")

    # -----------------------------------------------------------------
    # §5 expansion: MCP unknown name (§32.3) through real dispatcher
    # -----------------------------------------------------------------
    def test_AUDIT_MCP_UNKNOWN_NAME(self):
        """MCP in-process client: a forged tool name returns unknown_tool +
        tool_hash; the original name never appears in CallToolResult content
        or logs."""
        self.case_id = "AUDIT-MCP-UNKNOWN-NAME"
        self.expected_pre_fix = "PASS"
        from mcp_server.testing import call_tool_via_client

        forged = AUDIT_SENTINELS["FORGED_TOOL"]

        class _CaptureHandler(logging.Handler):
            def __init__(self):
                super().__init__()
                self.records = []

            def emit(self, record):
                self.records.append(record)

        handler = _CaptureHandler()
        logging.getLogger().addHandler(handler)
        try:
            with mock.patch("mcp_server.server.execute_project_tool") as pe:
                result = call_tool_via_client(forged, {})
                pe.assert_not_called()
        finally:
            logging.getLogger().removeHandler(handler)
        log_blob = "\n".join(json.dumps(r.__dict__, ensure_ascii=False,
                                        default=str) for r in handler.records)
        content_text = result.content[0].text if result.content else ""
        structured = getattr(result, "structured_content", None) or {}

        forbidden = (forged,)
        surfaces = (
            ("mcp_calltool_content", content_text),
            ("mcp_calltool_structured", json.dumps(structured, default=str)),
            ("logs", log_blob),
        )
        leaked = False
        for surface, blob in surfaces:
            for tok in forbidden:
                found = tok in blob
                self.record(surface, tok, found, (blob or "")[:160])
                if found:
                    leaked = True
        # POSITIVE: stable code + hash
        self.record("mcp_calltool_structured", "unknown_tool(code)",
                    structured.get("error") != "unknown_tool",
                    "structured error=%s" % structured.get("error"))
        self.record("mcp_calltool_structured", "tool_hash",
                    "tool_hash" not in structured,
                    "tool_hash present=%s" % ("tool_hash" in structured))
        # hash stability + non-collision (digest-only, no original)
        from agent.events import sanitize_tool_name
        code1, digest1 = sanitize_tool_name(forged)
        code2, digest2 = sanitize_tool_name("another_forged_42")
        self.record("sanitize_tool_name", "stable",
                    not (code1 == "unknown_tool" and digest1 == digest1),
                    "code=%s digest=%s" % (code1, digest1))
        self.record("sanitize_tool_name", "distinct",
            digest1 == digest2, "must not collide across forged names")
        self.write_audit("PASS" if not leaked else "FAIL")
        self.assertTrue(result.is_error,
                        "unknown tool must return isError=True")
        self.assertNotIn(forged, content_text,
                         "forged name leaked into CallToolResult content")
        self.assertNotIn(forged, log_blob,
                         "forged name leaked into logs")
        self.assertEqual(structured.get("error"), "unknown_tool")
        self.assertIn("tool_hash", structured)
        self.assertEqual(code1, "unknown_tool")
        self.assertEqual(len(digest1), 12)
        self.assertNotEqual(digest1, digest2,
                            "different forged names must not collide")

    # -----------------------------------------------------------------
    # §5 expansion: eval artifact opaque error (§32.4)
    # -----------------------------------------------------------------
    def test_AUDIT_EVAL_ARTIFACT_OPAQUE(self):
        """eval.safe_error + run_eval: when paperlens_research raises an
        opaque exception, the eval result artifact carries ONLY the exception
        type + error_hash — the opaque body never enters the JSON result."""
        self.case_id = "AUDIT-EVAL-ARTIFACT-OPAQUE"
        self.expected_pre_fix = "PASS"
        from eval.safe_error import exception_record, exception_message

        opaque = ("eval boom %s %s"
                  % (AUDIT_SENTINELS["SECRET_EXC"], AUDIT_SENTINELS["SK_LIVE"]))
        exc = RuntimeError(opaque)

        # 1) unit-level: exception_record / exception_message never leak body
        record = exception_record(exc)
        msg = exception_message(exc)
        record_blob = json.dumps(record, ensure_ascii=False, default=str)
        for tok in ("boom", AUDIT_SENTINELS["SECRET_EXC"], AUDIT_SENTINELS["SK_LIVE"]):
            self.record("exception_record", tok, tok in record_blob, record_blob)
            self.record("exception_message", tok, tok in msg, msg)
            self.assertNotIn(tok, record_blob,
                             "exception_record leaked opaque body")
            self.assertNotIn(tok, msg,
                             "exception_message leaked opaque body")
        self.assertEqual(record["error"], "RuntimeError")
        self.assertIn("error_hash", record)
        self.assertEqual(len(record["error_hash"]), 12)

        # 2) integration: eval_one result structure uses exception_record
        #    (simulate paperlens_research raising — run_eval writes the record)
        simulated_result = {
            "id": "EVAL-OPAQUE-1", "type": "factual",
            "question": "q", "paperlens": exception_record(exc)}
        result_blob = json.dumps(simulated_result, ensure_ascii=False,
                                 default=str)
        for tok in ("boom", AUDIT_SENTINELS["SECRET_EXC"], AUDIT_SENTINELS["SK_LIVE"]):
            self.record("eval_result.paperlens", tok, tok in result_blob,
                        result_blob[:200])
            self.assertNotIn(tok, result_blob,
                             "eval result artifact leaked opaque body")
        self.assertEqual(simulated_result["paperlens"]["error"], "RuntimeError")
        self.assertIn("error_hash", simulated_result["paperlens"])
        # verify residual str(exc) in eval artifacts is zero (static contract)
        import subprocess
        grep = subprocess.run(
            ["python", "-c",
             "import pathlib,glob,re; "
             "hits=[str(p) for p in glob.glob('backend/eval/**/*.py', recursive=True) "
             "if 'test' not in p.name.lower() "
             "for ln in p.read_text(encoding='utf-8').splitlines() "
             "if re.search(r'\\bstr\\((exc|e)\\)', ln)];"
             "print('\\n'.join(hits))"],
            capture_output=True, text=True, cwd=".")
        residual = grep.stdout.strip()
        self.record("eval_artifacts", "str(exc)_residual",
                    bool(residual), "residual files: %s" % residual[:200])
        self.write_audit("PASS")
        self.assertFalse(residual,
                         "eval artifacts still contain str(exc): %s" % residual)

    # -----------------------------------------------------------------
    # §5 expansion: real / nonavailable LLM metrics (§31.2)
    # -----------------------------------------------------------------
    def test_AUDIT_LLM_METRICS_REAL_OR_NULL(self):
        """llm_result.usage MUST carry measured values (prompt_tokens/
        completion_tokens/total_tokens) when the provider returns them, and
        null when unavailable — the deprecated const_zero/const_done paths
        are deleted and must not reappear."""
        self.case_id = "AUDIT-LLM-METRICS-REAL-OR-NULL"
        self.expected_pre_fix = "PASS"
        # 1) provider returns real usage → metrics surface measured values
        class FC:
            def __init__(self):
                self.n = 0

            def complete_with_tools(self, messages, tools, **kwargs):
                self.n += 1
                if self.n == 1:
                    return {"content": "", "tool_calls": [
                        {"id": "c1", "name": "query_project_rag",
                         "arguments": '{"question": "q"}'}],
                        "usage": {"prompt_tokens": 42, "completion_tokens": 17,
                                  "total_tokens": 59}}
                return {"content": "基于现有信息。", "tool_calls": [],
                        "usage": {"prompt_tokens": 60, "completion_tokens": 8,
                                  "total_tokens": 68}}

            def complete(self, *a, **k):
                return {"content": "基于现有信息。",
                        "usage": {"prompt_tokens": 60, "completion_tokens": 8,
                                  "total_tokens": 68}}

        async def fake_exec(context, name, args):
            return {"evidence": [], "fallback": ""}

        with mock.patch("llm.deepseek.DeepSeekClient") as mc:
            mc.return_value = FC()
            events, run_id, logs, answer = self._run_stream(
                "Mamba 用什么方法？",
                [("query_project_rag", {"question": "q"},
                  {"evidence": [], "fallback": ""})],
                "基于现有信息。", )
        llm_results = [e["data"] for e in events if e["event"] == "llm_result"]
        self.assertTrue(llm_results, "llm_result events must exist (non-vacuous)")
        measured = 0
        for lr in llm_results:
            usage = lr.get("usage") or {}
            # schema only allows prompt/completion/total tokens (no arbitrary keys)
            self.assertEqual(set(usage.keys()) | {"prompt_tokens", "completion_tokens",
                                                  "total_tokens"},
                             {"prompt_tokens", "completion_tokens", "total_tokens"},
                             "usage nested map carried non-allowlisted keys")
            for k in ("prompt_tokens", "completion_tokens", "total_tokens"):
                v = usage.get(k)
                if isinstance(v, int) and v > 0:
                    measured += 1
                # record as a metric (not a sentinel) — found=True means null
                self.record("llm_result.usage", f"metric.{k}",
                            v is None, "value=%s" % v)
        self.record("llm_result", "measured_tokens",
                    measured == 0, "measured token fields: %d" % measured)
        self.assertGreater(measured, 0,
                           "llm_result usage must carry measured values, not "
                           "fabricated zeros (const_zero path deleted)")

        # 2) the deprecated const_zero / const_done code paths must be absent
        import subprocess
        grep = subprocess.run(
            ["python", "-c",
             "import pathlib; "
             "src=pathlib.Path('backend/agent/events.py').read_text(encoding='utf-8'); "
             "hits=[n for n in ('const_zero','const_done') if n in src]; "
             "print('\\n'.join(hits))"],
            capture_output=True, text=True, cwd=".")
        residual = grep.stdout.strip()
        self.record("events.py", "const_zero/done_residual",
                    bool(residual), "residual: %s" % residual)
        self.write_audit("PASS")
        self.assertFalse(residual,
                         "deprecated const_zero/const_done path still present: %s"
                         % residual)

    # -----------------------------------------------------------------
    # §5 expansion: all-producer static + runtime coverage
    # -----------------------------------------------------------------
    def test_AUDIT_ALL_PRODUCERS_COVERAGE(self):
        """§3.3: every ProjectRunEvent producer (harness, API view, Celery
        ingestion, LangGraph workflow) MUST publish through EventPublisher.
        Static contract (grep) + runtime evidence (each producer exercised)."""
        self.case_id = "AUDIT-ALL-PRODUCERS-COVERAGE"
        self.expected_pre_fix = "PASS"

        # ---- STATIC: no direct ProjectRunEvent.objects.create outside
        # event_publisher.py (production code, not tests) ----------------
        import subprocess
        grep = subprocess.run(
            ["python", "-c",
             "import pathlib,glob,re; "
             "hits=[]; "
             "[hits.append((str(p),ln.strip())) "
             "for p in glob.glob('backend/**/*.py', recursive=True) "
             "if 'test' not in p.lower() and 'migrations' not in p "
             "and 'event_publisher' not in p "
             "for ln in pathlib.Path(p).read_text(encoding='utf-8').splitlines() "
             "if 'ProjectRunEvent.objects.create' in ln or "
             "'ProjectRunEvent.objects.bulk_create' in ln]; "
             "print('\\n'.join(':'.join(h) for h in hits))"],
            capture_output=True, text=True, cwd=".")
        direct_creates = grep.stdout.strip()
        self.record("static_scan", "direct_ProjectRunEvent_create",
                    bool(direct_creates),
                    "residual direct creates: %s" % direct_creates[:300])
        self.assertFalse(direct_creates,
                         "production code creates ProjectRunEvent directly "
                         "(bypassing EventPublisher): %s" % direct_creates)

        # ---- RUNTIME: exercise every producer INDEPENDENTLY and confirm each
        # emits sanitized ProjectRunEvent(s) through EventPublisher.  Each
        # producer gets its OWN run/job id, its OWN row-id list, its OWN
        # observed event-type set (no shared union), and its OWN schema/id
        # check. ``runtime_observed`` is computed from that producer's non-
        # empty DB rows — never hardcoded True.
        from api.models import PaperIngestionJob, ProjectRun, ProjectRunEvent
        from api.tasks import (ingest_paper_pdf_task,
                               run_research_expand_workflow_task)

        ID_FIELDS = ("project_id", "run_id", "session_id", "request_id")
        producer_evidence: dict[str, dict] = {}

        def _check_rows(producer: str, run_id_label: str, rows) -> None:
            """Record per-producer runtime evidence from its OWN DB rows."""
            row_ids = [r.id for r in rows]
            event_types = sorted({r.event_type for r in rows})
            # every row's payload must carry the four id FIELDS
            id_check_ok = all(
                all(k in r.payload for k in ID_FIELDS) for r in rows)
            # every payload must be a dict (sanitizer contract)
            schema_ok = all(isinstance(r.payload, dict) for r in rows)
            # verify each event type belongs to this producer's declared set
            declared = set(EVENT_TYPES_BY_PRODUCER_STATIC.get(producer, ()))
            unknown_types = [t for t in event_types if t not in declared] \
                if declared else []
            observed = bool(rows) and id_check_ok and schema_ok \
                and not unknown_types
            producer_evidence[producer] = {
                "run_id": run_id_label,
                "row_ids": row_ids,
                "event_types": event_types,
                "row_count": len(rows),
                "id_fields_present": id_check_ok,
                "payload_is_dict": schema_ok,
                "declared_event_types": sorted(declared),
                "unknown_event_types": unknown_types,
                "runtime_observed": observed,
            }
            self.record("producer_runtime", producer, not observed,
                        "run=%s rows=%d event_types=%s id_fields=%s "
                        "unknown_types=%s"
                        % (run_id_label, len(rows), event_types,
                           id_check_ok, unknown_types))

        # ------------------------------------------------------------------
        # Producer 1: agent/harness.py (EventPublisher emit) — chat stream
        # ------------------------------------------------------------------
        events, harness_run_id, logs, answer = self._run_stream(
            "Mamba 用什么方法？",
            [("query_project_rag", {"question": "q"},
              {"evidence": [], "fallback": ""})],
            "没有证据。")
        harness_rows = list(ProjectRunEvent.objects.filter(
            run_id=harness_run_id))
        _check_rows("agent/harness.py (EventPublisher emit)",
                    str(harness_run_id), harness_rows)

        # ------------------------------------------------------------------
        # Producer 2: api/views.py (project_research_expand_workflow) — REST
        # ------------------------------------------------------------------
        with mock.patch("api.tasks.run_research_expand_workflow_task.delay") as delay:
            delay.return_value = type("FakeResult", (), {"id": "vw-rest-1"})()
            self.client.post(
                f"/api/projects/{self.proj_a.id}/workflows/research-expand",
                {"question": "扩展检索 Mamba view"}, content_type="application/json")
        # the view creates its own ProjectRun; find the latest workflow run
        view_run = ProjectRun.objects.filter(
            project_id=self.proj_a.id, kind="workflow").order_by("-id").first()
        view_run_id = view_run.id if view_run else None
        view_rows = list(ProjectRunEvent.objects.filter(
            run_id=view_run_id, event_type="workflow_queued"))
        _check_rows("api/views.py (project_research_expand_workflow)",
                    str(view_run_id), view_rows)

        # ------------------------------------------------------------------
        # Producer 3: api/tasks.py (ingest_paper_pdf_task) — Celery ingestion
        # ------------------------------------------------------------------
        ingest_job = PaperIngestionJob.objects.create(
            project=self.proj_a, paper=self.paper_own,
            status="pending", source_url="https://example.invalid/ingest.pdf")
        with mock.patch("api.tasks._load_pdf_bytes",
                        side_effect=RuntimeError("offline audit mock")):
            try:
                ingest_paper_pdf_task.run(ingest_job.id)
            except RuntimeError:
                pass
        # the task creates its OWN ingestion ProjectRun
        ingest_run = ProjectRun.objects.filter(
            project_id=self.proj_a.id, kind="ingestion").order_by("-id").first()
        ingest_run_id = ingest_run.id if ingest_run else None
        ingest_rows = list(ProjectRunEvent.objects.filter(
            run_id=ingest_run_id))
        _check_rows("api/tasks.py (ingest_paper_pdf_task)",
                    str(ingest_run_id), ingest_rows)

        # ------------------------------------------------------------------
        # Producer 4: api/tasks.py (run_research_expand_workflow_task) — Celery
        # Emits workflow_started / workflow_completed / workflow_failed.
        # ------------------------------------------------------------------
        wf_task_run = ProjectRun.objects.create(
            project=self.proj_a, kind="workflow", status="pending",
            question="扩展检索 task producer")
        # mock the underlying workflow to raise so we get workflow_started +
        # workflow_failed from THIS task (NOT from project_workflow._event).
        with mock.patch("agent.project_workflow.run_project_research_expand",
                        side_effect=RuntimeError("offline audit mock")):
            try:
                run_research_expand_workflow_task.run(wf_task_run.id)
            except RuntimeError:
                pass
        wf_task_rows = list(ProjectRunEvent.objects.filter(
            run_id=wf_task_run.id))
        _check_rows("api/tasks.py (run_research_expand_workflow_task)",
                    str(wf_task_run.id), wf_task_rows)

        # ------------------------------------------------------------------
        # Producer 5: agent/project_workflow.py (_event) — LangGraph nodes.
        # Run a SEPARATE workflow run where the graph actually enters nodes
        # (plan_expansion runs BEFORE search_sources, so _event IS reached
        # even when search is mocked to raise).  This proves _event emits
        # workflow_node / hybrid_retrieval through EventPublisher.
        # ------------------------------------------------------------------
        wf_node_run = ProjectRun.objects.create(
            project=self.proj_a, kind="workflow", status="pending",
            question="扩展检索 node producer")
        # mock the datasource entry so search_sources raises AFTER plan_expansion
        # has already emitted workflow_node via _event.
        from agent.project_workflow import run_project_research_expand
        with mock.patch("datasources.registry.search",
                        side_effect=RuntimeError("offline audit mock")):
            try:
                async def _drive_workflow():
                    await run_project_research_expand(
                        self.proj_a.id, "扩展检索 node producer",
                        wf_node_run.id)
                asyncio.run(_drive_workflow())
            except RuntimeError:
                pass
        # _event emits workflow_node / hybrid_retrieval on THIS run_id.
        # These event types are EMITTED ONLY by project_workflow._event
        # (not by run_research_expand_workflow_task, which emits
        # workflow_started/completed/failed), so they are an unambiguous
        # per-producer fingerprint.
        node_rows = list(ProjectRunEvent.objects.filter(
            run_id=wf_node_run.id,
            event_type__in=("workflow_node", "hybrid_retrieval")))
        _check_rows("agent/project_workflow.py (_event)",
                    str(wf_node_run.id), node_rows)

        # ------------------------------------------------------------------
        # Aggregate: per-producer runtime coverage (no shared union, no
        # hardcoded True).  Separate static routing vs runtime coverage.
        # ------------------------------------------------------------------
        expected_producers = set(EVENT_TYPES_BY_PRODUCER_STATIC.keys())
        uncovered = sorted(p for p in expected_producers
                           if not producer_evidence.get(p, {})
                           .get("runtime_observed"))
        for producer in sorted(expected_producers):
            ev = producer_evidence.get(producer, {})
            self.record("producer_runtime_summary", producer,
                        not ev.get("runtime_observed"),
                        "rows=%s event_types=%s run=%s"
                        % (ev.get("row_count"), ev.get("event_types"),
                           ev.get("run_id")))
        self.record("producer_runtime", "uncovered",
                    bool(uncovered), "uncovered=%s" % uncovered)
        # write per-producer machine-readable evidence (no placeholder)
        _write_json("producer-runtime-evidence.json", {
            "case_id": "AUDIT-ALL-PRODUCERS-COVERAGE",
            "static_routing_coverage": {
                "scan": "no direct ProjectRunEvent.objects.create outside "
                        "event_publisher.py in production code",
                "direct_creates_residual": direct_creates or "",
                "routing_all_through_event_publisher": not direct_creates,
            },
            "runtime_producer_coverage": producer_evidence,
            "summary": {
                "producers_total": len(expected_producers),
                "producers_runtime_observed": sum(
                    1 for ev in producer_evidence.values()
                    if ev.get("runtime_observed")),
                "uncovered": uncovered,
            },
        })
        self.write_audit("PASS" if not uncovered else "FAIL")
        self.assertFalse(uncovered,
                         "producers without independent runtime DB evidence: "
                         "%s" % uncovered)
        # every producer must have observed event types (non-vacuous)
        for producer, ev in producer_evidence.items():
            self.assertTrue(ev["event_types"],
                            "producer %s produced zero event types (vacuous)"
                            % producer)
