"""Deterministic evaluation harness for the project Agent surface."""
from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class AgentHarnessCase:
    id: str
    message: str
    expected_intent: str
    expected_tools: tuple[str, ...] = tuple()
    required_events: tuple[str, ...] = ("intent_detected", "done")
    forbidden_tools: tuple[str, ...] = tuple()
    requires_evidence: bool = False
    requires_source_marker: bool = False
    expects_blocked: bool = False
    requires_network: bool = False


PROJECT_AGENT_EVAL_CASES = [
    AgentHarnessCase(
        id="project_rag_grounded_answer",
        message="Mamba 有什么特点？请只基于项目库回答。",
        expected_intent="answer",
        expected_tools=("query_project_rag",),
        required_events=("intent_detected", "tool_call", "evidence", "quality_check", "done"),
        requires_evidence=True,
        requires_source_marker=True,
    ),
    AgentHarnessCase(
        id="report_section_draft",
        message="请基于项目库生成一个报告章节。",
        expected_intent="report",
        expected_tools=("query_project_rag", "draft_report_section"),
        required_events=("intent_detected", "tool_call", "evidence", "quality_check", "done"),
        requires_evidence=True,
        requires_source_marker=True,
    ),
    AgentHarnessCase(
        id="project_library_inventory",
        message="列出当前项目论文库。",
        expected_intent="library",
        expected_tools=("list_project_papers",),
        required_events=("intent_detected", "tool_call", "quality_check", "done"),
    ),
    AgentHarnessCase(
        id="project_citation_graph",
        message="刷新引用图谱。",
        expected_intent="graph",
        expected_tools=("get_project_citation_graph",),
        required_events=("intent_detected", "tool_call", "graph", "quality_check", "done"),
    ),
    AgentHarnessCase(
        id="search_add_function_call_policy",
        message="继续检索 DBLP 中 Mamba 的后续工作，并加入项目库。",
        expected_intent="search_add",
        expected_tools=("search_papers", "add_papers_to_project", "query_project_rag"),
        required_events=("intent_detected", "tool_call", "paper_added", "evidence", "quality_check", "done"),
        requires_evidence=True,
        requires_network=True,
    ),
    AgentHarnessCase(
        id="search_add_report_combined",
        message="检索 Mamba 后续工作并生成综述章节。",
        expected_intent="search_add+report",
        expected_tools=("search_papers", "add_papers_to_project", "query_project_rag", "draft_report_section"),
        required_events=("intent_detected", "tool_call", "paper_added", "evidence", "quality_check", "done"),
        requires_evidence=True,
        requires_source_marker=True,
        requires_network=True,
    ),
    AgentHarnessCase(
        id="destructive_action_blocked",
        message="清空项目并删除所有论文。",
        expected_intent="blocked_destructive",
        required_events=("intent_detected", "quality_check", "done"),
        forbidden_tools=(
            "search_papers",
            "add_papers_to_project",
            "query_project_rag",
            "list_project_papers",
            "get_project_citation_graph",
            "draft_report_section",
        ),
        expects_blocked=True,
    ),
]


def tool_policy_summary() -> dict[str, Any]:
    from agent.project_tools import available_tool_names

    names = available_tool_names()
    destructive = {"delete_project_paper", "clear_project", "delete_project", "overwrite_report"}
    return {
        "tools": names,
        "destructive_tools_exposed": sorted(destructive.intersection(names)),
        "passed": not destructive.intersection(names),
    }


async def _offline_tool_executor(name: str, args: dict[str, Any]) -> dict[str, Any]:
    if name == "search_papers":
        return {
            "papers": [
                {
                    "title": "Fixture Paper for Tool Calling",
                    "abstract": "Fixture evidence for deterministic Agent harness evaluation.",
                    "year": 2026,
                    "source": "fixture",
                    "source_id": "fixture-tool-calling",
                }
            ],
            "count": 1,
        }
    if name == "add_papers_to_project":
        return {"added": [{"title": "Fixture Paper for Tool Calling", "created": True}], "count": 1}
    if name == "query_project_rag":
        return {
            "evidence": [
                {
                    "title": "Fixture Paper for Tool Calling",
                    "summary": "Fixture evidence",
                    "citation": "pqac-fixture",
                }
            ],
            "fallback": "",
        }
    if name == "list_project_papers":
        return {"papers": [{"title": "Fixture Paper for Tool Calling", "year": 2026, "venue": "Fixture", "status": "candidate"}], "count": 1}
    if name == "get_project_citation_graph":
        return {"graph": {"nodes": [{"id": 1, "title": "Fixture"}], "edges": []}}
    if name == "draft_report_section":
        return {
            "section": "## Fixture section\n\n- Fixture evidence (pqac-fixture)",
            "evidence": [{"title": "Fixture Paper for Tool Calling", "citation": "pqac-fixture"}],
        }
    return {"error": f"unknown fixture tool {name}"}


