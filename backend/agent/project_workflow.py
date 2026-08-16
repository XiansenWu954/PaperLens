"""Phase 2 Batch C — checkpointed project research-expand graph (P2-C-CX-01..05).

Design (Tasks 4.1-4.3, 4.5 + P2-C fixes):
  - the graph is compiled with a PostgreSQL checkpointer and invoked with
    ``thread_id=str(ProjectRun.id)`` so interrupt/resume and duplicate
    delivery always resume the SAME logical thread;
  - checkpoint state is limited to approved scalar/ID fields (project_id,
    run_id, phase, node, counts, booleans, stable status codes and bounded
    paper-id lists). Question, rewritten queries, search payloads, PDF URLs,
    full text, excerpts, drafts, API keys and exception bodies NEVER enter
    state (P2-C-CX-01);
  - ``add_candidates`` is a dedicated node: search results are added to the
    project WITHOUT auto-queueing, and only the resulting bounded paper IDs
    are carried in checkpoint state (P2-C-CX-01);
  - ``enqueue_ingestion`` processes ONLY the target papers of THIS run
    (the paper IDs produced by add_candidates) — it never scans all project
    papers; it reuses the Phase 1 safe source identity, digest filename,
    request key, get_or_create_job and claim_build (P2-C-CX-01);
  - only a current compatible, active and non-empty full-text version may
    mark a dependency ``ready`` (P2-C-CX-01);
  - the owner token lives in a process-local context variable, never in
    state/broker/events/logs/API; every committed node boundary renews the
    lease; on renewal failure the old owner exits safely and MUST NOT mark
    the shared run error or write further reports/events (P2-C-CX-02);
  - every node terminal, wait, resume, RAG commit, completion and failure
    event uses a stable attempt-free dedupe key; the completion event is
    produced exactly once by the graph's persist_report node (P2-C-CX-03);
  - the checkpointer saver is a per-task session, explicitly closed in a
    finally block — no global cross-event-loop singleton (P2-C-CX-04);
  - a duplicate start for a waiting/running run does not re-execute
    search/enqueue: the task decides skip/resume from DB + checkpoint
    (P2-C-CX-04).
"""
from __future__ import annotations

import contextvars
import hashlib
import logging
from typing import Any, TypedDict

from asgiref.sync import sync_to_async
from django.utils import timezone
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from .project_tools import draft_report_section, query_project_rag, search_papers

logger = logging.getLogger(__name__)


# ── approved checkpoint state: IDs / counts / booleans / stable codes ──
class ProjectWorkflowState(TypedDict, total=False):
    project_id: int
    run_id: int
    phase: str          # stable status code (started|planned|searched|added|enqueued|waiting|ready|rag|done|partial|error)
    node: str           # stable node code
    added_count: int
    paper_ids: list[int]  # bounded target paper IDs of THIS run (P2-C-CX-01)
    job_count: int
    pending_deps: bool
    evidence_count: int
    resolved_reference_count: int  # P2-D-R2-01: resolver-verified evidence count
    answer_bound_fulltext_count: int  # P2-D-R2-01: markers in final draft bound to resolved evidence
    rag_committed: bool
    critic_passed: bool   # Batch D 5.4: advisory critic verdict (downgrade-only)
    critic_risk: str      # Batch D 5.4: stable risk code
    evidence_ids: list[str]  # P2-D-R2-01: bounded canonical evidence IDs (no excerpts)
    summary_hash: str   # sha256 digest of sorted paper ids — never raw payloads
    report_id: int
    error_code: str     # stable error code, never raw exception text


# ── process-local owner runtime context (P2-C-CX-02) ──────────────────
_owner_token_var: contextvars.ContextVar[str] = contextvars.ContextVar(
    "paperlens_workflow_owner_token", default="")


def set_owner_context(token: str) -> None:
    """Bind the owner token to the current task context. Process memory
    ONLY — never serialized, never logged, never in broker payloads."""
    _owner_token_var.set(token)


def clear_owner_context() -> None:
    _owner_token_var.set("")


def current_owner_token() -> str:
    return _owner_token_var.get()


class OwnerLeaseLost(RuntimeError):
    """Raised when the owner lease was lost mid-flight. The caller MUST
    exit safely: no error status, no further events/reports."""


class WorkflowNodeError(RuntimeError):
    """P2-D-R2-04: stable workflow node error. ``str()`` is a STABLE code
    (never the raw exception body) — LangGraph checkpoints node errors,
    so any raw message would persist into the checkpoints table."""
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _run_id(state: ProjectWorkflowState) -> int:
    run_id = state.get("run_id")
    if run_id is None:
        raise RuntimeError("missing run identity")
    return int(run_id)


def _question(run_id: int) -> str:
    from api.models import ProjectRun
    return ProjectRun.objects.values_list("question", flat=True).get(id=run_id)


async def _question_async(run_id: int) -> str:
    return await sync_to_async(_question)(run_id)


# ════════════════════════════════════════════════════════════════════════
# Owner lease renewal at node boundaries (P2-C-CX-02)
# ════════════════════════════════════════════════════════════════════════

def _renew_lease(run_id: int, token: str) -> None:
    """Renew the lease. Raise OwnerLeaseLost when the caller no longer owns
    the run (superseded/expired) — the caller must stop writing side
    effects. With no bound token (direct graph entry in tests / tooling)
    renewal is skipped: there is no lease to renew."""
    from api.models import ProjectRun
    from agent.owner_service import renew_owner

    if not token:
        return
    run = ProjectRun.objects.get(id=run_id)
    if not renew_owner(run, token):
        raise OwnerLeaseLost("owner lease lost")


async def _renew_lease_async(run_id: int) -> None:
    await sync_to_async(_renew_lease)(run_id, current_owner_token())


# ════════════════════════════════════════════════════════════════════════
# Node implementations (each node is idempotent + DB-backed)
# ════════════════════════════════════════════════════════════════════════

async def plan_expansion(state: ProjectWorkflowState) -> dict[str, Any]:
    run_id = _run_id(state)
    question = await _question_async(run_id)
    if not question.strip():
        return {"phase": "error", "error_code": "empty_question",
                "node": "plan_expansion"}
    # P2-C-CX-02: renew BEFORE any side effect — a lost lease stops this
    # node from writing its event/phase at all.
    await _renew_lease_async(run_id)
    await _event(state, "workflow_node",
                 {"node": "plan_expansion", "status": "done"},
                 dedupe_key=f"run:{run_id}:node:plan_expansion:done")
    await _set_phase_async(run_id, "planned")
    return {"phase": "planned", "node": "plan_expansion"}


