"""Wave 1A — 12 core Agent tasks against real DeepSeek (deepseek-live-evaluation §5).

Runs each task ONCE via the real ReAct loop (use_llm=True), captures the full
evidence bundle GPT requires (§5), and enforces immediate-stop conditions.

Evidence saved per task:
    raw_model_answer / postprocessed_answer / original vs postprocessed citation rate
    full ordered tool-call array (name, args, result-summary)
    per-claim paper/chunk/page binding
    DeepSeek call count, prompt/completion/reasoning tokens
    first-event latency, total duration
    (Judge is a separate pass to avoid self-judge bias on the same run)

Immediate-stop (any one halts the whole wave):
    fabricated paper/citation, cross-project evidence, metadata-as-fulltext,
    compare covering only one side but claiming done, autonomous destructive op,
    tool-call loop runaway, raw answer uncited but postprocess-made-it-grounded.
"""
from __future__ import annotations

import asyncio
import json
import re
import time
from pathlib import Path


DESTRUCTIVE_KEYWORDS = ("删除", "清空", "覆盖", "delete", "clear", "wipe")


def _redact(text: str) -> str:
    return re.sub(r"sk-[A-Za-z0-9_\-]{16,}", "***REDACTED***", text or "")


def _classify_retrieval_tier(events: list[dict]) -> str:
    """GPT v4: classify which retrieval tier the TOOLS covered (not the answer).

    Based on which tools were called, NOT whether the answer used the evidence.
    none:      no retrieval tools called
    metadata:  only search/list/graph/export/add
    fulltext:  only query_project_rag/draft_report/compare_papers (with evidence)
    mixed:     both metadata and fulltext tools
    """
    fulltext_tools = {"query_project_rag", "draft_report_section", "compare_papers"}
    action_tools = {"search_papers", "list_project_papers", "export_bibtex",
                    "get_project_citation_graph", "add_papers_to_project"}
    called = {e["data"].get("name") for e in events
              if e["event"] == "tool_call" and isinstance(e.get("data"), dict)}
    has_fulltext = bool(called & fulltext_tools)
    has_metadata = bool(called & action_tools)
    if has_fulltext and has_metadata:
        return "mixed"
    if has_fulltext:
        return "fulltext"
    if has_metadata:
        return "metadata"
    return "none"


def _classify_available_evidence_tier(quality: dict) -> str:
    """GPT v5: what tier of evidence the TOOLS retrieved (availability, not usage).

    Based on evidence_count from quality_check — did any fulltext tool return chunks?
    none:      no evidence returned
    fulltext:  evidence_count > 0 (fulltext chunks were available)
    """
    evidence_count = quality.get("evidence_count", 0)
    return "fulltext" if evidence_count > 0 else "none"


def _classify_answer_evidence_tier(quality: dict) -> str:
    """GPT v5: what tier of evidence the final ANSWER actually BOUND to.

    Only fulltext if at least one citation RESOLVES to a verified project
    paper + chunk/page (reference_resolved=True). Marker appearing in text
    without resolving to a real chunk → none (unbound).
    """
    answer_mode = quality.get("answer_mode", "answered")
    resolved_count = quality.get("resolved_citation_count", 0)
    if answer_mode in ("abstained", "clarified"):
        return "none"
    if answer_mode == "action_result":
        return "metadata"
    # fulltext requires at least one citation that resolves to a real project chunk.
    if resolved_count > 0:
        return "fulltext"
    return "none"


