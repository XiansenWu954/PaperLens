"""Capability Evidence Policy (Tasks 4.1-4.4, §25).

The minimum evidence requirement for a task is decided by a STRUCTURED
capability contract derived from the intent/router result — never from answer
keywords, answer length, model self-reported answer_mode, or which tools were
called. Tool calls can never lower the requirement: a factual request stays
factual even when the model also calls list/search tools.

§25.2/25.3 additions:
- empty / ambiguous requests → CLARIFY; destructive requests → BLOCKED (never
  folded into action_result);
- every ACTION contract carries its required/terminal tool outcome; an action
  is successful only when the terminal tool ran and its final result is not an
  error (recovered early errors are warnings, not failures).
"""
from __future__ import annotations

from enum import Enum


class Capability(str, Enum):
    ACTION = "action"      # list/search/export/graph/add: structured action result
    FACTUAL = "factual"    # project QA: >=1 answer-bound resolved fulltext
    COMPARE = "compare"    # per-object comparison: every compared paper resolved fulltext
    REPORT = "report"      # report drafting: factual output bound to resolved fulltext
    CLARIFY = "clarify"    # empty / ambiguous request: no evidence, no tools
    BLOCKED = "blocked"    # destructive request: blocked, never action_result


# §25.3/§26.2: required steps must each succeed at least once; the terminal
# tool's FINAL outcome decides completion. search-add requires a successful
# search AND a successful add.
ACTION_REQUIRED_STEPS: dict[str, tuple[str, ...]] = {
    "search_add": ("search_papers",),
}
ACTION_TERMINAL_TOOLS: dict[str, tuple[str, ...]] = {
    "search_add": ("add_papers_to_project",),   # search-add completes with add
    "library": ("list_project_papers",),
    "graph": ("get_project_citation_graph",),
    "export": ("export_bibtex",),
}


class CapabilityContract:
    """Structured minimum-evidence contract for one capability."""

    def __init__(
        self,
        capability: Capability,
        requires_resolved_bound_fulltext: bool = False,
        per_paper_fulltext: bool = False,
        required_steps: tuple[str, ...] = (),
        terminal_tools: tuple[str, ...] = (),
    ) -> None:
        self.capability = capability
        self.requires_resolved_bound_fulltext = requires_resolved_bound_fulltext
        self.per_paper_fulltext = per_paper_fulltext
        self.required_steps = tuple(required_steps)
        self.terminal_tools = tuple(terminal_tools)

    @property
    def evidence_needs_fulltext(self) -> bool:
        return self.requires_resolved_bound_fulltext


ACTION_CONTRACT = CapabilityContract(Capability.ACTION)
FACTUAL_CONTRACT = CapabilityContract(Capability.FACTUAL, requires_resolved_bound_fulltext=True)
COMPARE_CONTRACT = CapabilityContract(Capability.COMPARE, requires_resolved_bound_fulltext=True,
                                      per_paper_fulltext=True)
REPORT_CONTRACT = CapabilityContract(Capability.REPORT, requires_resolved_bound_fulltext=True)
CLARIFY_CONTRACT = CapabilityContract(Capability.CLARIFY)
BLOCKED_CONTRACT = CapabilityContract(Capability.BLOCKED)


def capability_for_intent(intent) -> CapabilityContract:
    """Map the structured intent result to its capability contract.

    `intent` must expose `.name` and `.blocked` (ProjectIntent). Compound names
    are split on "+" and take the STRICTEST applicable capability.
    """
    name = str(getattr(intent, "name", "") or "")
    if getattr(intent, "blocked", False):
        # §25.2: destructive requests are BLOCKED; empty messages are CLARIFY —
        # neither may be folded into ACTION/action_result.
        if name == "empty":
            return CLARIFY_CONTRACT
        return BLOCKED_CONTRACT
    parts = [p for p in name.split("+") if p]
    if not parts or parts[0] == "empty":
        return CLARIFY_CONTRACT
    if parts[0] == "clarify":
        return CLARIFY_CONTRACT
    if "compare" in parts:
        return COMPARE_CONTRACT
    if "report" in parts:
        return REPORT_CONTRACT
    if parts[0] in ("answer", "search_direction", "library_reasoning"):
        # §24.1: library reasoning (core/recommendation/why) makes content
        # claims about papers → factual contract; pure inventory stays ACTION.
        return FACTUAL_CONTRACT
    # library / graph / search_add / export → ACTION with required steps and
    # a terminal outcome.
    return CapabilityContract(
        Capability.ACTION,
        required_steps=ACTION_REQUIRED_STEPS.get(parts[0], ()),
        terminal_tools=ACTION_TERMINAL_TOOLS.get(parts[0], ()),
    )
