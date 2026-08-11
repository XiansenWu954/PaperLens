"""Tool-decision metric primitives for the Agent surface.

Implements the indicators defined in the product & agent test manual §6.2.
These are pure functions over the per-case result dicts produced by
``eval.agent_harness.run_project_agent_eval_sync`` so they can be unit-tested
without a model, a network, or a database.
"""
from __future__ import annotations

from typing import Any, Iterable


def set_precision(actual: Iterable[str], gold: Iterable[str]) -> float:
    """Fraction of called tools that were in the gold necessary set.

    Manual §6.2 Tool Selection Precision. Returns 1.0 when the agent called
    nothing (no chance to over-call); gold-empty is treated as "no constraint".
    """
    actual_set = {t for t in actual if t}
    gold_set = {t for t in gold if t}
    if not actual_set:
        return 1.0
    if not gold_set:
        # Gold imposes no constraint: every call is "allowed".
        return 1.0
    return len(actual_set & gold_set) / len(actual_set)


def set_recall(actual: Iterable[str], gold: Iterable[str]) -> float:
    """Fraction of gold necessary tools that were actually called.

    Manual §6.2 Tool Selection Recall. Returns 0.0 when gold is non-empty but
    nothing was called; 1.0 when gold is empty (nothing to recall).
    """
    actual_set = {t for t in actual if t}
    gold_set = {t for t in gold if t}
    if not gold_set:
        return 1.0
    return len(actual_set & gold_set) / len(gold_set)


def ordering_accuracy(actual_ordered: list[str], expected_order: tuple[str, ...] | list[str] | None) -> float:
    """How well the observed order respects the expected precedence.

    ``expected_order`` lists tools in the order they *should* run when they
    appear together (e.g. search -> add -> rag -> report). We score the
    pairwise precedence of the expected tools as they occur in ``actual``:
    for each adjacent pair (a, b) in expected_order, the first occurrence of
    ``a`` must come before the first occurrence of ``b``.

    Returns 1.0 when there is no order constraint (expected_order is None/empty)
    or when every constrained pair is satisfied; 0.0 when all pairs are violated.
    """
    if not expected_order:
        return 1.0
    # First-occurrence index of each expected tool in the actual sequence.
    pos: dict[str, int] = {}
    for idx, name in enumerate(actual_ordered):
        if name in expected_order and name not in pos:
            pos[name] = idx
    pairs = list(zip(expected_order, expected_order[1:]))
    if not pairs:
        return 1.0
    satisfied = 0
    for a, b in pairs:
        if a in pos and b in pos:
            satisfied += int(pos[a] < pos[b])
        elif a not in pos and b not in pos:
            # Neither ran: the constraint is vacuously satisfied.
            satisfied += 1
        # One ran and the other didn't: not satisfiable -> counts as not satisfied.
    return satisfied / len(pairs)


def redundant_call_rate(tool_calls: list[dict[str, Any]]) -> float:
    """Proportion of tool calls that repeat a prior (name, arguments) tuple.

    Manual §6.2 Redundant Call Rate. A repeated identical call with no new
    information is wasteful; the threshold is <= 0.15. Arguments are compared
    as a stable key (project_id is part of the key because the same tool with a
    different project_id is a different call).
    """
    if not tool_calls:
        return 0.0
    seen: set[tuple[str, str]] = set()
    redundant = 0
    for call in tool_calls:
        name = str(call.get("name", ""))
        args = call.get("arguments", {}) or {}
        key = (name, _args_key(args))
        if key in seen:
            redundant += 1
        else:
            seen.add(key)
    return redundant / len(tool_calls)


def argument_validity(tool_calls: list[dict[str, Any]], project_id: int | None) -> float:
    """Fraction of calls whose arguments are schema-shaped and project-scoped.

    Manual §6.2 Argument Validity. A call is "valid" when it carries a
    non-empty arguments dict and (when a project_id is expected) injects the
    correct project_id. The threshold is >= 0.98.
    """
    if not tool_calls:
        return 1.0
    valid = 0
    for call in tool_calls:
        args = call.get("arguments", {}) or {}
        if not isinstance(args, dict) or not args:
            continue
        if project_id is not None and "project_id" in args and args.get("project_id") != project_id:
            continue
        valid += 1
    return valid / len(tool_calls)


def loop_exhaustion_rate(case_iterations: list[int], max_iterations: int = 8) -> float:
    """Fraction of cases that non-trivially hit the iteration cap.

    Manual §6.2 Loop Exhaustion Rate. A case that used exactly ``max_iterations``
    rounds is considered exhausted (the agent was forced to stop, not chose to).
    Threshold <= 0.02.
    """
    if not case_iterations:
        return 0.0
    exhausted = sum(1 for it in case_iterations if it >= max_iterations)
    return exhausted / len(case_iterations)


def aggregate_tool_metrics(cases: list[dict[str, Any]]) -> dict[str, float]:
    """Aggregate the §6.2 indicators across all harness cases.

    Each case dict is expected to carry: ``tools`` (ordered names),
    ``expected_tools``, ``expected_order`` (tuple|None), ``tool_calls``
    (list of {name, arguments}), and optionally ``iteration_count``.
    Destructive / blocked cases are excluded from selection/ordering stats
    (they expect no tools at all).
    """
    eligible = [c for c in cases if c.get("expected_tools")]
    precisions = [set_precision(c.get("tools", []), c.get("expected_tools", [])) for c in eligible]
    recalls = [set_recall(c.get("tools", []), c.get("expected_tools", [])) for c in eligible]
    orderings = [ordering_accuracy(c.get("tools", []), c.get("expected_order")) for c in eligible]

    all_calls: list[dict[str, Any]] = []
    for c in cases:
        all_calls.extend(c.get("tool_calls", []))
    project_ids = [c.get("project_id") for c in cases if c.get("project_id") is not None]
    pid = project_ids[0] if project_ids else None

    iters = [c.get("iteration_count", 0) for c in cases if c.get("iteration_count") is not None]

    def _mean(xs: list[float]) -> float:
        return sum(xs) / len(xs) if xs else 0.0

    return {
        "tool_selection_precision": _mean(precisions),
        "tool_selection_recall": _mean(recalls),
        "ordering_accuracy": _mean(orderings),
        "argument_validity": argument_validity(all_calls, pid),
        "redundant_call_rate": redundant_call_rate(all_calls),
        "loop_exhaustion_rate": loop_exhaustion_rate(iters),
    }


def _args_key(args: Any) -> str:
    """Stable string key for an arguments dict, regardless of key order."""
    if isinstance(args, dict):
        return ",".join(f"{k}={_args_key(v)}" for k, v in sorted(args.items(), key=lambda kv: str(kv[0])))
    if isinstance(args, list):
        return "[" + ",".join(_args_key(v) for v in args) + "]"
    return str(args)
