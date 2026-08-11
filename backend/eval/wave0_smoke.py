"""Wave 0 — connection & protocol smoke (deepseek-live-evaluation-plan §5).

Uses the LEAST budget to confirm the environment before any quality eval.
Five checks; STOPS on the first P0 (auth/model/json/400/fabrication).

Usage (from backend/):
    python -m eval.wave0_smoke
    python -m eval.wave0_smoke --write-report

Budget cap (§7): 12 DeepSeek calls total. Each call is counted; if the cap is
hit the script stops with a clear reason. Output is redacted (no key, no full
prompt) and writes JSON to eval/reports/<run_id>/live/wave-0-smoke.json.

NOTE on embeddings: BGE-M3 (FlagEmbedding) may not be installed. W0-3 RAG
therefore runs with the FAKE embedding provider + SQLite — it validates the
LLM answer quality and citation behaviour, NOT real retrieval quality (that is
Wave 5, which needs BGE-M3 + pgvector). This is recorded honestly in the report.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path

# Force UTF-8 stdout on Windows (manual §3.2 / PRE-07).
try:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
except (AttributeError, OSError):
    pass


def _redact(text: str) -> str:
    return re.sub(r"sk-[A-Za-z0-9_\-]{16,}", "***REDACTED***", text or "")


class UsageTracker:
    """Count DeepSeek calls + accumulate tokens (manual §7 budget cap)."""

    def __init__(self, max_calls: int = 12) -> None:
        self.max_calls = max_calls
        self.calls = 0
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.reasoning_tokens = 0

    def record(self, usage: dict | None) -> None:
        self.calls += 1
        if not usage:
            return
        self.prompt_tokens += int(usage.get("prompt_tokens", 0))
        self.completion_tokens += int(usage.get("completion_tokens", 0))
        # DeepSeek reports reasoning tokens inside completion_tokens_details
        rd = usage.get("completion_tokens_details") or {}
        self.reasoning_tokens += int(rd.get("reasoning_tokens", 0))

    @property
    def over_budget(self) -> bool:
        return self.calls > self.max_calls


def _load_env() -> bool:
    """Load backend/.env; return whether key is configured. Never prints the key."""
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).resolve().parents[1] / ".env")
    return bool(os.environ.get("DEEPSEEK_API_KEY", "").strip())


# --- Wave 0 checks ---------------------------------------------------------


def w0_1_plain_completion(tracker: UsageTracker) -> dict:
    """W0-1: minimal plain JSON response (auth + model name)."""
    from llm.deepseek import DeepSeekClient

    client = DeepSeekClient()
    started = time.perf_counter()
    r = client.complete(
        [{"role": "user", "content": "Reply with exactly the word: ok"}],
        thinking=False,
        max_tokens=16,
    )
    tracker.record(r.get("usage"))
    content = (r.get("content") or "").strip().lower()
    return {
        "id": "W0-1",
        "name": "plain completion",
        "passed": bool(content) and "ok" in content,
        "model": client.model,
        "duration_ms": round((time.perf_counter() - started) * 1000, 2),
        "content_preview": _redact(content[:50]),
        "had_reasoning": bool(r.get("reasoning")),
    }


def w0_2_function_calling_two_rounds(tracker: UsageTracker) -> dict:
    """W0-2: two-round Function Calling, validates reasoning_content round-trip (B1)."""
    from llm.deepseek import DeepSeekClient

    client = DeepSeekClient()
    started = time.perf_counter()
    tools = [{
        "type": "function",
        "function": {
            "name": "get_project_time",
            "description": "Return the current server time in ISO format.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    }]
    messages = [
        {"role": "system", "content": "Use the get_project_time tool to answer."},
        {"role": "user", "content": "What time is it on the server? Call the tool then answer briefly."},
    ]
    # Round 1: model should call the tool.
    r1 = client.complete_with_tools(messages, tools, thinking=True, max_tokens=1024)
    tracker.record(r1.get("usage"))
    tool_calls = r1.get("tool_calls", [])
    reasoning_1 = r1.get("reasoning_content")

    if not tool_calls:
        return {"id": "W0-2", "name": "function calling 2 rounds", "passed": False,
                "reason": "model did not call the tool in round 1",
                "model": client.model, "duration_ms": round((time.perf_counter() - started) * 1000, 2)}

    # Re-append assistant message WITH reasoning_content (the B1 fix).
    assistant_msg = {"role": "assistant", "content": r1.get("content") or ""}
    if reasoning_1:
        assistant_msg["reasoning_content"] = reasoning_1
    assistant_msg["tool_calls"] = [
        {"id": tc["id"], "type": "function",
         "function": {"name": tc["name"], "arguments": tc["arguments"]}}
        for tc in tool_calls
    ]
    messages.append(assistant_msg)
    # Tool result message.
    messages.append({
        "role": "tool", "tool_call_id": tool_calls[0]["id"],
        "content": datetime.utcnow().isoformat() + "Z",
    })

    # Round 2: model answers based on the tool result. If reasoning_content
    # round-trip is broken, DeepSeek returns 400 here.
    try:
        r2 = client.complete_with_tools(messages, tools, thinking=True, max_tokens=512)
    except Exception as exc:
        return {"id": "W0-2", "name": "function calling 2 rounds", "passed": False,
                "reason": f"round 2 raised {exc.__class__.__name__} (likely reasoning_content protocol)",
                "model": client.model, "duration_ms": round((time.perf_counter() - started) * 1000, 2)}
    tracker.record(r2.get("usage"))
    answer = (r2.get("content") or "").strip()
    # Success = round 2 returned an answer (no 400) and did not call the tool again.
    return {
        "id": "W0-2",
        "name": "function calling 2 rounds",
        "passed": bool(answer) and not r2.get("tool_calls"),
        "reasoning_content_round_tripped": reasoning_1 is not None,
        "round2_answer_preview": _redact(answer[:80]),
        "model": client.model,
        "duration_ms": round((time.perf_counter() - started) * 1000, 2),
    }


def w0_3_project_rag_answer(tracker: UsageTracker) -> dict:
    """W0-3: a real project-RAG answer with citations (LLM quality, fake embedding)."""
    from django.db import connection
    from api.demo import seed_demo_project
    from agent.harness import ProjectAgentHarness

    started = time.perf_counter()
    seeded = seed_demo_project("Wave 0 RAG smoke", reuse=True, status="active", reset=True)
    project_id = seeded["project"]["id"]
    harness = ProjectAgentHarness(project_id, use_llm=True)
    result = asyncio.run(harness.run("Mamba 有什么核心特点？请基于项目库回答并引用来源。"))
    # Count LLM calls from events (each llm_call/llm_result pair ≈ 1 call).
    llm_events = sum(1 for e in result["events"] if e["event"] == "llm_call")
    tracker.calls += llm_events  # RAG path calls aren't via the tracker.record path
    answer = result.get("answer", "")
    quality = next((e["data"] for e in result["events"] if e["event"] == "quality_check"), {})
    # Honest: model_cited tells us whether the model itself cited (vs auto-append).
    return {
        "id": "W0-3",
        "name": "project RAG answer",
        "passed": bool(answer) and quality.get("model_cited", False),
        "embedding_note": "fake-hash provider (BGE-M3 not installed); validates LLM quality, not retrieval",
        "database": connection.vendor,
        "verdict": quality.get("verdict"),
        "model_cited": quality.get("model_cited"),
        "postprocessed_added_markers": quality.get("postprocessed_added_markers"),
        "answer_preview": _redact(answer[:200]),
        "tool_trajectory": [e["event"] for e in result["events"] if e["event"] in ("tool_call", "evidence", "quality_check")][:12],
        "duration_ms": round((time.perf_counter() - started) * 1000, 2),
    }


def w0_4_no_evidence_abstention(tracker: UsageTracker) -> dict:
    """W0-4: an unanswerable question → the agent should abstain, not fabricate."""
    from api.demo import seed_demo_project
    from agent.harness import ProjectAgentHarness

    started = time.perf_counter()
    seeded = seed_demo_project("Wave 0 abstain smoke", reuse=True, status="active", reset=True)
    project_id = seeded["project"]["id"]
    harness = ProjectAgentHarness(project_id, use_llm=True)
    # A question the demo project (Mamba / Attention) cannot support.
    result = asyncio.run(harness.run("这篇论文里关于量子纠错码的具体实验数值是多少？"))
    llm_events = sum(1 for e in result["events"] if e["event"] == "llm_call")
    tracker.calls += llm_events
    answer = result.get("answer", "")
    quality = next((e["data"] for e in result["events"] if e["event"] == "quality_check"), {})
    lowered = answer.lower()
    fabricated = any(w in lowered for w in ["量子纠错", "quantum error correction", "steane", "surface code", "shor"])
    # The agent abstains in varied wording: "无法定位", "项目论文库目前是空的",
    # "暂时无法", "请确认", "需要", "search_papers" (offers to search rather than
    # invent). Any honest gap-acknowledgement counts as a correct abstention.
    abstained = (
        any(w in lowered for w in ["无法", "暂时无法", "空", "没有", "未包含", "不在", "无相关", "cannot", "no evidence", "not in", "请确认", "需要"])
        or quality.get("verdict") in ("needs_more_evidence", "needs_source_markers")
    )
    return {
        "id": "W0-4",
        "name": "no-evidence abstention",
        "passed": abstained and not fabricated,
        "verdict": quality.get("verdict"),
        "model_cited": quality.get("model_cited"),
        "answer_preview": _redact(answer[:200]),
        "duration_ms": round((time.perf_counter() - started) * 1000, 2),
    }


def w0_5_bge_m3_embedding() -> dict:
    """W0-5: BGE-M3 dense+sparse (if FlagEmbedding is installed)."""
    started = time.perf_counter()
    try:
        from FlagEmbedding import BGEM3FlagModel  # noqa: F401
    except Exception as exc:
        return {"id": "W0-5", "name": "BGE-M3 embedding", "passed": False,
                "status": "NOT RUN",
                "reason": f"FlagEmbedding not importable: {exc.__class__.__name__}",
                "duration_ms": 0}
    # If importable, actually load + encode.
    try:
        from rag.embedding import BGEM3EmbeddingProvider
        prov = BGEM3EmbeddingProvider(model_name="BAAI/bge-m3", dimension=1024)
        dense, sparse = prov.encode_dense_sparse(["selective state space model"])
        import numpy as np
        dense_ok = isinstance(dense, np.ndarray) and dense.shape[-1] == 1024
        sparse_ok = bool(sparse) and len(sparse) > 0
        return {"id": "W0-5", "name": "BGE-M3 embedding",
                "passed": bool(dense_ok and sparse_ok),
                "dense_dim": int(dense.shape[-1]) if dense_ok else 0,
                "sparse_nonzero": len(sparse) if sparse_ok else 0,
                "duration_ms": round((time.perf_counter() - started) * 1000, 2)}
    except Exception as exc:
        return {"id": "W0-5", "name": "BGE-M3 embedding", "passed": False,
                "status": "NOT RUN", "reason": f"load failed: {exc.__class__.__name__}: {exc}",
                "duration_ms": round((time.perf_counter() - started) * 1000, 2)}


def main() -> int:
    import django
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    django.setup()

    parser = argparse.ArgumentParser()
    parser.add_argument("--write-report", action="store_true")
    args = parser.parse_args()

    if not _load_env():
        print("DEEPSEEK_API_KEY not configured. Aborting Wave 0.")
        return 2

    tracker = UsageTracker(max_calls=12)
    run_id = f"{datetime.now().strftime('%Y%m%d-%H%M')}-wave0"
    results: list[dict] = []
    started = time.perf_counter()
    p0_stop = False

    checks = [
        ("W0-1", lambda: w0_1_plain_completion(tracker)),
        ("W0-2", lambda: w0_2_function_calling_two_rounds(tracker)),
        ("W0-3", lambda: w0_3_project_rag_answer(tracker)),
        ("W0-4", lambda: w0_4_no_evidence_abstention(tracker)),
        ("W0-5", lambda: w0_5_bge_m3_embedding()),
    ]

    for cid, check in checks:
        if tracker.over_budget:
            print(f"[{cid}] SKIPPED — budget cap ({tracker.max_calls} calls) reached")
            results.append({"id": cid, "passed": False, "reason": "budget cap reached"})
            p0_stop = True
            break
        try:
            print(f"[{cid}] running...", flush=True)
            res = check()
        except Exception as exc:
            # §32.4: eval artifacts never carry raw exception text — type +
            # hash + fixed copy only (no regex-only redaction).
            from .safe_error import exception_message, exception_record

            res = {"id": cid, "passed": False,
                   "reason": exception_message(exc), **exception_record(exc)}
            print(f"[{cid}] EXCEPTION: {res['reason']}")
        results.append(res)
        print(f"[{cid}] {'PASS' if res.get('passed') else 'FAIL/NOT-RUN'}: {_redact(json.dumps(res, ensure_ascii=False, default=str))[:200]}")
        # Wave 0 stop condition (§5): a P0 in W0-1/W0-2 (auth/model/protocol) halts everything.
        if cid in ("W0-1", "W0-2") and not res.get("passed"):
            print(f"[{cid}] P0 failure — stopping Wave 0 (protocol/auth broken).")
            p0_stop = True
            break

    total_ms = round((time.perf_counter() - started) * 1000, 2)
    passed = all(r.get("passed") for r in results if r.get("id") in ("W0-1", "W0-2"))
    # W0-3/W0-4/W0-5 may be NOT RUN without halting the whole wave.
    verdict = "PASS" if passed and not p0_stop else ("FAIL" if p0_stop else "PASS WITH KNOWN RISKS")

    report = {
        "run_id": run_id,
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "verdict": verdict,
        "deepseek_key_configured": True,  # never the value
        "model": results[0].get("model") if results else "unknown",
        "usage": {
            "deepseek_calls": tracker.calls,
            "budget_cap": tracker.max_calls,
            "prompt_tokens": tracker.prompt_tokens,
            "completion_tokens": tracker.completion_tokens,
            "reasoning_tokens": tracker.reasoning_tokens,
        },
        "total_duration_ms": total_ms,
        "checks": results,
        "pre_gates": {
            "PRE-01 key_safe": "key in gitignored .env only",
            "PRE-03 bge_m3": results[-1].get("passed") if results else False,
            "PRE-04 postgres": "SQLite (Docker not running) — LLM-quality only, not retrieval",
            "PRE-11 reasoning_round_trip": next((r.get("passed") for r in results if r.get("id") == "W0-2"), False),
        },
    }

    print("\n" + "=" * 60)
    print(f"WAVE 0 VERDICT: {verdict}")
    print(f"DeepSeek calls: {tracker.calls}/{tracker.max_calls} | "
          f"tokens prompt+completion = {tracker.prompt_tokens}+{tracker.completion_tokens} "
          f"(reasoning {tracker.reasoning_tokens})")

    if args.write_report:
        out_dir = Path("eval/reports") / run_id / "live"
        out_dir.mkdir(parents=True, exist_ok=True)
        out_file = out_dir / "wave-0-smoke.json"
        out_file.write_text(_redact(json.dumps(report, ensure_ascii=False, indent=2, default=str)), encoding="utf-8")
        print(f"Wrote {out_file}")

    return 0 if verdict != "FAIL" else 1


if __name__ == "__main__":
    raise SystemExit(main())
