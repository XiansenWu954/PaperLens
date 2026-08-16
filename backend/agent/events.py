"""Tasks 5.x: single event serializer with recursive per-type schemas (§30.2).

Every outward event payload (SSE, token stream, ProjectRunEvent persistence)
passes through ``sanitize_event`` with a typed/recursive schema — dict/list
values (usage, citations, tool_errors, workflow payloads, graph nodes) are
never passed through wholesale.

Rules:
- raw model answers never leave memory (safety gate + explicit eval hook only)
- free-text fields are redacted (secret patterns) or replaced by stable codes;
  tool error messages are NOT persisted — only stable error codes and hashes
- correlation ids are ALWAYS present as fields (values may be null when the
  context does not provide them — never fabricated)
- forged/unknown tool names become ``unknown_tool`` + short digest
- token events are serialized too (schema + ids + redaction) but callers may
  choose not to persist them
- supported final answer text is user-visible product content; secret-pattern
  redaction is applied before token/ChatMessage/run.output/API response
"""
from __future__ import annotations

import hashlib
import re
from typing import Any

# ---------------------------------------------------------------------------
# Stable tool-name mapping (forged names never enter events/logs)
# ---------------------------------------------------------------------------

def _known_tool_names() -> frozenset[str]:
    from .project_tools import PROJECT_AGENT_TOOLS

    return frozenset(
        tool.get("function", {}).get("name", "")
        for tool in PROJECT_AGENT_TOOLS
        if tool.get("function", {}).get("name")
    )


KNOWN_TOOL_NAMES = _known_tool_names()


def sanitize_tool_name(name: str) -> tuple[str, str | None]:
    """Known tool names pass through; anything else becomes the stable code
    ``unknown_tool`` plus a short digest (the original name is never echoed)."""
    if name in KNOWN_TOOL_NAMES:
        return name, None
    digest = hashlib.sha256(str(name).encode("utf-8")).hexdigest()[:12]
    return "unknown_tool", digest


# ---------------------------------------------------------------------------
# Secret-pattern redaction for free text AND supported answer text (§30.2)
# ---------------------------------------------------------------------------

_SK_SECRET_RE = re.compile(r"sk-[A-Za-z0-9_\-]{8,}")

_SECRET_HINT_RE = re.compile(
    r"(?i)(secret|password|token|api[_-]?key)[\s:=]+[A-Za-z0-9_\-\.]{8,}"
)

_SECRET_WORD_RE = re.compile(r"(?i)\bSECRET_[A-Z0-9_]{4,}\b")

_BEARER_RE = re.compile(r"(?i)bearer[\s:=]+[A-Za-z0-9_\-\.]{8,}")


def redact_text(text: Any, limit: int = 1000) -> Any:
    """Redact obvious secret patterns from free text; keep everything else.

    Used for error messages, citation markers/sections and the final answer
    before it reaches token events, ChatMessage, run.output and API responses.
    """
    if not isinstance(text, str):
        return text
    text = _SK_SECRET_RE.sub("[REDACTED]", text)
    text = _SECRET_HINT_RE.sub("[REDACTED]", text)
    text = _SECRET_WORD_RE.sub("[REDACTED]", text)
    text = _BEARER_RE.sub("[REDACTED]", text)
    return text[:limit]


# Backward-compatible name used by earlier Tasks 5.x code/tests.
sanitize_text = redact_text


def error_hash(exc: BaseException) -> str:
    """Stable digest of the exception class + message (message itself is never
    logged/persisted; the digest allows correlation across surfaces)."""
    digest_input = f"{exc.__class__.__name__}:{str(exc)}"
    return hashlib.sha256(digest_input.encode("utf-8")).hexdigest()[:12]


def safe_stack_frames(exc: BaseException, depth: int = 4) -> list[dict[str, Any]]:
    """Safe stack structure — file/line/function only, never locals/values."""
    import traceback

    frames = []
    for frame in traceback.extract_tb(exc.__traceback__ or ())[-depth:]:
        frames.append({
            "file": str(frame.filename)[-160:],
            "line": int(frame.lineno),
            "function": str(frame.name),
        })
    return frames


# ---------------------------------------------------------------------------
# Recursive per-event schemas
# ---------------------------------------------------------------------------