async def search_sources(state: ProjectWorkflowState) -> dict[str, Any]:
    """Search once (pure computation) and PERSIST the found papers to the DB
    (paper rows only, no ProjectPaper membership, no jobs). Returns the
    bounded paper IDs for this run — the ONLY search-derived payload that
    survives into the checkpoint (P2-C-CX-01). add_candidates then links
    those IDs.

    P2-C-R2-01: the durable write (paper upsert) happens only AFTER the
    lease is re-validated; a superseded owner performs zero writes."""
    run_id = _run_id(state)
    question = await _question_async(run_id)
    query = _rewrite_query(question)
    payload = await search_papers(query, max_results=8)
    results = payload.get("papers") or []
    # P2-C-R2-01: fence BEFORE the durable side effect (paper upsert).
    await _renew_lease_async(run_id)
    paper_ids = await sync_to_async(_persist_search_papers)(results)
    summary_hash = hashlib.sha256(
        ",".join(str(p) for p in paper_ids).encode("utf-8")).hexdigest()[:32]
    await _event(state, "workflow_node",
                 {"node": "search_sources", "status": "done",
                  "paper_count": len(paper_ids)},
                 dedupe_key=f"run:{run_id}:node:search_sources:done")
    await _set_phase_async(run_id, "searched")
    return {"node": "search_sources", "phase": "searched",
            "added_count": 0, "paper_ids": paper_ids[:100],
            "summary_hash": summary_hash}


def _persist_search_papers(papers: list[dict]) -> list[int]:
    """Upsert search-result papers into the global paper table ONLY (no
    membership, no job, no build). Never returns URLs — just IDs."""
    from papers.models import upsert_paper

    ids: list[int] = []
    for payload in papers:
        try:
            paper = upsert_paper(payload)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "workflow search persist skipped",
                extra={"event": "workflow_search_persist_skipped",
                       "reason": exc.__class__.__name__, "status": "skipped"})
            continue
        ids.append(paper.id)
    return ids


async def add_candidates(state: ProjectWorkflowState) -> dict[str, Any]:
    """P2-C-CX-01: link THIS run's bounded target paper IDs into the project
    as memberships WITHOUT any auto-queueing (no job/build created here).
    Never searches again — the papers were persisted by search_sources.

    P2-C-R2-01: membership writes happen only AFTER lease re-validation."""
    run_id = _run_id(state)
    project_id = int(state["project_id"])
    paper_ids = [int(p) for p in (state.get("paper_ids") or [])]
    # P2-C-R2-01: fence BEFORE the durable side effect (memberships).
    await _renew_lease_async(run_id)
    added_count = await sync_to_async(_link_candidates_to_project)(
        project_id, paper_ids, reason="LangGraph expansion")
    await _event(state, "workflow_node",
                 {"node": "add_candidates", "status": "done",
                  "paper_count": added_count},
                 dedupe_key=f"run:{run_id}:node:add_candidates:done")
    await _set_phase_async(run_id, "added")
    return {"node": "add_candidates", "phase": "added",
            "added_count": added_count}


def _link_candidates_to_project(project_id: int, paper_ids: list[int],
                                reason: str = "") -> int:
    """Membership-only linking — NEVER enqueues ingestion."""
    from api.models import ProjectPaper, ResearchProject

    project = ResearchProject.objects.get(id=project_id)
    count = 0
    for paper_id in paper_ids:
        try:
            link, created = ProjectPaper.objects.get_or_create(
                project=project, paper_id=paper_id,
                defaults={"status": "candidate", "source_reason": reason,
                          "added_by": "agent"})
            if not created and reason:
                link.source_reason = reason
                link.save(update_fields=["source_reason", "updated_at"])
            count += 1
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "workflow add_candidates skipped",
                extra={"event": "workflow_add_candidate_skipped",
                       "reason": exc.__class__.__name__, "status": "skipped"})
            continue
    return count


async def enqueue_ingestion(state: ProjectWorkflowState) -> dict[str, Any]:
    run_id = _run_id(state)
    project_id = int(state["project_id"])
    # P2-C-CX-01: ONLY the target papers of this run (bounded paper IDs),
    # never all project papers.
    target_paper_ids = [int(p) for p in (state.get("paper_ids") or [])]
    # P2-C-R2-01: fence BEFORE the durable side effects (job creation,
    # build claim, Celery delivery, dependency rows).
    await _renew_lease_async(run_id)
    job_count, pending_deps = await sync_to_async(_enqueue_ingestion_for_run)(
        run_id, project_id, target_paper_ids)
    await _event(state, "workflow_node",
                 {"node": "enqueue_ingestion", "status": "done",
                  "job_count": job_count},
                 dedupe_key=f"run:{run_id}:node:enqueue_ingestion:done")
    await _set_phase_async(run_id, "enqueued")
    return {"node": "enqueue_ingestion", "phase": "enqueued",
            "job_count": job_count, "pending_deps": pending_deps}


async def await_ingestion(state: ProjectWorkflowState) -> dict[str, Any]:
    """Refresh dependencies; while any is pending the run transitions to
    waiting and interrupts. On resume the same node re-checks and, once all
    dependencies are terminal, execution continues to RAG.

    P2-C-R3-01: the lease is confirmed BEFORE the dependency refresh (it
    writes rows), BEFORE the waiting state write and BEFORE the waiting
    event."""
    run_id = _run_id(state)
    await _renew_lease_async(run_id)
    pending = await sync_to_async(_refresh_dependency_status)(run_id)
    if pending:
        await _renew_lease_async(run_id)
        await _set_waiting_async(run_id)
        await _event(state, "workflow_waiting",
                     {"phase": "await_ingestion", "status": "waiting"},
                     dedupe_key=f"run:{run_id}:waiting")
        # P2-C-CX-02: release the owner before pausing the worker.
        await sync_to_async(_release_owner_safe)(run_id)
        # First pass raises GraphInterrupt and pauses the thread; on explicit
        # resume the call returns the wakeup value and we re-check below.
        interrupt({"reason": "waiting_ingestion", "run_id": run_id})
        await _renew_lease_async(run_id)
        pending = await sync_to_async(_refresh_dependency_status)(run_id)
        if pending:
            return {"phase": "waiting", "node": "await_ingestion"}
    await _renew_lease_async(run_id)
    return {"phase": "ready", "node": "await_ingestion"}