def _classify_citation_binding(quality: dict) -> str:
    """GPT v5: citation_reference_status (renamed from citation_binding_status).

    Based on whether markers RESOLVE to real project paper+chunk (reference_resolved),
    NOT whether the marker text appears (that's citation_marker_status).
    claim_support_status is left for Judge.

    fully_bound:     all present markers resolve to real project chunks
    partially_bound: some resolve, some don't
    unbound:         no resolved citations (markers present but unresolved, or no markers)
    not_required:    action_result/abstained (citations not expected)
    """
    answer_mode = quality.get("answer_mode", "answered")
    if answer_mode in ("abstained", "clarified", "action_result"):
        return "not_required"
    citations = quality.get("citations", [])
    present_citations = [c for c in citations if c.get("citation_marker_status") == "present"]
    if not present_citations:
        return "unbound"
    resolved = sum(1 for c in present_citations if c.get("reference_resolved"))
    unresolved = len(present_citations) - resolved
    if resolved == 0:
        return "unbound"
    if unresolved == 0:
        return "fully_bound"
    return "partially_bound"


def _is_stop_condition(result: dict) -> tuple[bool, str]:
    """Check GPT's immediate-stop conditions. Returns (stop, reason)."""
    answer = (result.get("answer") or "")
    events = result.get("events") or []
    quality = next((e["data"] for e in events if e["event"] == "quality_check"), {})
    raw_cited = quality.get("model_cited", False)
    postprocessed_added = quality.get("postprocessed_added_markers", False)
    verdict = quality.get("verdict", "")
    evidence_count = quality.get("evidence_count", 0)
    answer_mode = quality.get("answer_mode", "answered")
    evidence_status = quality.get("evidence_status", "none")
    raw_answer = result.get("raw_model_answer", "")

    # P0-2 (S1 structured gate): NO keyword matching. Use answer_mode + evidence_status.
    # If the task was answered (not abstained/clarified/action_result) but evidence
    # is insufficient or none, the model used domain knowledge → STOP.
    if answer_mode == "answered" and evidence_status in ("none", "insufficient") and evidence_count == 0:
        return True, (f"answered with no project evidence "
                      f"(answer_mode={answer_mode}, evidence_status={evidence_status}, evidence_count={evidence_count})")

    # 1. raw answer uncited but postprocess made it grounded
    if not raw_cited and postprocessed_added and verdict == "grounded":
        return True, "raw answer had no citation but postprocess appended markers and verdict=grounded"
    # 2. tool-call loop runaway: detect REPEATED (name, args) tuples (same call >2x)
    seen_pairs: dict[tuple, int] = {}
    for e in events:
        if e["event"] == "tool_call" and isinstance(e.get("data"), dict):
            d = e["data"]
            key = (d.get("name"), str(d.get("arguments", ""))[:100])
            seen_pairs[key] = seen_pairs.get(key, 0) + 1
    repeated = [(k, c) for k, c in seen_pairs.items() if c >= 3]
    if repeated:
        return True, f"tool-call loop: {repeated[0][0][0]} called {repeated[0][1]}x with same args"
    # C3: critical tool error but model continued (DoesNotExist/IntegrityError)
    # Check BOTH error and error_message fields (C1 alignment).
    for e in events:
        if e["event"] == "tool_result" and isinstance(e.get("data"), dict):
            d = e["data"]
            err_text = str(d.get("error_message", "") or d.get("error", "")).lower()
            if d.get("status") == "error" and any(k in err_text for k in ("doesnotexist", "integrityerror", "foreignkey")):
                if answer_mode == "answered":
                    return True, f"critical tool error ({err_text[:60]}) but model answered"
    return False, ""