_ID_KEYS = ("project_id", "run_id", "session_id", "request_id")

_CITATION_SCHEMA = {
    "marker": "text",
    "citation_marker_status": "raw",
    "reference_resolved": "bool",
    "reference_resolution_status": "raw",
    "resolution_reason": "text",
    "project_id": "int_or_none",
    "paper_id": "int_or_none",
    "chunk_id": "int_or_none",
    "content_hash": "text",
    "evidence_id": "text",
    "chunk_index": "int_or_none",
    "page_start": "int_or_none",
    "page_end": "int_or_none",
    "section": "text",
    "evidence_type": "raw",
    "claim_support_status": "raw",
    "verified": "bool",
}

_TOOL_ENTRY_SCHEMA = {"tool": "tool", "error": "text"}
_WARNING_SCHEMA = {"tool": "tool", "error": "text", "step": "raw"}
_FAILURE_SCHEMA = {"mode": "raw", "tool": "tool", "error": "text"}

_USAGE_SCHEMA = {
    "prompt_tokens": "int_or_none",
    "completion_tokens": "int_or_none",
    "total_tokens": "int_or_none",
}

_QUALITY_SCALARS = {
    "verdict": "raw",
    "evidence_count": "int_or_none",
    "source_marker_count": "int_or_none",
    "resolved_citation_count": "int_or_none",
    "verified_count": "int_or_none",
    "unverified_count": "int_or_none",
    "answer_mode": "raw",
    "evidence_status": "raw",
    "citation_presence": "raw",
    "retrieval_status": "raw",
    "reference_resolution_status": "raw",
    "citation_binding_status": "raw",
    "claim_support_status": "raw",
    "legacy_unresolved_count": "int_or_none",
    "compare_missing_paper_ids": "int_list",
    "raw_model_answer_chars": "int_or_none",
    "model_cited": "bool",
    "postprocessed_added_markers": "bool",
    "safety_replaced": "bool",
    "action_failed": "bool",
}

_GRAPH_NODE_SCHEMA = {
    "id": "raw", "title": "title", "year": "int_or_none",
    "citation_count": "int_or_none", "cluster": "int_or_none",
    "cluster_label": "title", "seminal": "raw", "is_root": "bool",
    "is_frontier": "bool",
}
_GRAPH_EDGE_SCHEMA = {
    "source": "raw", "target": "raw", "weight": "raw",
    "source_title": "title", "target_title": "title",
}

_EVIDENCE_ITEM_SCHEMA = {
    "paper_id": "int_or_none", "title": "title", "section": "text",
    "page_start": "int_or_none", "page_end": "int_or_none",
    "evidence_id": "text", "chunk_id": "int_or_none",
    "content_hash": "text", "citation": "text", "source_marker": "text",
    "evidence_type": "raw",
}

_SEARCH_PAPER_SCHEMA = {
    "title": "title", "year": "int_or_none", "source": "text",
    "venue": "text", "citation_count": "int_or_none",
}

_ADDED_ITEM_SCHEMA = {
    "title": "title", "paper_id": "int_or_none", "status": "text",
    "created": "bool",
}