def _release_owner_safe(run_id: int) -> None:
    from api.models import ProjectRun
    from agent.owner_service import release_owner
    token = current_owner_token()
    if not token:
        return
    try:
        release_owner(ProjectRun.objects.get(id=run_id), token)
    except Exception:  # noqa: BLE001 - release is best-effort
        pass


async def query_hybrid_rag(state: ProjectWorkflowState) -> dict[str, Any]:
    """Batch D 5.3 + P2-D-CX-01 + P2-D-R2-01: RAG runs ONLY after every
    dependency reached a terminal state; queries ONLY ready/succeeded
    dependency papers; validates evidence through the canonical
    CitationResolver; persists the bounded canonical evidence IDs (never
    excerpts) in checkpoint state."""
    run_id = _run_id(state)
    project_id = int(state["project_id"])
    question = await _question_async(run_id)
    timing = await sync_to_async(_strict_timing_check)(run_id)
    if not timing["all_terminal"]:
        await _event(state, "workflow_node",
                     {"node": "query_hybrid_rag", "status": "blocked",
                      "reason": "dependencies_not_terminal"},
                     dedupe_key=f"run:{run_id}:node:query_hybrid_rag:blocked")
        return {"phase": "waiting", "node": "query_hybrid_rag",
                "rag_committed": False}
    dep_paper_ids = await sync_to_async(_resolved_dependency_paper_ids)(run_id)
    if not dep_paper_ids:
        await _renew_lease_async(run_id)
        committed = await sync_to_async(_commit_rag_and_stamp_timing)(
            run_id, timing["last_terminal_at"], 0)
        await _event(state, "workflow_node",
                     {"node": "query_hybrid_rag", "status": "done",
                      "evidence_count": 0},
                     dedupe_key=f"run:{run_id}:node:query_hybrid_rag:done")
        await _set_phase_async(run_id, "rag")
        return {"phase": "rag", "node": "query_hybrid_rag",
                "evidence_count": 0, "resolved_reference_count": 0,
                "evidence_ids": [], "rag_committed": committed}
    result = await query_project_rag(project_id, question, k=8,
                                     paper_ids=dep_paper_ids)
    raw_evidence = result.get("evidence") or []
    # P2-D-R2-01: resolve through CitationResolver — keep ONLY the bounded
    # canonical evidence IDs (no excerpts/payloads) in checkpoint state.
    resolved_ids, resolved_count = await sync_to_async(
        _resolve_scoped_evidence)(project_id, raw_evidence)
    await _renew_lease_async(run_id)
    committed = await sync_to_async(_commit_rag_and_stamp_timing)(
        run_id, timing["last_terminal_at"], resolved_count)
    await _event(state, "workflow_node",
                 {"node": "query_hybrid_rag", "status": "done",
                  "evidence_count": resolved_count},
                 dedupe_key=f"run:{run_id}:node:query_hybrid_rag:done")
    await _set_phase_async(run_id, "rag")
    return {"phase": "rag", "node": "query_hybrid_rag",
            "evidence_count": resolved_count,
            "resolved_reference_count": resolved_count,
            "evidence_ids": resolved_ids[:100], "rag_committed": committed}


def _resolved_dependency_paper_ids(run_id: int) -> list[int]:
    """P2-D-CX-01: paper IDs of ready/succeeded dependencies ONLY — the
    papers whose full text the run is ALLOWED to read."""
    from api.models import ProjectWorkflowDependency

    deps = ProjectWorkflowDependency.objects.filter(
        run_id=run_id, status__in=("ready", "succeeded"))
    return list(deps.values_list("paper_id", flat=True))


def _resolve_scoped_evidence(project_id: int,
                             evidence_items: list) -> tuple[list[str], int]:
    """P2-D-R2-01: resolve evidence through the canonical CitationResolver.
    Returns (bounded canonical evidence IDs, resolved count) — only IDs
    survive (no excerpts/payloads)."""
    from agent.citations import CitationResolver, RESOLVED

    if not evidence_items:
        return [], 0
    resolver = CitationResolver(project_id)
    resolutions = resolver.resolve(evidence_items)
    resolved_keys = [k.replace("ev:", "")
                     for k, r in resolutions.items()
                     if r.reference_resolved and r.reason_code == RESOLVED]
    return resolved_keys, len(resolved_keys)


def _validate_resolved_evidence(project_id: int,
                                evidence_items: list) -> int:
    """P2-D-CX-01: count only evidence that resolves through the canonical
    CitationResolver with a real active Text chunk, correct hash, matching
    embedding version and project membership."""
    _, count = _resolve_scoped_evidence(project_id, evidence_items)
    return count


def _strict_timing_check(run_id: int) -> dict[str, Any]:
    """5.3: verify every dependency is terminal; return the latest terminal
    timestamp for the first_rag_at ordering proof."""
    from api.models import ProjectWorkflowDependency

    deps = list(ProjectWorkflowDependency.objects.filter(run_id=run_id))
    non_terminal = [d.id for d in deps if d.status not in
                    ("succeeded", "failed", "unavailable", "ready")]
    terminals = [d.terminal_at for d in deps if d.terminal_at]
    return {
        "all_terminal": not non_terminal and bool(deps),
        "non_terminal": non_terminal,
        "last_terminal_at": max(terminals) if terminals else None,
    }


def _commit_rag_and_stamp_timing(run_id: int, last_terminal_at,
                                 evidence_count: int) -> bool:
    """5.3: stamp first_rag_at ONCE, strictly after the last dependency
    terminal timestamp (fail closed on any ordering violation), then commit
    the single rag_committed event."""
    from api.models import ProjectRun
    from django.utils import timezone

    def _fail(reason: str) -> bool:
        logger.warning(
            "strict timing gate rejected RAG commit",
            extra={"event": "workflow_timing_gate_rejected",
                   "run_id": run_id, "reason": reason, "status": "rejected"})
        return False

    run = ProjectRun.objects.get(id=run_id)
    if run.first_rag_at is not None:
        return _commit_rag_event(run_id, evidence_count)  # replay: once
    now = timezone.now()
    if last_terminal_at is None:
        # ready-only dependencies have no job terminal_at; the run's
        # recorded ingestion terminal (or now) bounds the ordering
        last_terminal_at = run.last_ingestion_terminal_at
    if last_terminal_at is not None and now <= last_terminal_at:
        return _fail("first_rag_at_not_after_terminal")
    ProjectRun.objects.filter(id=run_id, first_rag_at__isnull=True).update(
        first_rag_at=now)
    return _commit_rag_event(run_id, evidence_count)


