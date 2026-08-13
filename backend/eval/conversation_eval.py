"""Multi-turn conversation evaluation harness (product & agent manual §5.5/§6.6).

The runtime supports multi-turn via ``ChatAgentLoop.run(message, history)`` and
session-scoped history, but until now no evaluation exercised more than a single
turn. This module defines the conversation-case format and a *deterministic*
driver so the multi-turn plumbing can be validated offline (no live model).

What this scaffold CAN verify deterministically:
    - The driver correctly advances through turns.
    - Each turn's tool trajectory matches expectations under the deterministic
      router (``use_llm=False``).
    - The same project/session is reused across turns.

What it CANNOT verify (needs the real-model release gate, manual §7 Phase 4):
    - Reference resolution ("第二篇呢？") by the LLM.
    - Constraint retention across 5+ turns.
    - Correction adoption after user feedback.
    - Goal completion at the conversation level.

Those LLM-dependent behaviours are left to ``evaluate_interactive`` against a
running backend with a DeepSeek key; see ``docs/internal/gate-runbook.md``.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class ConversationTurn:
    """A single turn within a multi-turn conversation case."""

    message: str
    # Tools that MUST be called on this turn (gold). Empty = no constraint.
    expect_tools: tuple[str, ...] = ()
    # Tools that MUST NOT be called on this turn.
    forbid_tools: tuple[str, ...] = ()
    # When True, the agent should NOT call search on this turn (manual §6.6
    # "Unnecessary Re-search"): used to assert it reuses existing evidence.
    expect_no_search: bool = False


@dataclass(frozen=True)
class ConversationCase:
    """A scripted multi-turn conversation (manual §5.5)."""

    id: str
    description: str
    turns: tuple[ConversationTurn, ...]


# A representative subset of the manual's §5.5 scripts. These run under the
# deterministic router, so each turn's expected tools mirror what the router
# would choose for that single message. The point is to prove the multi-turn
# driver and the session-reuse plumbing, not LLM understanding.
CONVERSATION_CASES: tuple[ConversationCase, ...] = (
    ConversationCase(
        id="quantity_followup",
        description="「再找 3 篇」应继承主题继续检索（手册 §5.5 UX-M02）",
        turns=(
            ConversationTurn(
                message="检索 DBLP 中 Mamba 的后续工作并加入项目。",
                expect_tools=("search_papers", "add_papers_to_project"),
            ),
            ConversationTurn(
                message="再找 3 篇相关工作。",
                expect_tools=("search_papers", "add_papers_to_project"),
            ),
        ),
    ),
    ConversationCase(
        id="narrow_constraint",
        description="「只看 2023 年后的」应继续遵守新约束检索（手册 §5.5 UX-M03）",
        turns=(
            ConversationTurn(
                message="列出当前项目论文库。",
                expect_tools=("list_project_papers",),
            ),
            ConversationTurn(
                message="只看 2023 年后的工作，再检索一批。",
                expect_tools=("search_papers", "add_papers_to_project"),
            ),
        ),
    ),
    ConversationCase(
        id="reuse_evidence",
        description="已有证据时不应重新外搜（手册 §6.6 Unnecessary Re-search）",
        turns=(
            ConversationTurn(
                message="基于项目库回答：Mamba 有什么特点？",
                expect_tools=("query_project_rag",),
                expect_no_search=True,
            ),
            ConversationTurn(
                message="再补充一点它的局限。",
                expect_tools=("query_project_rag",),
                expect_no_search=True,
            ),
        ),
    ),
    ConversationCase(
        id="qa_to_artifact",
        description="从问答到产物：把刚才结论写成 related work（手册 §5.5 UX-M06）",
        turns=(
            ConversationTurn(
                message="基于项目库回答：Mamba 的核心方法是什么？",
                expect_tools=("query_project_rag",),
            ),
            ConversationTurn(
                message="把刚才的结论写成一个 related work 草稿。",
                expect_tools=("query_project_rag", "draft_report_section"),
            ),
        ),
    ),
)


async def run_conversation_case(
    case: ConversationCase,
    project_id: int,
    *,
    use_llm: bool = False,
) -> dict[str, Any]:
    """Drive one conversation case turn-by-turn against a project.

    Reuses a single ``ProjectAgentHarness`` + session per conversation so that
    session history (the last 8 messages) carries across turns — this is what
    makes a "conversation" rather than independent single-shot calls.
    """
    # Imported lazily so this module can be imported without Django configured
    # (e.g. for unit-testing the case dataclass shapes).
    from agent.harness import ProjectAgentHarness

    # Offline isolation: use the fixture tool executor when not live (avoid real
    # external source calls in deterministic tests).
    tool_executor = None
    if not use_llm:
        from eval.agent_harness import _offline_tool_executor
        tool_executor = _offline_tool_executor

    harness = ProjectAgentHarness(project_id, use_llm=use_llm, tool_executor=tool_executor)
    turn_results: list[dict[str, Any]] = []
    for index, turn in enumerate(case.turns):
        run = await harness.run(turn.message)
        events = run.get("events", [])
        tools = [
            ev["data"].get("name")
            for ev in events
            if ev.get("event") == "tool_call" and isinstance(ev.get("data"), dict)
        ]
        missing = [t for t in turn.expect_tools if t not in tools]
        forbidden = [t for t in turn.forbid_tools if t in tools]
        searched = "search_papers" in tools
        passed = (not missing) and (not forbidden) and not (turn.expect_no_search and searched)
        turn_results.append({
            "turn": index + 1,
            "message": turn.message,
            "expect_tools": list(turn.expect_tools),
            "forbid_tools": list(turn.forbid_tools),
            "actual_tools": tools,
            "missing_tools": missing,
            "forbidden_tools_observed": forbidden,
            "expect_no_search": turn.expect_no_search,
            "searched": searched,
            "answer_preview": (run.get("answer") or "").strip()[:200],
            "passed": passed,
        })
    passed = all(t["passed"] for t in turn_results)
    return {"id": case.id, "description": case.description, "passed": passed, "turns": turn_results}


def run_conversation_eval_sync(project_id: int, *, use_llm: bool = False) -> dict[str, Any]:
    """Synchronous entry point: run all CONVERSATION_CASES against a project.

    Offline isolation (manual §3.2): when ``use_llm=False`` we force the fake
    embedding provider for the duration of the run so the deterministic path can
    never accidentally hit real BGE-M3 or external sources. The live ReAct path
    (``use_llm=True``) intentionally leaves the provider as-configured.
    """
    import os

    saved_provider = os.environ.get("PAPERLENS_EMBEDDING_PROVIDER")
    try:
        if not use_llm:
            os.environ["PAPERLENS_EMBEDDING_PROVIDER"] = "fake"
        results = [asyncio.run(run_conversation_case(case, project_id, use_llm=use_llm)) for case in CONVERSATION_CASES]
    finally:
        if saved_provider is None:
            os.environ.pop("PAPERLENS_EMBEDDING_PROVIDER", None)
        else:
            os.environ["PAPERLENS_EMBEDDING_PROVIDER"] = saved_provider
    passed = all(r["passed"] for r in results)
    return {
        "passed": passed,
        "case_count": len(results),
        "passed_cases": sum(1 for r in results if r["passed"]),
        "total_turns": sum(len(r["turns"]) for r in results),
        "cases": results,
    }