_SCHEMAS: dict[str, dict[str, Any]] = {
    "harness_started": {"fields": {
        "session_id": "int_or_none", "run_id": "int_or_none"}},
    "intent_detected": {"fields": {
        "intent": "text", "rationale": "text", "blocked": "bool",
        "planned_tools": "str_list"}},
    "agent_mode": {"fields": {
        "mode": "text", "max_iterations": "int_or_none"}},
    "tool_call": {"fields": {
        "name": "tool", "tool_call_id": "text", "iteration": "int_or_none",
        "status": "text", "model_supplied_project_id": "int_or_none"},
        "computed": {"summary": "tool_label", "arguments": "safe_args"}},
    "tool_result": {"fields": {
        "name": "tool", "status": "text", "count": "int_or_none",
        "nodes": "int_or_none", "edges": "int_or_none",
        "length": "int_or_none", "error": "text",
        "error_message": "fixed_copy", "retryable": "bool",
        "fallback": "text"}},
    "evidence": {"computed": {
        "evidence_count": "count_of_evidence", "evidence": "evidence_items",
        "fallback": "text"}},
    "search_results": {"computed": {
        "count": "count_of_papers", "papers": "search_papers"}},
    "paper_added": {"computed": {
        "count": "count_of_added", "added": "added_items",
        "added_titles": "added_titles"}},
    "graph": {"fields": {
        "nodes": {"kind": "items", "schema": _GRAPH_NODE_SCHEMA},
        "edges": {"kind": "items", "schema": _GRAPH_EDGE_SCHEMA}}},
    "llm_call": {"fields": {
        "phase": "text", "iteration": "int_or_none"}},
    "llm_result": {"fields": {
        "phase": "text", "iteration": "int_or_none",
        "usage": {"kind": "dict", "schema": _USAGE_SCHEMA}},
        # §31.2: real producer measurements only — null when unavailable.
        "computed": {
            "status": "text", "answer_chars": "int_or_none",
            "duration_ms": "int_or_none"}},
    "tool_scope_violation": {"fields": {
        "tool": "tool", "rejected_fields": "str_list",
        "attempted_project_id": "int_or_none"}},
    "quality_check": {
        "fields": _QUALITY_SCALARS,
        "nested": {
            "citations": {"kind": "items", "schema": _CITATION_SCHEMA},
            "tool_errors": {"kind": "items", "schema": _TOOL_ENTRY_SCHEMA},
            "recovered_warnings": {"kind": "items", "schema": _WARNING_SCHEMA},
            "action_failure_mode": {"kind": "dict", "schema": _FAILURE_SCHEMA},
        }},
    "done": {"fields": {
        "session_id": "int_or_none", "run_id": "int_or_none"}},
    # §31.1: error surfaces carry a stable code + fixed user copy ONLY — the
    # raw exception message is never serialized (no regex-based redaction).
    "error": {"fields": {"error": "text", "error_hash": "text"},
              "computed": {"message": "fixed_error_copy"}},
    "token": {"fields": {"text": "answer_text"}},
    # workflow / ingestion producers (same serializer, no question/query/body)
    "workflow_queued": {"fields": {"celery_task_id": "text"}},
    "workflow_started": {"fields": {
        "run_id": "int_or_none", "project_id": "int_or_none"}},
    "workflow_completed": {"fields": {
        "run_id": "int_or_none", "report_id": "int_or_none",
        "status": "text"}},
    "workflow_failed": {"fields": {
        "message": "error_code", "error_hash": "text"}},
    # Task 4.3/4.5 (Batch C): waiting + committed RAG — allowlisted IDs,
    # statuses, counts, phase and stable codes only.
    "workflow_waiting": {"fields": {
        "phase": "text", "status": "text"}},
    # P2-C-R2-03 (Batch C): resume marker — IDs/status/phase only.
    "workflow_resumed": {"fields": {
        "phase": "text", "status": "text", "run_id": "int_or_none"}},
    "rag_committed": {"fields": {
        "phase": "text", "node": "text", "status": "text",
        "evidence_count": "int_or_none"}},
    "workflow_node": {"fields": {
        "node": "text", "status": "text", "paper_count": "int_or_none",
        "added_count": "int_or_none", "job_count": "int_or_none",
        "passed": "bool", "evidence_count": "int_or_none", "risk": "text",
        "recommendation": "text", "section_chars": "int_or_none",
        "report_id": "int_or_none"}},
    "hybrid_retrieval": {"fields": {
        "node": "text", "status": "text", "evidence_count": "int_or_none",
        "fallback": "text"}},
    "ingestion_started": {"fields": {
        "job_id": "int_or_none", "paper_id": "int_or_none",
        "attempt_count": "int_or_none"}},
    "ingestion_progress": {"fields": {
        "job_id": "int_or_none", "status": "text"}},
    "ingestion_retry": {"fields": {
        "job_id": "int_or_none", "attempt_count": "int_or_none",
        "error_code": "error_code", "error_hash": "text",
        "retryable": "bool"}},
    "ingestion_completed": {"fields": {
        "job_id": "int_or_none", "paper_id": "int_or_none",
        "chunk_count": "int_or_none", "index_version_id": "int_or_none",
        "reused": "bool", "fulltext_ready": "bool",
        "duration_ms": "int_or_none"}},
    "ingestion_failed": {"fields": {
        "job_id": "int_or_none", "message": "error_code",
        "error_code": "error_code", "error_hash": "text",
        "retryable": "bool", "duration_ms": "int_or_none"}},
    "ingestion_upload_queued": {"fields": {
        "job_id": "int_or_none", "paper_id": "int_or_none",
        "deduplicated": "bool", "reused": "bool",
        "fulltext_ready": "bool"}},
    "ingestion_url_queued": {"fields": {
        "job_id": "int_or_none", "paper_id": "int_or_none",
        "source_hash": "text", "deduplicated": "bool",
        "reused": "bool", "fulltext_ready": "bool"}},
    "ingestion_job_retried": {"fields": {
        "job_id": "int_or_none", "paper_id": "int_or_none",
        "retryable": "bool", "fulltext_ready": "bool"}},
    "ingestion_agent_queued": {"fields": {
        "job_id": "int_or_none", "paper_id": "int_or_none",
        "reason": "text"}},
    "ingestion_agent_skipped": {"fields": {
        "paper_id": "int_or_none", "reason": "text"}},
}