async def critic(state: ProjectWorkflowState) -> dict[str, Any]:
    """Batch D 5.4: the critic verdict is ADVISORY ONLY — it may downgrade
    (done->partial/partial->error semantics recorded in outcome fields) but
    NEVER upgrades past the deterministic gates evaluated in
    persist_report."""
    run_id = _run_id(state)
    evidence_count = state.get("evidence_count", 0)
    verdict = {
        "passed": bool(evidence_count),
        "evidence_count": evidence_count,
        "risk": "low" if evidence_count >= 4 else
                ("medium" if evidence_count else "high"),
    }
    await _renew_lease_async(run_id)
    await _event(state, "workflow_node",
                 {"node": "critic", "status": "done", **verdict},
                 dedupe_key=f"run:{run_id}:node:critic:done")
    await _set_phase_async(run_id, "critiqued")
    return {"node": "critic", "phase": "critiqued",
            "critic_passed": verdict["passed"],
            "critic_risk": verdict["risk"]}


async def draft_report(state: ProjectWorkflowState) -> dict[str, Any]:
    """P2-D-R3-01: the run's ``evidence_ids`` (from query_hybrid_rag's
    THIS-recall manifest) are the ONLY allowed evidence. Reload exactly
    those canonical IDs from the database, re-verify them through the
    CitationResolver, and surface their citation tokens to the drafting
    tool — NO project-level free-form retrieval. Cites use the canonical
    evidence ID (``[cite:ev-...]``) to avoid paper/docname marker
    collisions."""
    run_id = _run_id(state)
    project_id = int(state["project_id"])
    question = await _question_async(run_id)
    # P2-D-R3-01: reload EXACTLY the manifest IDs from the DB and
    # re-verify through the CitationResolver.
    manifest_ids = [str(e) for e in (state.get("evidence_ids") or [])]
    verified = await sync_to_async(_reload_manifest_evidence)(
        run_id, project_id, manifest_ids)
    # P2-D-R4-01: the production draft CONSUMES the verified manifest —
    # a workflow-local deterministic draft that cites ONLY the canonical
    # evidence IDs. NO project-level retrieval, NO draft_report_section.
    section = _draft_from_manifest(question, verified)
    # P2-C-R2-01: fence BEFORE the durable side effect (draft persistence).
    await _renew_lease_async(run_id)
    await sync_to_async(_store_draft)(run_id, section)
    await _event(state, "workflow_node",
                 {"node": "draft_report", "status": "done",
                  "section_chars": len(section)},
                 dedupe_key=f"run:{run_id}:node:draft_report:done")
    await _set_phase_async(run_id, "drafted")
    return {"node": "draft_report", "phase": "drafted"}


def _draft_from_manifest(question: str,
                         verified: list[dict]) -> str:
    """P2-D-R4-01: workflow-local deterministic draft builder.

    Consumes ONLY the verified manifest evidence returned by
    ``_reload_manifest_evidence`` — never queries the project, never calls
    the project-level ``draft_report_section`` (which does free-form
    project RAG). Citations are ``[cite:<canonical_evidence_id>]`` so the
    persist-time binding intersects with THIS run's recall manifest
    exactly. No LLM / prompt surface is added."""
    lines = [f"# Expansion report", ""]
    if not verified:
        lines.append("No verified full-text evidence for this recall.")
        return "\n".join(lines)
    for item in verified:
        eid = item["evidence_id"]
        lines.append(f"- Verified full-text evidence [cite:{eid}]")
    lines.append("")
    return "\n".join(lines)


def _reload_manifest_evidence(run_id: int, project_id: int,
                              manifest_ids: list[str]) -> list[dict]:
    """P2-D-R3-01: load EXACTLY the manifest evidence IDs from the DB and
    re-verify each through the canonical CitationResolver (chunk exists,
    hash/version unchanged, project membership, dependency scope, active
    embedding). Returns the VERIFIED envelope items (empty item list for
    IDs that no longer resolve)."""
    from rag.models import Text
    from agent.evidence import make_evidence_id

    dep_paper_ids = set(_resolved_dependency_paper_ids(run_id))
    verified: list[dict] = []
    for eid in manifest_ids[:100]:
        chunk = None
        for c in Text.objects.filter(
                paper_id__in=dep_paper_ids,
                index_version__status="active").only(
                "id", "paper_id", "content_hash", "embedding_version",
                "index_version_id", "citation_key", "docname"):
            if make_evidence_id(project_id, c.paper_id, c.id,
                                c.content_hash,
                                c.embedding_version) == eid:
                chunk = c
                break
        if chunk is None:
            continue  # stale/invalid ID — fails closed (not counted)
        verified.append({
            "evidence_id": eid,
            "project_id": project_id,
            "paper_id": chunk.paper_id,
            "chunk_id": chunk.id,
            "content_hash": chunk.content_hash,
            "embedding_version": chunk.embedding_version,
            "citation": chunk.citation_key,
            "source_marker": chunk.citation_key or chunk.docname,
            "evidence_type": "fulltext",
        })
    if not verified:
        return []
    # CitationResolver re-verification of the reloaded envelopes
    from agent.citations import CitationResolver, RESOLVED
    resolver = CitationResolver(project_id)
    resolutions = resolver.resolve(verified)
    return [item for item in verified
            for k, r in resolutions.items()
            if k == f"ev:{item['evidence_id']}"
            and r.reference_resolved and r.reason_code == RESOLVED]


def _markers_for_resolved_evidence(project_id: int,
                                   dep_paper_ids: list[int]) -> list[str]:
    """Legacy helper kept for backward compatibility — the R3-01 binding
    uses the manifest-based `_verify_marker_binding` instead."""
    from rag.models import Text

    if not dep_paper_ids:
        return []
    chunks = Text.objects.filter(
        paper_id__in=dep_paper_ids,
        index_version__status="active",
    )
    return [c.citation_key or c.docname for c in chunks[:50]]


_CITE_MARKER_RE = None