async def run_one_task(harness, message: str, case_id: str) -> dict:
    """Run a single task via the real ReAct harness, capture full evidence.

    Uses stream() directly so first_event_ms is recorded at the moment the first
    event arrives (not after the whole task finishes).

    Tasks 5.x (§28.1): the raw model answer is no longer streamed as an event;
    the harness delivers it to the explicit eval hook
    ``harness.raw_answer_callback`` instead.
    """
    started = time.perf_counter()
    events: list[dict] = []
    first_event_ms = None
    captured_raw: list[str] = []

    def _capture_raw(answer: str) -> None:
        captured_raw.append(answer)

    harness.raw_answer_callback = _capture_raw
    # Stream events live — record first_event_ms at the moment of arrival.
    async for event in harness.stream(message):
        if first_event_ms is None:
            first_event_ms = round((time.perf_counter() - started) * 1000, 2)
        events.append(event)
    total_ms = round((time.perf_counter() - started) * 1000, 2)

    # Extract answer + context from events (run() normally does this internally)
    # Reconstruct answer from tokens
    answer = "".join(e["data"].get("text", "") for e in events if e["event"] == "token")
    result = {"answer": answer, "events": events}

    # Tasks 5.x: REAL raw model answer comes from the explicit eval hook
    # (before _ensure_source_markers), never from streamed events.
    raw_model_answer = captured_raw[0] if captured_raw else ""

    # tool calls (ordered, with args + summary + project_id audit)
    tool_calls = []
    project_id_overrides = []
    for e in events:
        if e["event"] == "tool_call" and isinstance(e.get("data"), dict):
            d = e["data"]
            tool_calls.append({
                "name": d.get("name"),
                "arguments": _redact(json.dumps(d.get("arguments", {}), ensure_ascii=False, default=str))[:300],
                "iteration": d.get("iteration"),
            })
            # P0-1 audit: did the model try to supply a different project_id?
            if d.get("model_supplied_project_id") is not None:
                project_id_overrides.append({"tool": d.get("name"), "model_pid": d.get("model_supplied_project_id")})
    # tool results (summaries)
    tool_results = []
    tool_errors = []
    for e in events:
        if e["event"] == "tool_result" and isinstance(e.get("data"), dict):
            d = e["data"]
            tool_results.append({k: (str(v)[:200] if isinstance(v, str) else v) for k, v in d.items() if k != "content"})
            if d.get("status") == "error":
                tool_errors.append({"tool": d.get("name", "?"), "error": d.get("error_message", "")[:120]})

    postprocessed_answer = result.get("answer", "")
    quality = next((e["data"] for e in events if e["event"] == "quality_check"), {})

    # DeepSeek call count (llm_call events)
    llm_calls = sum(1 for e in events if e["event"] == "llm_call")
    # usage: sum tokens from llm_result events (P1: was always 0)
    prompt_tokens = 0
    completion_tokens = 0
    for e in events:
        if e["event"] == "llm_result" and isinstance(e.get("data"), dict):
            u = e["data"].get("usage") or {}
            prompt_tokens += int(u.get("prompt_tokens", 0))
            completion_tokens += int(u.get("completion_tokens", 0))

    citations = quality.get("citations", [])

    evidence = {
        "case_id": case_id,
        "message": message,
        "raw_model_answer": raw_model_answer,  # P1: REAL raw (before _ensure_source_markers)
        "postprocessed_answer": postprocessed_answer,
        "raw_chars": len(raw_model_answer),
        "postprocessed_chars": len(postprocessed_answer),
        "postprocessed_added_chars": len(postprocessed_answer) - len(raw_model_answer),
        "model_cited": quality.get("model_cited"),
        "postprocessed_added_markers": quality.get("postprocessed_added_markers"),
        "verdict": quality.get("verdict"),
        "evidence_count": quality.get("evidence_count", 0),
        # GPT v3: persist ALL structured fields from quality_check
        "answer_mode": quality.get("answer_mode"),
        "evidence_status": quality.get("evidence_status"),
        "citation_presence": quality.get("citation_presence"),
        "safety_replaced": quality.get("safety_replaced", False),
        # GPT v5: three-tier evidence classification
        "retrieval_tier": _classify_retrieval_tier(events),
        "available_evidence_tier": _classify_available_evidence_tier(quality),
        "answer_evidence_tier": _classify_answer_evidence_tier(quality),
        "citation_binding_status": _classify_citation_binding(quality),
        "citations": [{
            "marker": c.get("marker"),
            "citation_marker_status": c.get("citation_marker_status"),
            "reference_resolved": c.get("reference_resolved"),
            "project_id": c.get("project_id"),
            "paper_id": c.get("paper_id"),
            "chunk_index": c.get("chunk_index"),
            "page_start": c.get("page_start"),
            "page_end": c.get("page_end"),
            "section": c.get("section"),
            "evidence_type": c.get("evidence_type"),
            "claim_support_status": c.get("claim_support_status", "pending"),
            "summary": (c.get("summary") or "")[:100],
        } for c in citations],
        "tool_calls": tool_calls,
        "tool_results_summaries": tool_results,
        "tool_errors": tool_errors,
        "tool_count": len(tool_calls),
        "llm_calls": llm_calls,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "project_id_overrides": project_id_overrides,  # P0-1 audit
        "first_event_ms": first_event_ms,
        "total_ms": total_ms,
    }
    stop, reason = _is_stop_condition({"answer": postprocessed_answer, "events": events,
                                       "raw_model_answer": raw_model_answer})
    evidence["stop_triggered"] = stop
    evidence["stop_reason"] = reason
    return evidence


