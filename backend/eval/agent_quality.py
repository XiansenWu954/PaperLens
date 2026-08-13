"""Resume-grade quality metrics for the PaperLens Agent core.

This module aggregates intent, tool trajectory, grounding, harness,
prompt-contract, MCP, and data-source checks into one machine-readable report.
It is intentionally deterministic by default so the resume/demo metrics do not
depend on live network availability.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import Counter
from typing import Any

from eval.agent_harness import run_project_agent_eval_sync, tool_policy_summary
from eval.intent_eval import evaluate_intent_classifier
from eval.tool_metrics import aggregate_tool_metrics

logger = logging.getLogger(__name__)


def run_agent_quality_eval(project_id: int, include_network: bool = False) -> dict[str, Any]:
    started = time.perf_counter()
    logger.info(
        "agent quality evaluation started",
        extra={
            "event": "agent_quality_evaluation_started",
            "project_id": project_id,
            "include_network": include_network,
        },
    )

    intent_eval = evaluate_intent_classifier(project_id=project_id)
    harness_eval = run_project_agent_eval_sync(project_id, include_network=include_network)
    prompt_contracts = prompt_contract_summary()
    mcp_contracts = mcp_contract_summary()
    data_sources = data_source_summary()
    timeout_recovery = timeout_recovery_summary(project_id)

    metrics = {
        "intent_routing": intent_metrics(intent_eval),
        "function_calling": function_calling_metrics(intent_eval, harness_eval),
        "rag_grounding": rag_grounding_metrics(harness_eval),
        "prompt_engineering": prompt_contracts,
        "execution_harness": harness_metrics(harness_eval, timeout_recovery),
        "mcp_surface": mcp_contracts,
        "data_sources": data_sources,
    }
    score = overall_score(metrics)
    passed = all(section.get("passed", False) for section in metrics.values())
    duration_ms = round((time.perf_counter() - started) * 1000, 2)
    result = {
        "passed": passed,
        "score": score,
        "duration_ms": duration_ms,
        "metrics": metrics,
        "raw": {
            "intent_eval": intent_eval,
            "project_agent_eval": harness_eval,
            "timeout_recovery": timeout_recovery,
        },
    }

    logger.info(
        "agent quality evaluation completed",
        extra={
            "event": "agent_quality_evaluation_completed",
            "project_id": project_id,
            "include_network": include_network,
            "status": "passed" if passed else "failed",
            "score": score,
            "duration_ms": duration_ms,
            "intent_accuracy": metrics["intent_routing"]["intent_accuracy"],
            "tool_trajectory_accuracy": metrics["function_calling"]["tool_trajectory_accuracy"],
            "grounded_answer_rate": metrics["rag_grounding"]["grounded_answer_rate"],
            "mcp_safe_surface": metrics["mcp_surface"]["safe_surface"],
        },
    )
    return result


def intent_metrics(result: dict[str, Any]) -> dict[str, Any]:
    cases = result["cases"]
    blocked_cases = [case for case in cases if case["expected_blocked"]]
    tool_cases = [case for case in cases if case["expected_tools"]]
    return {
        "passed": result["passed"],
        "total_cases": result["total"],
        "passed_cases": result["passed_count"],
        "intent_accuracy": _rate(result["passed_count"], result["total"]),
        "tool_plan_exact_match_rate": _rate(
            sum(1 for case in tool_cases if case["actual_tools"] == case["expected_tools"]),
            len(tool_cases),
        ),
        "destructive_intent_block_rate": _rate(
            sum(1 for case in blocked_cases if case["actual_blocked"]),
            len(blocked_cases),
        ),
        "failed_cases": [case["id"] for case in result["failed"]],
    }


def function_calling_metrics(intent_eval: dict[str, Any], harness_eval: dict[str, Any]) -> dict[str, Any]:
    policy = harness_eval["policy"]
    cases = harness_eval["cases"]
    tool_cases = [case for case in cases if case["expected_tools"]]
    trajectory_passes = [
        case
        for case in tool_cases
        if not case["missing_tools"] and not case["forbidden_tools_observed"]
    ]
    tool_result_events = sum(case["events"].count("tool_result") for case in cases)
    tool_call_events = sum(case["events"].count("tool_call") for case in cases)
    search_expand = next(
        (case for case in intent_eval["cases"] if case["id"] == "search_expand_cn"),
        None,
    )
    # §6.2 tool-decision indicators, aggregated across the deterministic cases.
    tool_decision = aggregate_tool_metrics(cases)
    return {
        "passed": policy["passed"] and len(trajectory_passes) == len(tool_cases),
        "available_tools": policy["tools"],
        "destructive_tools_exposed": policy["destructive_tools_exposed"],
        "safe_tool_policy": policy["passed"],
        "tool_trajectory_cases": len(tool_cases),
        "tool_trajectory_passed": len(trajectory_passes),
        "tool_trajectory_accuracy": _rate(len(trajectory_passes), len(tool_cases)),
        "tool_result_event_rate": _rate(tool_result_events, tool_call_events),
        "search_expand_routes_to_tools": bool(search_expand and search_expand["passed"]),
        # Manual §6.2 tool-decision metrics (thresholds in the test manual).
        "tool_selection_precision": tool_decision["tool_selection_precision"],
        "tool_selection_recall": tool_decision["tool_selection_recall"],
        "ordering_accuracy": tool_decision["ordering_accuracy"],
        "argument_validity": tool_decision["argument_validity"],
        "redundant_call_rate": tool_decision["redundant_call_rate"],
        "loop_exhaustion_rate": tool_decision["loop_exhaustion_rate"],
    }


def rag_grounding_metrics(harness_eval: dict[str, Any]) -> dict[str, Any]:
    cases = harness_eval["cases"]
    evidence_required = [case for case in cases if case["evidence_count"] > 0 or "evidence" in case["events"]]
    # Task 4.x: capability-policy fail-closed abstentions (safety_replaced) are
    # COMPLIANT outcomes, not grounding failures — excluded from source_required.
    source_required = [
        case for case in cases
        if (case["source_marker_present"] or case["evidence_count"] > 0)
        and not case.get("safety_replaced")
    ]
    verdicts = Counter(case["quality_verdict"] for case in cases)
    grounded = verdicts.get("grounded", 0)
    partial = verdicts.get("partial", 0)
    evidence_total = sum(case["evidence_count"] for case in cases)
    return {
        "passed": partial == 0 and all(case["source_marker_present"] for case in source_required),
        "evidence_cases": len(evidence_required),
        "evidence_present_rate": _rate(sum(1 for case in evidence_required if case["evidence_count"] > 0), len(evidence_required)),
        "source_marker_rate": _rate(sum(1 for case in source_required if case["source_marker_present"]), len(source_required)),
        "grounded_answer_rate": _rate(grounded, len(cases)),
        "partial_answer_rate": _rate(partial, len(cases)),
        "average_evidence_per_run": round(evidence_total / len(cases), 2) if cases else 0,
        "quality_verdicts": dict(verdicts),
    }


def harness_metrics(harness_eval: dict[str, Any], timeout_recovery: dict[str, Any]) -> dict[str, Any]:
    cases = harness_eval["cases"]
    required_event_passes = sum(1 for case in cases if not case["missing_events"])
    quality_events = sum(1 for case in cases if "quality_check" in case["events"])
    tool_errors = sum(1 for case in cases for event in case["events"] if event == "error")
    durations = [case.get("duration_ms", 0) for case in cases]
    event_counts = [case.get("event_count", len(case["events"])) for case in cases]
    return {
        "passed": required_event_passes == len(cases) and timeout_recovery["passed"],
        "required_event_coverage": _rate(required_event_passes, len(cases)),
        "quality_check_event_rate": _rate(quality_events, len(cases)),
        "timeout_recovery_passed": timeout_recovery["passed"],
        "timeout_verdict": timeout_recovery["quality_verdict"],
        "tool_error_event_count": tool_errors,
        "average_events_per_run": round(sum(event_counts) / len(event_counts), 2) if event_counts else 0,
        "average_duration_ms": round(sum(durations) / len(durations), 2) if durations else 0,
        "p95_duration_ms": percentile(durations, 0.95),
    }


def prompt_contract_summary() -> dict[str, Any]:
    from agent import prompts

    checks = [
        (
            "chat_uses_project_evidence",
            "项目论文库证据" in prompts.PROJECT_CHAT_RESPONDER_SYSTEM,
        ),
        (
            "chat_forbids_fake_pqac",
            "不能编造 pqac key" in prompts.PROJECT_CHAT_RESPONDER_SYSTEM,
        ),
        (
            "chat_blocks_autonomous_destructive_actions",
            "不能作为自主工具调用" in prompts.PROJECT_CHAT_RESPONDER_SYSTEM,
        ),
        (
            "report_evidence_only",
            "只基于输入 evidence" in prompts.PROJECT_REPORT_WRITER_SYSTEM,
        ),
        (
            "report_does_not_overwrite_versions",
            "不直接覆盖任何 ReportVersion" in prompts.PROJECT_REPORT_WRITER_SYSTEM,
        ),
        (
            "critic_outputs_strict_json",
            "输出严格 JSON" in prompts.PROJECT_CRITIC_SYSTEM,
        ),
        (
            "critic_checks_tool_policy",
            "破坏性工具" in prompts.PROJECT_CRITIC_SYSTEM,
        ),
    ]
    passed = sum(1 for _, ok in checks if ok)
    return {
        "passed": passed == len(checks),
        "passed_checks": passed,
        "total_checks": len(checks),
        "contract_coverage": _rate(passed, len(checks)),
        "checks": [{"id": name, "passed": ok} for name, ok in checks],
    }


def _mcp_tool_schema(tool: Any) -> dict[str, Any] | None:
    return getattr(tool, "input_schema", None) or getattr(tool, "inputSchema", None)


def mcp_contract_summary() -> dict[str, Any]:
    from agent.project_tools import PROJECT_AGENT_TOOLS
    from mcp_server.server import MCP_PROJECT_TOOL_NAMES, _project_mcp_tools

    function_contracts = {
        tool["function"]["name"]: tool["function"]["parameters"]
        for tool in PROJECT_AGENT_TOOLS
        if tool["function"]["name"] in MCP_PROJECT_TOOL_NAMES
    }
    mcp_tools = {tool.name: _mcp_tool_schema(tool) for tool in _project_mcp_tools()}
    destructive = {"add_papers_to_project", "draft_report_section", "delete_project_paper", "clear_project", "overwrite_report"}
    schema_drift = [
        name
        for name, schema in function_contracts.items()
        if mcp_tools.get(name) != schema
    ]
    missing = sorted(set(function_contracts) - set(mcp_tools))
    unexpected = sorted(set(mcp_tools) - set(function_contracts))
    unsafe = sorted(set(mcp_tools).intersection(destructive))
    passed = not schema_drift and not missing and not unexpected and not unsafe
    return {
        "passed": passed,
        "safe_surface": not unsafe,
        "exported_tool_count": len(mcp_tools),
        "exported_tools": sorted(mcp_tools),
        "schema_drift_count": len(schema_drift),
        "schema_drift_tools": schema_drift,
        "missing_tools": missing,
        "unexpected_tools": unexpected,
        "unsafe_tools_exposed": unsafe,
    }


def data_source_summary() -> dict[str, Any]:
    from datasources.registry import DEFAULT_SOURCES, REGISTRY

    expected_defaults = {"dblp", "openalex", "arxiv"}
    defaults = set(DEFAULT_SOURCES)
    missing_defaults = sorted(expected_defaults - defaults)
    return {
        "passed": not missing_defaults and "dblp" in REGISTRY,
        "default_sources": list(DEFAULT_SOURCES),
        "registered_sources": sorted(REGISTRY),
        "dblp_registered": "dblp" in REGISTRY,
        "dblp_default": "dblp" in defaults,
        "missing_expected_defaults": missing_defaults,
    }


def timeout_recovery_summary(project_id: int) -> dict[str, Any]:
    from agent.harness import ProjectAgentHarness

    async def slow_executor(
        _context, _name: str, _args: dict[str, Any]
    ) -> dict[str, Any]:
        await asyncio.sleep(0.05)
        return {"papers": [], "count": 0}

    result = asyncio.run(
        ProjectAgentHarness(
            project_id,
            tool_executor=slow_executor,
            tool_timeout_seconds=0.001,
        ).run("继续检索 Mamba 后续论文")
    )
    events = result["events"]
    tool_results = [event["data"] for event in events if event["event"] == "tool_result"]
    quality = next((event["data"] for event in events if event["event"] == "quality_check"), {})
    timed_out = any(item.get("error") == "tool_timeout" for item in tool_results)
    return {
        "passed": timed_out and quality.get("verdict") == "partial" and bool(result["answer"].strip()),
        "timed_out_tool_results": sum(1 for item in tool_results if item.get("error") == "tool_timeout"),
        "quality_verdict": quality.get("verdict", ""),
        "answer_chars": len(result["answer"]),
    }


def overall_score(metrics: dict[str, dict[str, Any]]) -> float:
    scores = [
        metrics["intent_routing"]["intent_accuracy"],
        metrics["function_calling"]["tool_trajectory_accuracy"],
        metrics["rag_grounding"]["source_marker_rate"],
        metrics["prompt_engineering"]["contract_coverage"],
        metrics["execution_harness"]["required_event_coverage"],
        1.0 if metrics["mcp_surface"]["passed"] else 0.0,
        1.0 if metrics["data_sources"]["passed"] else 0.0,
    ]
    return round(sum(scores) / len(scores), 4)


def percentile(values: list[float], p: float) -> float:
    if not values:
        return 0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round((len(ordered) - 1) * p)))
    return round(ordered[index], 2)


def _rate(numerator: int, denominator: int) -> float:
    if denominator <= 0:
        return 1.0
    return round(numerator / denominator, 4)


def dumps_quality_eval(result: dict[str, Any]) -> str:
    return json.dumps(result, ensure_ascii=True, indent=2, default=str)