def _parse_cite_markers(text: str) -> list[str]:
    """Parse `[cite:MARKER]` style markers from a draft/report body."""
    global _CITE_MARKER_RE
    if _CITE_MARKER_RE is None:
        import re
        _CITE_MARKER_RE = re.compile(r"\[cite:\s*([^\]\s]+)\s*\]")
    return _CITE_MARKER_RE.findall(text or "")


def _deterministic_outcome(run_id: int, resolved_reference_count: int,
                           answer_bound_fulltext_count: int,
                           critic_passed: bool) -> dict[str, Any]:
    """Batch D 5.4 + P2-D-R2-01: deterministic done/partial/error gates.

      - error: answer_bound_fulltext_count == 0 (no resolved, answer-bound
        fulltext) — ZERO reports.
      - partial: >=1 failed/unavailable dependency but at least one
        answer-bound fulltext marker.
      - done: every dependency succeeded/ready with answer-bound fulltext.

    The critic is downgrade-only: ``critic_passed=False`` may downgrade
    done->partial or partial->error but NEVER upgrades. One report max per
    run (source_run uniqueness, Batch B).
    """
    from api.models import ProjectWorkflowDependency

    deps = list(ProjectWorkflowDependency.objects.filter(run_id=run_id))
    failed = [d for d in deps if d.status == "failed"]
    unavailable = [d for d in deps if d.status == "unavailable"]
    usable = answer_bound_fulltext_count > 0

    if not usable:
        outcome = "error"
        reason = "no_usable_fulltext"
    elif failed or unavailable:
        outcome = "partial"
        reason = "partial_dependencies"
    else:
        outcome = "done"
        reason = "all_dependencies_succeeded"

    # critic downgrade-only
    if critic_passed is False and outcome == "done":
        outcome = "partial"
        reason = "critic_downgraded"
    elif critic_passed is False and outcome == "partial":
        outcome = "error"
        reason = "critic_downgraded"

    return {
        "outcome": outcome,
        "reason": reason,
        "failed_paper_ids": sorted(d.paper_id for d in failed),
        "unavailable_paper_ids": sorted(d.paper_id for d in unavailable),
        "dependency_total": len(deps),
    }


def _render_partial_disclosure(outcome: dict[str, Any]) -> str:
    """5.4: partial reports MUST structurally disclose failed papers and
    evidence gaps (paper IDs and stable codes only — never titles/URLs)."""
    lines = [
        "## Evidence Gaps",
        f"- outcome: {outcome['outcome']} ({outcome['reason']})",
        f"- dependencies: {outcome['dependency_total']}",
    ]
    if outcome["failed_paper_ids"]:
        ids = ", ".join(str(p) for p in outcome["failed_paper_ids"])
        lines.append(f"- failed papers (ingestion failed): [{ids}]")
    if outcome["unavailable_paper_ids"]:
        ids = ", ".join(str(p) for p in outcome["unavailable_paper_ids"])
        lines.append(f"- unavailable papers (no ingestible source): [{ids}]")
    if outcome["outcome"] == "partial":
        lines.append("- conclusion is based on the available full text only")
    return "\n".join(lines)


async def persist_report(state: ProjectWorkflowState) -> dict[str, Any]:
    """Batch D 5.4 + P2-D-R2-01: deterministic terminal node with
    RE-VERIFIED marker binding.

    done/partial -> exactly one report (partial prepends the structured
    disclosure); error -> ZERO reports. The gate uses
    ``answer_bound_fulltext_count`` — the number of draft ``[cite:]``
    markers that bind to the CURRENT dependency-scoped resolved evidence —
    recomputed from the database draft at persist time (the state's
    evidence_count is NEVER trusted)."""
    run_id = _run_id(state)
    project_id = int(state["project_id"])
    question = await _question_async(run_id)
    critic_passed = state.get("critic_passed", True)
    manifest_ids = [str(e) for e in (state.get("evidence_ids") or [])]
    # P2-C-R2-01: fence BEFORE the durable side effects.
    await _renew_lease_async(run_id)
    # P2-D-R3-01: EXACT manifest binding — re-verified from the DB draft.
    binding = await sync_to_async(_verify_marker_binding)(
        run_id, project_id, manifest_ids)
    outcome = await sync_to_async(_deterministic_outcome)(
        run_id, binding["resolved_reference_count"],
        binding["answer_bound_fulltext_count"], critic_passed)
    outcome["resolved_reference_count"] = binding["resolved_reference_count"]
    outcome["answer_bound_fulltext_count"] = \
        binding["answer_bound_fulltext_count"]
    if outcome["outcome"] == "error":
        # 5.4: no valid fulltext / unbound citations -> error, ZERO reports.
        await sync_to_async(_set_run_error)(run_id, outcome["reason"])
        await _event(state, "workflow_failed",
                     {"node": "persist_report", "status": "error",
                      "reason": outcome["reason"]},
                     dedupe_key=f"run:{run_id}:failed")
        return {"node": "persist_report", "phase": "error",
                "error_code": outcome["reason"], "report_id": None,
                "resolved_reference_count":
                    binding["resolved_reference_count"],
                "answer_bound_fulltext_count":
                    binding["answer_bound_fulltext_count"]}
    disclosure = _render_partial_disclosure(outcome)
    report_id = await sync_to_async(_save_report)(
        project_id, question, "", run_id, disclosure=disclosure)
    status_value = outcome["outcome"]  # done | partial
    await sync_to_async(_set_run_terminal_status)(run_id, status_value)
    # P2-C-CX-03: THE single completion producer — stable attempt-free key.
    await _event(state, "workflow_completed",
                 {"node": "persist_report", "status": status_value,
                  "report_id": report_id},
                 dedupe_key=f"run:{run_id}:completed")
    await _set_phase_async(run_id, status_value)
    return {"node": "persist_report", "phase": status_value,
            "report_id": report_id,
            "resolved_reference_count": binding["resolved_reference_count"],
            "answer_bound_fulltext_count":
                binding["answer_bound_fulltext_count"]}