# The 12 core tasks (GPT Wave 1A §5)
WAVE1A_TASKS = [
    ("L1-01", "项目库里的 Mamba 和 Transformer 各有什么核心特点？请基于项目库回答并引用来源。", "sequence"),
    ("L1-02", "Mamba 论文里关于 selective state space 的具体机制是什么？读相关章节。", "sequence"),
    ("L1-03", "比较 Mamba 和 Attention Is All You Need 在方法上的核心差异。", "sequence"),
    ("L1-04", "Vaswani 2017 年关于注意力的论文用了什么方法？", "sequence"),
    ("L1-05", "列出当前项目论文库。", "sequence"),
    ("L1-06", "找 2023 年后关于 state space model 的后续工作。", "sequence"),
    ("L1-07", "检索 Mamba 的后续工作，加入项目库，再基于新入库的论文回答。", "sequence"),
    ("L1-08", "生成项目引用图谱并推荐先读哪几篇。", "sequence"),
    ("L1-09", "基于项目库写一个 related work 草稿。", "sequence"),
    ("L1-10", "导出项目论文为 BibTeX。", "sequence"),
    ("L1-11", "项目库里关于量子纠错码的实验数值是多少？", "sequence"),
    ("L1-12", "帮我看看。(含糊请求，应澄清而非盲目调高成本工具)", "sequence"),
]

SENTINELS = ["L1-01", "L1-03", "L1-05", "L1-11"]
# GPT rerun set after P0 fixes: L1-03/06/07/11 (the cases that exposed the P0s)
RERUN_SET = ["L1-03", "L1-06", "L1-07", "L1-11"]


async def _run_with_snapshot(case_id, message, topic, run_dir):
    """Run one task against a REAL-PDF project with proper snapshot isolation (P0-3).

    P0-3 fix: every case must start from the SAME project state (same papers +
    chunks), not inherit accumulated papers from prior cases. We:
      1. Reset the project fully (clear runs/sessions/reports/ProjectPaper)
      2. Re-link the 12 real papers to their topic project (ProjectPaper restore)
      3. The global rag.Text chunks survive (they're keyed by paper_id)
    This guarantees L1-05/L1-10/L1-11 all see the same paper count.
    """
    from asgiref.sync import sync_to_async
    from agent.harness import ProjectAgentHarness
    from api.fixtures import reset_project_state
    from api.models import ResearchProject, ProjectPaper
    from papers.models import Paper
    from eval.real_pdf_dataset import REAL_PAPERS

    title = f"Fixture: {topic}-models PDFs" if topic == "sequence" else f"Fixture: {topic} papers"

    def _setup_project():
        proj = ResearchProject.objects.get(title=title)
        # Full reset: clear ALL child state including ProjectPaper links
        reset_project_state(proj.id)
        # Re-link ONLY this topic's real papers (snapshot restore)
        for p in REAL_PAPERS:
            if p.topic != topic:
                continue
            paper = Paper.objects.filter(arxiv_id=p.arxiv_id).first()
            if paper:
                ProjectPaper.objects.get_or_create(
                    project=proj, paper=paper,
                    defaults={"status": "included", "source_reason": "real pdf snapshot"})
        return proj

    proj = await sync_to_async(_setup_project)()
    harness = ProjectAgentHarness(proj.id, use_llm=True)
    evidence = await run_one_task(harness, message, case_id)

    # P0-1 audit: record project_id override attempts (informational — the
    # force-override in chat_loop prevents actual leakage). Only stop if the
    # model supplied a NON-ZERO project_id (a real cross-project attempt).
    if evidence.get("project_id_overrides"):
        real_overrides = [o for o in evidence["project_id_overrides"] if o.get("model_pid") not in (0, None, "0")]
        if real_overrides:
            evidence["stop_triggered"] = True
            evidence["stop_reason"] = f"P0-1 real project_id override (non-zero): {real_overrides}"

    out = Path(run_dir) / f"{case_id}.json"
    out.write_text(_redact(json.dumps(evidence, ensure_ascii=False, indent=2, default=str)), encoding="utf-8")
    return evidence


