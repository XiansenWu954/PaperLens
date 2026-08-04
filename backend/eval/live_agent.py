"""Live DeepSeek evaluation for project Agent answer quality.

This module intentionally calls the real LLM. It is not used by the default test
suite; run it explicitly when validating demo/resume output quality.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any

from agent.harness import ProjectAgentHarness
from agent.prompts import LIVE_PROJECT_CRITIC_SYSTEM

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class LiveAgentCase:
    id: str
    message: str
    expected_tools: tuple[str, ...] = field(default_factory=tuple)
    requires_evidence: bool = True
    requires_source_marker: bool = True
    requires_network: bool = False
    min_answer_chars: int = 120
    requires_llm: bool = True
    use_critic: bool = True


LIVE_AGENT_CASES: tuple[LiveAgentCase, ...] = (
    LiveAgentCase(
        id="compare_project_papers_cn",
        message="基于项目库，对比 Mamba 和 Transformer 的核心差异，并说明各自适用场景。",
        expected_tools=("query_project_rag",),
    ),
    LiveAgentCase(
        id="limitations_cn",
        message="这些论文共同局限是什么？请给出可以继续检索的方向。",
        expected_tools=("query_project_rag", "search_papers"),
    ),
    LiveAgentCase(
        id="report_section_cn",
        message="生成一个相关工作章节草稿，主题是长序列建模方法演进。",
        expected_tools=("query_project_rag", "draft_report_section"),
        min_answer_chars=180,
    ),
    LiveAgentCase(
        id="library_reasoning_cn",
        message="列出当前项目论文，并说明哪些更适合作为核心论文。",
        expected_tools=("list_project_papers", "query_project_rag"),
        requires_evidence=True,
        requires_source_marker=True,
        min_answer_chars=80,
    ),
    LiveAgentCase(
        id="search_add_rag_eval_cn",
        message="找 2 篇 retrieval augmented generation 评测论文并加入项目库，然后说明为什么值得加入。",
        expected_tools=("search_papers", "add_papers_to_project", "query_project_rag"),
        requires_network=True,
        min_answer_chars=160,
    ),
    LiveAgentCase(
        id="expand_mamba_scope_cn",
        message="继续扩大 Mamba 后续工作范围，优先补充 DBLP/OpenAlex 能找到的论文。",
        expected_tools=("search_papers", "add_papers_to_project", "query_project_rag"),
        requires_network=True,
        min_answer_chars=160,
    ),
    LiveAgentCase(
        id="graph_explain_cn",
        message="展示当前项目引用图谱，并解释这些论文之间有什么关系。",
        expected_tools=("get_project_citation_graph",),
        requires_evidence=False,
        requires_source_marker=False,
        min_answer_chars=80,
        requires_llm=False,
        use_critic=False,
    ),
    LiveAgentCase(
        id="method_term_mamba_cn",
        message="解释 selective state space model 在 Mamba 中主要解决什么问题，请基于项目库回答。",
        expected_tools=("query_project_rag",),
        min_answer_chars=120,
    ),
    LiveAgentCase(
        id="report_limitations_cn",
        message="基于项目证据生成一个关于长序列建模局限性的报告小节。",
        expected_tools=("query_project_rag", "draft_report_section"),
        min_answer_chars=180,
    ),
    LiveAgentCase(
        id="rag_eval_search_directions_cn",
        message="这些 RAG 评测论文还能继续检索哪些方向？",
        expected_tools=("query_project_rag", "search_papers"),
        requires_network=True,
        min_answer_chars=140,
    ),
)


def run_live_agent_eval(
    project_id: int,
    *,
    include_network: bool = False,
    max_cases: int | None = None,
    use_critic: bool = True,
) -> dict[str, Any]:
    started = time.perf_counter()
    selected = [
        case for case in LIVE_AGENT_CASES
        if include_network or not case.requires_network
    ]
    if max_cases:
        selected = selected[:max_cases]
    logger.info(
        "live agent evaluation started",
        extra={
            "event": "live_agent_evaluation_started",
            "project_id": project_id,
            "case_count": len(selected),
            "include_network": include_network,
        },
    )
    cases = [
        _run_live_case(project_id, case, use_critic=use_critic)
        for case in selected
    ]
    passed_count = sum(1 for case in cases if case["passed"])
    average_score = _average(
        [case["critic"].get("score", 0) for case in cases if case.get("critic")]
    )
    result = {
        "passed": passed_count == len(cases),
        "case_count": len(cases),
        "passed_count": passed_count,
        "average_critic_score": average_score,
        "duration_ms": round((time.perf_counter() - started) * 1000, 2),
        "cases": cases,
    }
    logger.info(
        "live agent evaluation completed",
        extra={
            "event": "live_agent_evaluation_completed",
            "project_id": project_id,
            "case_count": len(cases),
            "passed_count": passed_count,
            "average_critic_score": average_score,
            "duration_ms": result["duration_ms"],
            "status": "passed" if result["passed"] else "failed",
        },
    )
    return result


def _run_live_case(project_id: int, case: LiveAgentCase, *, use_critic: bool) -> dict[str, Any]:
    started = time.perf_counter()
    result = asyncio.run(
        ProjectAgentHarness(
            project_id,
            use_llm=True,
            tool_timeout_seconds=30.0 if case.requires_network else 15.0,
        ).run(case.message)
    )
    events = result["events"]
    answer = result["answer"].strip()
    event_names = [event["event"] for event in events]
    tools = [
        event["data"].get("name")
        for event in events
        if event["event"] == "tool_call" and isinstance(event.get("data"), dict)
    ]
    missing_tools = [name for name in case.expected_tools if name not in tools]
    forbidden_tools = [
        name for name in tools
        if name in {"delete_project_paper", "clear_project", "overwrite_report", "delete_project"}
    ]
    evidence = _collect_evidence(events)
    tool_artifacts = _collect_tool_artifacts(events)
    quality = next((event["data"] for event in events if event["event"] == "quality_check"), {})
    llm_result = next((event["data"] for event in events if event["event"] == "llm_result"), {})
    source_marker_present = _answer_has_source_marker(answer, evidence)
    critic = _evaluate_structured_case(case, answer, tool_artifacts) if not case.use_critic else (
        _critic_case(case, answer, evidence, tools, quality, tool_artifacts) if use_critic else {}
    )
    checks = {
        "answer_min_length": len(answer) >= case.min_answer_chars,
        "llm_result_ok": llm_result.get("status") == "ok" if case.requires_llm else llm_result.get("status") in {None, "ok"},
        "expected_tools_present": not missing_tools,
        "no_forbidden_tools": not forbidden_tools,
        "evidence_present": len(evidence) > 0 if case.requires_evidence else True,
        "source_marker_present": source_marker_present if case.requires_source_marker else True,
        "harness_quality_acceptable": quality.get("verdict") in {"grounded", "needs_more_evidence"},
        "critic_passed": bool(critic.get("passed", True)) if (use_critic or not case.use_critic) else True,
    }
    passed = all(checks.values())
    logger.info(
        "live agent case completed",
        extra={
            "event": "live_agent_case_completed",
            "project_id": project_id,
            "case_id": case.id,
            "status": "passed" if passed else "failed",
            "answer_chars": len(answer),
            "evidence_count": len(evidence),
            "tool_count": len(tools),
            "critic_score": critic.get("score"),
        },
    )
    return {
        "id": case.id,
        "passed": passed,
        "message": case.message,
        "answer": answer,
        "answer_chars": len(answer),
        "events": event_names,
        "tools": tools,
        "expected_tools": list(case.expected_tools),
        "missing_tools": missing_tools,
        "forbidden_tools": forbidden_tools,
        "evidence_count": len(evidence),
        "source_marker_present": source_marker_present,
        "quality": quality,
        "llm_result": llm_result,
        "tool_artifacts": tool_artifacts,
        "critic": critic,
        "checks": checks,
        "duration_ms": round((time.perf_counter() - started) * 1000, 2),
    }


def _evaluate_structured_case(
    case: LiveAgentCase,
    answer: str,
    tool_artifacts: dict[str, Any],
) -> dict[str, Any]:
    if case.id == "graph_explain_cn":
        return _evaluate_graph_case(answer, tool_artifacts)
    return {
        "passed": True,
        "score": 1.0,
        "grounding": 1.0,
        "usefulness": 1.0,
        "citation_integrity": 1.0,
        "tool_use": 1.0,
        "issues": [],
        "recommendation": "structured case checked without LLM critic",
    }


def _evaluate_graph_case(answer: str, tool_artifacts: dict[str, Any]) -> dict[str, Any]:
    graph = tool_artifacts.get("graph") or {}
    nodes = graph.get("nodes") or []
    edges = graph.get("edges") or []
    node_count = int(graph.get("node_count") or 0)
    edge_count = int(graph.get("edge_count") or 0)
    lower_answer = answer.lower()

    has_counts = str(node_count) in answer and str(edge_count) in answer
    has_node_detail = any(str(node.get("title") or "") in answer for node in nodes[:5])
    if edge_count:
        has_edge_detail = any(
            str(edge.get("source_title") or "") in answer
            and str(edge.get("target_title") or "") in answer
            for edge in edges[:5]
        )
        has_basis = "referenced_works" in lower_answer or "shared reference" in lower_answer
    else:
        has_edge_detail = True
        has_basis = "no edge" in lower_answer or "referenced_works" in lower_answer

    checks = {
        "has_graph_nodes": node_count > 0,
        "has_counts": has_counts,
        "has_node_detail": has_node_detail,
        "has_edge_detail": has_edge_detail,
        "has_basis": has_basis,
    }
    passed = all(checks.values())
    return {
        "passed": passed,
        "score": 0.95 if passed else 0.55,
        "grounding": 1.0 if passed else 0.5,
        "usefulness": 0.9 if passed else 0.5,
        "citation_integrity": 1.0,
        "tool_use": 1.0 if node_count else 0.0,
        "issues": [name for name, ok in checks.items() if not ok],
        "recommendation": (
            "graph answer is grounded in citation graph artifacts"
            if passed
            else "include graph counts, representative nodes, edge titles, and referenced_works basis"
        ),
    }


def _critic_case(
    case: LiveAgentCase,
    answer: str,
    evidence: list[dict[str, Any]],
    tools: list[str],
    quality: dict[str, Any],
    tool_artifacts: dict[str, Any],
) -> dict[str, Any]:
    from llm.deepseek import DeepSeekClient

    client = DeepSeekClient(max_retries=1)
    payload = {
        "case_id": case.id,
        "user_message": case.message,
        "expected_tools": case.expected_tools,
        "actual_tools": tools,
        "quality": quality,
        "tool_artifacts": tool_artifacts,
        "evidence": evidence[:8],
        "answer": answer[:4000],
    }
    response = client.complete(
        [
            {"role": "system", "content": LIVE_PROJECT_CRITIC_SYSTEM},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
        ],
        thinking=False,
        temperature=0.0,
        max_tokens=700,
        response_format={"type": "json_object"},
    )
    return _parse_json_object(response.get("content", ""))


def _collect_evidence(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    evidence: list[dict[str, Any]] = []
    for event in events:
        if event["event"] != "evidence":
            continue
        data = event.get("data") or {}
        if isinstance(data, dict):
            evidence.extend(data.get("evidence") or [])
    return evidence


def _collect_tool_artifacts(events: list[dict[str, Any]]) -> dict[str, Any]:
    artifacts: dict[str, Any] = {
        "added_papers": [],
        "searched_papers": [],
        "search_result_count": 0,
        "graph": {},
    }
    for event in events:
        data = event.get("data") or {}
        if event["event"] == "paper_added" and isinstance(data, dict):
            artifacts["added_papers"] = [
                {"title": item.get("title"), "paper_id": item.get("paper_id"), "created": item.get("created")}
                for item in (data.get("added") or [])[:10]
            ]
            artifacts["added_count"] = data.get("count", len(artifacts["added_papers"]))
        elif event["event"] == "search_results" and isinstance(data, dict):
            artifacts["searched_papers"] = [
                {
                    "title": item.get("title"),
                    "year": item.get("year"),
                    "source": item.get("source"),
                    "venue": item.get("venue"),
                }
                for item in (data.get("papers") or [])[:10]
            ]
            artifacts["search_result_count"] = data.get("count", len(artifacts["searched_papers"]))
        elif event["event"] == "tool_result" and isinstance(data, dict) and data.get("name") == "search_papers":
            artifacts["search_result_count"] = data.get("count", 0)
            if data.get("status") == "error":
                artifacts["search_error"] = data.get("error")
        elif event["event"] == "graph" and isinstance(data, dict):
            nodes = data.get("nodes") or []
            edges = data.get("edges") or []
            title_by_id = {str(node.get("id")): node.get("title") for node in nodes}
            artifacts["graph"] = {
                "node_count": len(nodes),
                "edge_count": len(edges),
                "nodes": [
                    {
                        "id": node.get("id"),
                        "title": node.get("title"),
                        "year": node.get("year"),
                        "is_root": node.get("is_root"),
                        "is_frontier": node.get("is_frontier"),
                    }
                    for node in nodes[:10]
                ],
                "edges": [
                    {
                        "source_title": title_by_id.get(str(edge.get("source")), str(edge.get("source"))),
                        "target_title": title_by_id.get(str(edge.get("target")), str(edge.get("target"))),
                        "weight": edge.get("weight", 1),
                    }
                    for edge in edges[:10]
                ],
            }
    return artifacts


def _answer_has_source_marker(answer: str, evidence: list[dict[str, Any]]) -> bool:
    lowered = answer.lower()
    for item in evidence:
        for key in ("source_marker", "citation", "title", "docname"):
            marker = str(item.get(key) or "").strip()
            if marker and marker.lower() in lowered:
                return True
    return False


def _parse_json_object(text: str) -> dict[str, Any]:
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, re.S)
        if not match:
            return {
                "passed": False,
                "score": 0,
                "issues": ["critic returned non-JSON output"],
                "recommendation": text[:240],
            }
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError:
            return {
                "passed": False,
                "score": 0,
                "issues": ["critic returned invalid JSON object"],
                "recommendation": text[:240],
            }


def _average(values: list[float]) -> float:
    if not values:
        return 0.0
    return round(sum(values) / len(values), 4)


def dumps_live_agent_eval(result: dict[str, Any]) -> str:
    return json.dumps(result, ensure_ascii=True, indent=2, default=str)