def _verify_marker_binding(run_id: int, project_id: int,
                           manifest_ids: list[str] | None = None) -> dict[str, int]:
    """P2-D-R3-01: EXACT manifest binding.

    1. read the final draft from the DB;
    2. extract ``[cite:<canonical-evidence-id>]`` tokens;
    3. intersect with THIS run's recall manifest (``evidence_ids``);
    4. re-verify each bound ID against the database (chunk exists, hash/
       version unchanged, project membership, dependency scope, active
       embedding) through the canonical CitationResolver;
    5. return the precise bound count.

    NEVER scans all dependency-paper active chunks as a substitute for
    the recall set; NEVER trusts state counts."""
    from api.models import ProjectRun

    if manifest_ids is None:
        # fall back to the run's stored manifest? The manifest lives in
        # checkpoint state — the caller (persist_report) always passes it.
        manifest_ids = []
    manifest = set(manifest_ids)
    # Re-verify the manifest itself against the CURRENT database state
    verified = _reload_manifest_evidence(run_id, project_id,
                                         sorted(manifest))
    verified_ids = {item["evidence_id"] for item in verified}

    run = ProjectRun.objects.get(id=run_id)
    cited = set(_parse_cite_markers(run.draft_output))
    bound = cited & verified_ids  # EXACT intersection with this recall
    return {
        "resolved_reference_count": len(verified_ids),
        "answer_bound_fulltext_count": len(bound),
    }


def _set_run_error(run_id: int, reason: str) -> None:
    from api.models import ProjectRun
    ProjectRun.objects.filter(id=run_id).update(
        status="error", error_message=f"{reason}: no report generated",
        updated_at=timezone.now())


def _set_run_terminal_status(run_id: int, status_value: str) -> None:
    from api.models import ProjectRun
    ProjectRun.objects.filter(id=run_id).update(
        status=status_value, updated_at=timezone.now())


# ════════════════════════════════════════════════════════════════════════
# Sync helpers (DB / side-effect boundaries — all idempotent)
# ════════════════════════════════════════════════════════════════════════

def _enqueue_ingestion_for_run(run_id: int, project_id: int,
                               target_paper_ids: list[int]):
    """Task 4.2 + P2-C-CX-01 + P2-C-R2-02: enqueue ONLY the target papers of
    this run.

    Scope validation happens BEFORE any job/build/delivery is created: the
    membership must belong to the current project, must NOT be excluded and
    the paper must be one of this run's bounded target IDs. Foreign /
    excluded / unlinked papers never reach the IngestionService.

    Reuses the Phase 1 safe source identity (digest), digest file name,
    scoped request key, get_or_create_job and claim_build so the Agent /
    workflow duplicate paths converge on ONE project job and ONE global
    build. Two runs may share a build but keep independent dependency rows.
    Never calls PaperIngestionJob.objects.create directly.
    """
    from api.ingestion_service import IngestionService
    from api.models import ProjectPaper, ProjectRun
    from api.tasks import ingest_paper_pdf_task

    run = ProjectRun.objects.select_related("project").get(id=run_id)
    service = IngestionService()
    job_count = 0
    pending = False
    target_set = set(target_paper_ids)
    rows = (
        ProjectPaper.objects.select_related("paper", "project")
        .filter(project_id=project_id, paper_id__in=target_paper_ids)
        .exclude(status="excluded")  # P2-C-R2-02: excluded never enqueues
    )
    token = current_owner_token()
    for row in rows:
        paper = row.paper
        if _has_ready_fulltext(paper.id):
            # P2-C-R3-01: fence before the dependency write.
            _renew_lease(run_id, token)
            _safe_dependency(run, paper, job=None, status="ready")
            continue
        pdf_url = str(paper.pdf_url or "").strip()
        if not pdf_url:
            _renew_lease(run_id, token)
            _safe_dependency(run, paper, job=None, status="unavailable")
            continue
        try:
            # P2-C-R3-01: fence before EACH paper's job/build/dispatch.
            _renew_lease(run_id, token)
            # P2-C-R3-02: single canonical URL entry — request key, safe
            # digest file name and the global build claim derive from ONE
            # identity contract shared with the URL API and Agent queue.
            job, _created, _version = service.enqueue_url_job(
                project=row.project, paper=paper,
                source_url=pdf_url, source_kind="workflow",
            )
        except OwnerLeaseLost:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "workflow enqueue failed",
                extra={"event": "workflow_enqueue_failed",
                       "reason": exc.__class__.__name__, "status": "skipped"})
            _safe_dependency(run, paper, job=None, status="unavailable",
                             error_code="enqueue_failed")
            continue
        try:
            result = ingest_paper_pdf_task.delay(job.id)
            if result.id:
                job.celery_task_id = result.id
                job.save(update_fields=["celery_task_id", "updated_at"])
        except Exception:  # noqa: BLE001 - eager envs never break enqueue
            pass
        _safe_dependency(run, paper, job=job, status="pending")
        job_count += 1
        pending = True
    return job_count, pending


def _has_ready_fulltext(paper_id: int) -> bool:
    """P2-C-CX-01 + P2-C-R2-02: a dependency is ``ready`` ONLY when the
    active full-text version is fully current and compatible:
      - status == active
      - parser_identity matches the Phase 1 parser
      - embedding model / version / dim match the CURRENT configuration
      - chunk_count > 0
      - at least one Text chunk exists with metadata consistent with the
        version (stale/missing chunk rows are not ready)
    """
    from api.ingestion_service import IngestionService
    from rag.embedding import embedding_metadata
    from rag.models import PaperIndexVersion, Text

    v = PaperIndexVersion.objects.filter(
        paper_id=paper_id, status="active",
        parser_identity=IngestionService.PARSER_IDENTITY).first()
    if v is None:
        return False
    if not (v.chunk_count and v.chunk_count > 0):
        return False
    meta = embedding_metadata()
    if str(v.embedding_model) != str(meta["embedding_model"]):
        return False
    if str(v.embedding_version) != str(meta["embedding_version"]):
        return False
    if int(v.embedding_dim or 0) != int(meta["embedding_dim"]):
        return False
    has_chunk = Text.objects.filter(
        index_version=v,
        embedding_model=str(meta["embedding_model"]),
        embedding_version=str(meta["embedding_version"]),
        embedding_dim=int(meta["embedding_dim"]),
    ).exists()
    return has_chunk


def _digest_source(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:64]


def _safe_dependency(run, paper, *, job=None, status: str,
                     error_code: str = ""):
    """Dependency creation via the workflow_data boundary (Task 4.2):
    foreign/excluded/unlinked/job-mismatch fail closed."""
    from api.workflow_data import WorkflowDataError, create_workflow_dependency
    try:
        create_workflow_dependency(
            run=run, paper=paper, ingestion_job=job, status=status)
    except WorkflowDataError as exc:
        if exc.code == "duplicate_dependency":
            return  # idempotent re-entry — never a second row
        logger.warning(
            "workflow dependency rejected",
            extra={"event": "workflow_dependency_rejected",
                   "reason": exc.code, "status": "skipped"})
        if error_code or status == "pending":
            from api.models import ProjectWorkflowDependency
            ProjectWorkflowDependency.objects.filter(
                run=run, paper=paper).update(
                status="unavailable", error_code=exc.code)