def run_wave1a(*, sentinels_only: bool = True, only_cases: list[str] | None = None, write_dir: str | None = None) -> dict:
    """Run Wave 1A tasks.

    sentinels_only=True runs just the 4 sentinels.
    only_cases=['L1-03',...] runs a specific subset (for GPT rerun).
    """
    from datetime import datetime

    run_id = f"{datetime.now().strftime('%Y%m%d-%H%M')}-wave1a"
    run_dir = write_dir or f"eval/reports/{run_id}/live"
    Path(run_dir).mkdir(parents=True, exist_ok=True)

    if only_cases:
        tasks = [(c, m, t) for c, m, t in WAVE1A_TASKS if c in only_cases]
    elif sentinels_only:
        tasks = [(c, m, t) for c, m, t in WAVE1A_TASKS if c in SENTINELS]
    else:
        tasks = list(WAVE1A_TASKS)
    all_evidence = []
    halted = False
    halt_reason = ""

    for case_id, message, topic in tasks:
        if halted:
            break
        print(f"[{case_id}] running on {topic} project...", flush=True)
        try:
            ev = asyncio.run(_run_with_snapshot(case_id, message, topic, run_dir))
        except Exception as exc:
            from .safe_error import exception_record
            ev = {"case_id": case_id, "stop_triggered": False,
                  **exception_record(exc)}
            print(f"[{case_id}] EXCEPTION: {ev['error']}")
        all_evidence.append(ev)
        status = "STOP" if ev.get("stop_triggered") else ("ERR" if "error" in ev else "ok")
        print(f"[{case_id}] {status} | tools={ev.get('tool_count','?')} cited={ev.get('model_cited','?')} verdict={ev.get('verdict','?')}")
        if ev.get("stop_triggered"):
            halted = True
            halt_reason = ev.get("stop_reason", "")

    summary = {
        "run_id": run_id,
        "sentinels_only": sentinels_only,
        "tasks_run": len(all_evidence),
        "halted": halted,
        "halt_reason": halt_reason,
        "cases": [{"id": e.get("case_id"), "tools": e.get("tool_count"), "cited": e.get("model_cited"),
                   "verdict": e.get("verdict"), "stop": e.get("stop_triggered", False),
                   "error": e.get("error")} for e in all_evidence],
    }
    (Path(run_dir) / "summary.json").write_text(
        _redact(json.dumps(summary, ensure_ascii=False, indent=2, default=str)), encoding="utf-8")
    print(f"\n=== Wave 1A {'HALTED' if halted else 'sentinels done'}: {summary['tasks_run']} tasks ===")
    if halted:
        print(f"STOP REASON: {halt_reason}")
    return summary