# Event types that are never emitted/persisted (raw model answers).
_FORBIDDEN_EVENTS = frozenset({"final_answer_raw"})


# ---------------------------------------------------------------------------
# Value transforms
# ---------------------------------------------------------------------------

def _as_int(value: Any) -> int | None:
    try:
        if value is None or isinstance(value, bool):
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def _apply_value(kind: str, value: Any, payload: dict[str, Any]) -> Any:
    if kind == "raw":
        return value
    if kind == "text":
        return redact_text(value)
    if kind == "title":
        # Paper/source titles are controlled UI product fields (§30.4) — they
        # are truncated but not secret-redacted (they are user-visible library
        # data, not prompts/payloads).
        return str(value)[:200] if value is not None else None
    if kind == "int_or_none":
        return _as_int(value)
    if kind == "bool":
        return bool(value)
    if kind == "str_list":
        if not isinstance(value, list):
            return []
        return [redact_text(str(item))[:200] for item in value if item is not None]
    if kind == "int_list":
        if not isinstance(value, list):
            return []
        return [v for v in (_as_int(i) for i in value) if v is not None]
    if kind == "error_message":
        return redact_text(value)
    if kind == "error_code":
        return str(value)[:64]
    if kind == "fixed_copy":
        return "工具执行失败，请稍后重试。"
    if kind == "fixed_error_copy":
        return "服务暂时不可用，请稍后重试。"
    if kind == "answer_text":
        return redact_text(value)
    if kind == "tool_label":
        tool = payload.get("name")
        tool, _digest = sanitize_tool_name(tool)
        return tool
    if kind == "safe_args":
        # Controlled argument view model: numeric knobs only — never query/
        # question/papers/reason/free-text arguments (§30.4).
        safe: dict[str, Any] = {}
        if isinstance(value, dict):
            for key, val in value.items():
                if isinstance(val, bool) or (
                        isinstance(val, int) and not isinstance(val, bool)):
                    safe[key] = val
        return safe
    if kind == "count_of_evidence":
        items = payload.get("evidence") or []
        return len(items) if isinstance(items, list) else 0
    if kind == "evidence_items":
        items = payload.get("evidence") or []
        if not isinstance(items, list):
            return []
        out = []
        for item in items:
            if not isinstance(item, dict):
                continue
            entry = {}
            for key, kkind in _EVIDENCE_ITEM_SCHEMA.items():
                if key in item:
                    entry[key] = _apply_value(kkind, item[key], {})
            out.append(entry)
        return out
    if kind == "count_of_papers":
        items = payload.get("papers") or payload.get("results") or []
        return len(items) if isinstance(items, list) else 0
    if kind == "search_papers":
        items = payload.get("papers") or payload.get("results") or []
        if not isinstance(items, list):
            return []
        out = []
        for item in items:
            if not isinstance(item, dict):
                continue
            entry = {}
            for key, kkind in _SEARCH_PAPER_SCHEMA.items():
                if key in item:
                    entry[key] = _apply_value(kkind, item[key], {})
            out.append(entry)
        return out
    if kind == "count_of_added":
        items = payload.get("added") or []
        return len(items) if isinstance(items, list) else 0
    if kind == "added_items":
        items = payload.get("added") or []
        if not isinstance(items, list):
            return []
        out = []
        for item in items:
            if not isinstance(item, dict):
                continue
            entry = {}
            for key, kkind in _ADDED_ITEM_SCHEMA.items():
                if key in item:
                    entry[key] = _apply_value(kkind, item[key], {})
            out.append(entry)
        return out
    if kind == "added_titles":
        items = payload.get("added") or []
        if not isinstance(items, list):
            return []
        return [str(item.get("title") or "")[:200]
                for item in items if isinstance(item, dict)]
    return None