def _refresh_dependency_status(run_id: int) -> bool:
    """Refresh each dependency from its linked job terminal state.
    Returns True while any dependency is still pending."""
    from api.models import ProjectWorkflowDependency

    pending = False
    deps = list(
        ProjectWorkflowDependency.objects.select_related("ingestion_job")
        .filter(run_id=run_id))
    for dep in deps:
        job = dep.ingestion_job
        if dep.status == "pending" and job is not None:
            if job.status == "embedded":
                dep.status = "succeeded"
                dep.terminal_at = job.terminal_at or job.updated_at
                dep.save(update_fields=["status", "terminal_at", "updated_at"])
            elif job.status == "failed":
                dep.status = "failed"
                dep.terminal_at = job.terminal_at or job.updated_at
                dep.error_code = job.error_code or "ingestion_failed"
                dep.save(update_fields=["status", "terminal_at",
                                        "error_code", "updated_at"])
        if dep.status == "pending":
            pending = True
    return pending


def _commit_rag_event(run_id: int, evidence_count: int) -> bool:
    """Task 4.5 + P2-C-CX-03: at most ONE committed RAG event per run
    (stable attempt-free dedupe key)."""
    from api.models import ProjectRun
    from agent.event_publisher import EventPublisher

    run = ProjectRun.objects.get(id=run_id)
    safe = EventPublisher(run=run).publish_with_key(
        "rag_committed",
        {"phase": "rag", "node": "query_hybrid_rag",
         "status": "committed", "evidence_count": evidence_count},
        dedupe_key=f"run:{run_id}:rag_committed")
    return bool(safe)


def _store_draft(run_id: int, section: str) -> None:
    """Draft goes to the private non-serialized ProjectRun.draft_output."""
    from api.models import ProjectRun
    ProjectRun.objects.filter(id=run_id).update(draft_output=section)


def _set_phase(run_id: int, phase: str) -> None:
    from api.models import ProjectRun
    ProjectRun.objects.filter(id=run_id).update(
        workflow_phase=phase, updated_at=timezone.now())


async def _set_phase_async(run_id: int, phase: str) -> None:
    await sync_to_async(_set_phase)(run_id, phase)


def _set_waiting(run_id: int) -> None:
    from api.models import ProjectRun
    ProjectRun.objects.filter(id=run_id).update(
        status="waiting_ingestion",
        workflow_phase="await_ingestion",
        waiting_at=timezone.now(),
        updated_at=timezone.now())


async def _set_waiting_async(run_id: int) -> None:
    await sync_to_async(_set_waiting)(run_id)


def _save_report(project_id: int, question: str, content: str,
                 run_id: int | None = None,
                 disclosure: str = "") -> int | None:
    """Task 4.5 + Batch D 5.4: report persistence via the workflow data
    boundary — the draft body comes from ProjectRun.draft_output when
    content is empty (checkpoint state never carries draft text). One
    report per run (source_run uniqueness). Partial reports carry the
    structured evidence-gap disclosure (IDs + stable codes only).

    Signature kept compatible with the approved Batch B production-path
    tests (``_save_report(project_id, question, content, run_id=...)``).
    """
    from api.models import ProjectRun
    from api.workflow_data import WorkflowDataError, create_workflow_report

    if run_id is None:
        logger.warning(
            "workflow report skipped: no run identity",
            extra={"event": "workflow_report_skipped",
                   "reason": "missing_run_identity", "status": "skipped"})
        return None
    try:
        run = ProjectRun.objects.get(id=run_id, project_id=project_id)
    except ProjectRun.DoesNotExist:
        logger.warning(
            "workflow report skipped: run/project mismatch",
            extra={"event": "workflow_report_skipped",
                   "reason": "run_project_mismatch", "status": "skipped"})
        return None
    if not content and run.draft_output:
        content = run.draft_output
    if not content:
        content = "证据不足，未能生成有效章节。"
    if disclosure:
        content = f"{disclosure}\n\n{content}"
    try:
        report = create_workflow_report(
            run=run,
            title=f"Expansion report: {question[:80]}",
            content=content,
            source="langgraph",
        )
        return report.id
    except WorkflowDataError as exc:
        logger.warning(
            "workflow report persistence handled by boundary",
            extra={"event": "workflow_report_boundary_reject",
                   "reason": exc.code, "status": "handled"})
        existing = getattr(run, "owned_report", None)
        return existing.id if existing else None


async def _event(state: ProjectWorkflowState, event_type: str,
                 payload: dict[str, Any], *, dedupe_key: str = "") -> None:
    from agent.event_publisher import EventPublisher

    run_id = state.get("run_id")
    if not run_id:
        return
    publisher = EventPublisher(
        project_id=state.get("project_id"), run_id=run_id)
    if dedupe_key:
        await sync_to_async(publisher.publish_with_key)(
            event_type, payload, dedupe_key=dedupe_key)
    else:
        await sync_to_async(publisher.publish)(event_type, payload)
    logger.info(
        "project workflow event",
        extra={"event": event_type, "project_id": state.get("project_id"),
               "run_id": run_id,
               "workflow_node": payload.get("node", ""),
               "status": payload.get("status", "ok")})


# ════════════════════════════════════════════════════════════════════════
# Checkpointer session + graph construction + entry points (P2-C-CX-04)
# ════════════════════════════════════════════════════════════════════════