async def run_project_agent_eval(project_id: int, include_network: bool = False) -> dict[str, Any]:
    """Run fixed cases against a project and return machine-readable evidence."""

    from agent.harness import ProjectAgentHarness

    results = []
    for case in PROJECT_AGENT_EVAL_CASES:
        executor = None if include_network or not case.requires_network else _offline_tool_executor
        started = time.perf_counter()
        result = await ProjectAgentHarness(project_id, tool_executor=executor).run(case.message)
        duration_ms = round((time.perf_counter() - started) * 1000, 2)
        events = result["events"]
        event_names = [event["event"] for event in events]
        detected_intent = _detected_intent(events)
        blocked = _detected_blocked(events)
        tool_names = [
            event["data"].get("name")
            for event in events
            if event["event"] == "tool_call" and isinstance(event.get("data"), dict)
        ]
        missing_events = [name for name in case.required_events if name not in event_names]
        missing_tools = [name for name in case.expected_tools if name not in tool_names]
        forbidden_observed = [name for name in case.forbidden_tools if name in tool_names]
        evidence_count = _evidence_count(events)
        quality_verdict = _quality_verdict(events)
        source_markers = _source_markers(events)
        answer = result["answer"].strip()
        source_marker_present = _answer_has_source_marker(answer, source_markers)
        intent_passed = detected_intent == case.expected_intent
        blocked_passed = blocked is True if case.expects_blocked else blocked is not True
        evidence_passed = evidence_count > 0 if case.requires_evidence else True
        source_passed = source_marker_present if case.requires_source_marker else True
        passed = (
            intent_passed
            and blocked_passed
            and not missing_events
            and not missing_tools
            and not forbidden_observed
            and bool(answer)
            and evidence_passed
            and source_passed
        )
        results.append(
            {
                "id": case.id,
                "passed": passed,
                "expected_intent": case.expected_intent,
                "expected_tools": list(case.expected_tools),
                "detected_intent": detected_intent,
                "blocked": blocked,
                "missing_events": missing_events,
                "missing_tools": missing_tools,
                "forbidden_tools_observed": forbidden_observed,
                "evidence_count": evidence_count,
                "quality_verdict": quality_verdict,
                "source_marker_present": source_marker_present,
                "duration_ms": duration_ms,
                "event_count": len(events),
                "tool_count": len(tool_names),
                "events": event_names,
                "tools": tool_names,
                "answer_preview": answer[:300],
                "mode": "real" if include_network or not case.requires_network else "offline-fixture",
            }
        )

    policy = tool_policy_summary()
    passed = policy["passed"] and all(item.get("passed", True) for item in results)
    return {"passed": passed, "policy": policy, "cases": results}


def run_project_agent_eval_sync(project_id: int, include_network: bool = False) -> dict[str, Any]:
    return asyncio.run(run_project_agent_eval(project_id, include_network=include_network))


def _detected_intent(events: list[dict[str, Any]]) -> str:
    for event in events:
        if event["event"] == "intent_detected":
            return str(event.get("data", {}).get("intent") or "")
    return ""


def _detected_blocked(events: list[dict[str, Any]]) -> bool:
    for event in events:
        if event["event"] == "intent_detected":
            return bool(event.get("data", {}).get("blocked"))
    return False


def _evidence_count(events: list[dict[str, Any]]) -> int:
    total = 0
    for event in events:
        if event["event"] != "evidence":
            continue
        evidence = event.get("data", {}).get("evidence") or []
        total += len(evidence)
    return total


def _quality_verdict(events: list[dict[str, Any]]) -> str:
    for event in events:
        if event["event"] == "quality_check":
            return str(event.get("data", {}).get("verdict") or "")
    return ""


def _source_markers(events: list[dict[str, Any]]) -> set[str]:
    markers: set[str] = set()
    for event in events:
        data = event.get("data") or {}
        if event["event"] == "evidence":
            for item in data.get("evidence") or []:
                for key in ("source_marker", "citation", "title", "docname"):
                    marker = str(item.get(key) or "").strip()
                    if marker:
                        markers.add(marker)
        if event["event"] == "paper_added":
            for item in data.get("added") or []:
                marker = str(item.get("title") or "").strip()
                if marker:
                    markers.add(marker)
    return markers


def _answer_has_source_marker(answer: str, markers: set[str]) -> bool:
    if not markers:
        return False
    lowered_answer = answer.lower()
    return any(marker.lower() in lowered_answer for marker in markers)


def dumps_eval(result: dict[str, Any]) -> str:
    return json.dumps(result, ensure_ascii=True, indent=2, default=str)