def _apply_schema(schema: dict[str, Any] | str, payload: dict[str, Any],
                  _depth: int = 0) -> dict[str, Any] | list[dict[str, Any]] | None:
    if isinstance(schema, str):
        return _apply_value(schema, payload, payload)
    if _depth > 8:
        return None
    # A bare field-map (e.g. a nested item schema) is treated as ``fields``.
    if not any(k in schema for k in ("fields", "nested", "computed")):
        schema = {"fields": schema}
    out: dict[str, Any] = {}
    for key, kkind in schema.get("fields", {}).items():
        if key not in payload:
            continue
        if isinstance(kkind, dict):
            value = payload[key]
            if kkind.get("kind") == "items" and isinstance(value, list):
                items = []
                for item in value:
                    if not isinstance(item, dict):
                        continue
                    sub = _apply_schema(kkind["schema"], item, _depth + 1)
                    if isinstance(sub, dict):
                        items.append(sub)
                out[key] = items
            elif kkind.get("kind") == "dict" and isinstance(value, dict):
                sub = _apply_schema(kkind["schema"], value, _depth + 1)
                if isinstance(sub, dict):
                    out[key] = sub
        elif kkind == "tool":
            tool, digest = sanitize_tool_name(payload[key])
            out[key] = tool
            if digest:
                out["tool_hash"] = digest
        else:
            out[key] = _apply_value(kkind, payload[key], payload)
    for key, kkind in schema.get("computed", {}).items():
        out[key] = _apply_value(kkind, payload.get(key), payload)
    for key, nested in schema.get("nested", {}).items():
        value = payload.get(key)
        if nested.get("kind") == "items" and isinstance(value, list):
            items = []
            for item in value:
                if not isinstance(item, dict):
                    continue
                sub = _apply_schema(nested["schema"], item, _depth + 1)
                if isinstance(sub, dict):
                    items.append(sub)
            out[key] = items
        elif nested.get("kind") == "dict" and isinstance(value, dict):
            sub = _apply_schema(nested["schema"], value, _depth + 1)
            if isinstance(sub, dict):
                out[key] = sub
    return out


# ---------------------------------------------------------------------------
# Public serializer entry
# ---------------------------------------------------------------------------

def sanitize_event(
    event_type: str,
    payload: dict[str, Any],
    ids: dict[str, Any] | None = None,
    context=None,
) -> dict[str, Any]:
    """Return the serialized, schema-limited payload for one event.

    ``ids`` (or the legacy ``context`` ToolExecutionContext) supplies the
    server-bound correlation ids; the four id FIELDS are always present (values
    may be None when the context does not provide them — never fabricated).
    """
    if event_type in _FORBIDDEN_EVENTS:
        return {}

    schema = _SCHEMAS.get(event_type)
    if schema is None or not isinstance(payload, dict):
        safe: dict[str, Any] = {}
    else:
        result = _apply_schema(schema, payload)
        safe = result if isinstance(result, dict) else {}

    if ids is None and context is not None:
        ids = {
            "project_id": context.project_id,
            "run_id": context.run_id,
            "session_id": context.session_id,
            "request_id": context.request_id or None,
        }
    ids = ids or {}
    for key in _ID_KEYS:
        safe[key] = ids.get(key)
    return safe


def event_schemas() -> dict[str, Any]:
    """Machine-readable schema dump (used by reports/audit tooling)."""
    def _dump(schema: dict[str, Any]) -> dict[str, Any]:
        return {k: v for k, v in schema.items()}

    return {name: _dump(schema) for name, schema in sorted(_SCHEMAS.items())}