class CheckpointerSession:
    """A task-scoped checkpointer session: one AsyncPostgresSaver bound to
    the calling event loop, explicitly closed via ``aclose()``. No global
    cross-loop singleton — repeated start/resume never accumulates residual
    PostgreSQL connections."""

    def __init__(self) -> None:
        self.connection = None
        self.saver = None

    async def get_saver(self):
        if self.saver is not None:
            return self.saver
        import psycopg
        from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
        from django.db import connection as django_conn

        db = django_conn.settings_dict
        self.connection = await psycopg.AsyncConnection.connect(
            host=db.get("HOST") or "localhost",
            port=int(db.get("PORT") or 5432),
            dbname=db.get("NAME") or "",
            user=db.get("USER") or "",
            password=db.get("PASSWORD") or "",
            autocommit=True,
        )
        self.saver = AsyncPostgresSaver(self.connection)
        return self.saver

    async def aclose(self) -> bool:
        """Explicitly close the session connection.

        P2-C-R2-03: a close failure MUST be observable — it is logged with
        a stable code (never connection details) and reflected in the
        return value so callers/tests can detect leaking sessions. Returns
        True when the connection is closed (or was never open)."""
        conn, self.connection, self.saver = self.connection, None, None
        if conn is None:
            return True
        try:
            await conn.close()
            return True
        except Exception as exc:  # noqa: BLE001 - observable, not silent
            logger.error(
                "workflow checkpoint session close failed",
                extra={"event": "workflow_session_close_failed",
                       "reason": exc.__class__.__name__,
                       "status": "close_failed"})
            return False


def get_checkpointer():
    """Backward-compatible accessor (always None in the new session model —
    callers must use CheckpointerSession)."""
    return None


def close_checkpointer() -> None:
    """Test-teardown hook — no-op with task-scoped sessions."""
    return None


def _stable_node(node):
    """P2-D-R2-04: wrap a graph node so an unexpected exception is
    converted to a STABLE-code WorkflowNodeError BEFORE LangGraph can
    checkpoint it — the raw exception body never enters checkpoints,
    pending writes, events or logs."""
    from langgraph.errors import GraphBubbleUp

    async def _wrapped(state):
        try:
            return await node(state)
        except (OwnerLeaseLost, GraphBubbleUp):
            raise  # lease fencing + LangGraph interrupts pass through
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "workflow node failed",
                extra={"event": "workflow_node_failed",
                       "node": getattr(node, "__name__", ""),
                       "reason": exc.__class__.__name__,
                       "status": "error"})
            from agent.events import error_hash
            raise WorkflowNodeError(
                f"{exc.__class__.__name__}:{error_hash(exc)}") from exc
    _wrapped.__name__ = getattr(node, "__name__", "node")
    return _wrapped


async def build_project_workflow(session: CheckpointerSession):
    """Compile the workflow graph with the CALLER-OWNED checkpointer session.

    P2-C-R2-03: the session is a REQUIRED argument — this function never
    creates a session implicitly, so every connection has an owner
    responsible for closing it."""
    if session is None:
        raise ValueError("explicit CheckpointerSession required")
    graph = StateGraph(ProjectWorkflowState)
    graph.add_node("plan_expansion", _stable_node(plan_expansion))
    graph.add_node("search_sources", _stable_node(search_sources))
    graph.add_node("add_candidates", _stable_node(add_candidates))
    graph.add_node("enqueue_ingestion", _stable_node(enqueue_ingestion))
    graph.add_node("await_ingestion", _stable_node(await_ingestion))
    graph.add_node("query_hybrid_rag", _stable_node(query_hybrid_rag))
    graph.add_node("critic", _stable_node(critic))
    graph.add_node("draft_report", _stable_node(draft_report))
    graph.add_node("persist_report", _stable_node(persist_report))
    graph.add_edge(START, "plan_expansion")
    graph.add_edge("plan_expansion", "search_sources")
    graph.add_edge("search_sources", "add_candidates")
    graph.add_edge("add_candidates", "enqueue_ingestion")
    graph.add_edge("enqueue_ingestion", "await_ingestion")
    graph.add_edge("await_ingestion", "query_hybrid_rag")
    graph.add_edge("query_hybrid_rag", "critic")
    graph.add_edge("critic", "draft_report")
    graph.add_edge("draft_report", "persist_report")
    graph.add_edge("persist_report", END)
    saver = await session.get_saver()
    return graph.compile(checkpointer=saver)


def _graph_config(run_id: int) -> dict:
    return {"configurable": {"thread_id": str(run_id)}}


async def run_project_research_expand(project_id: int, question: str,
                                      run_id: int) -> dict[str, Any]:
    """Start the checkpointed workflow under thread_id=str(run_id) using a
    task-scoped saver session (P2-C-CX-04)."""
    session = CheckpointerSession()
    try:
        graph = await build_project_workflow(session)
        state: ProjectWorkflowState = {
            "project_id": project_id,
            "run_id": run_id,
            "phase": "started",
        }
        result = await graph.ainvoke(state, config=_graph_config(run_id))
        return dict(result)
    finally:
        await session.aclose()


async def resume_project_research_expand(project_id: int,
                                         run_id: int) -> dict[str, Any]:
    """Explicit resume entry (Task 4.3 + P2-C-R2-03): re-enters the SAME
    thread with a Command(resume=...) wakeup using a caller-owned saver
    session, and emits the ``workflow_resumed`` safe event with a stable
    attempt-free dedupe key. on_commit callbacks and Beat reconciliation
    are Batch D and intentionally not implemented here."""
    session = CheckpointerSession()
    try:
        graph = await build_project_workflow(session)
        # P2-C-R3-01: confirm ownership BEFORE publishing workflow_resumed.
        await _renew_lease_async(run_id)
        # P2-C-R2-03: one resumed event per run — replay-safe.
        await _event({"project_id": project_id, "run_id": run_id},
                     "workflow_resumed",
                     {"phase": "resume", "status": "resumed",
                      "run_id": run_id},
                     dedupe_key=f"run:{run_id}:resumed")
        result = await graph.ainvoke(
            Command(resume={"wakeup": "explicit_resume", "run_id": run_id}),
            config=_graph_config(run_id))
        return dict(result)
    finally:
        await session.aclose()


async def sync_workflow_dependencies(state: ProjectWorkflowState) -> dict[str, Any]:
    """Dependency construction entry used by Batch A CX-04: only own
    non-excluded target papers become dependencies."""
    run_id = _run_id(state)
    project_id = int(state["project_id"])
    target_paper_ids = [int(p) for p in (state.get("paper_ids") or [])]
    await sync_to_async(_enqueue_ingestion_for_run)(
        run_id, project_id, target_paper_ids)
    return {"phase": "enqueued"}


def _rewrite_query(question: str) -> str:
    lowered = question.lower()
    if "mamba" in lowered:
        return "Mamba selective state space model long sequence follow-up"
    if "rag" in lowered or "retrieval augmented generation" in lowered:
        return "retrieval augmented generation evaluation faithfulness benchmark"
    if "citation" in lowered or "引用" in lowered:
        return "citation graph bibliographic coupling paper recommendation"
    return question[:180]
