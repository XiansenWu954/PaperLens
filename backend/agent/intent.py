"""Project Agent intent recognition and tool planning."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any


SEARCH_TOKENS = (
    "搜索",
    "检索",
    "查找",
    "找",
    "补充",
    "继续",
    "扩大",
    "扩展",
    "更多",
    "深入",
    "跟进",
    "加入",
    "添加",
    "入库",
    "search",
    "add",
    "find",
    "continue",
    "expand",
    "broaden",
    "more",
    "dblp",
    "openalex",
    "arxiv",
)
LIST_QUERY_PHRASES = (
    "有哪些论文",
    "有多少论文",
    "论文列表",
    "paper list",
    "library inventory",
)
LIST_ACTION_TOKENS = (
    "列出",
    "查看",
    "显示",
    "展示",
    "show",
    "list",
    "inventory",
)
LIST_SUBJECT_TOKENS = (
    "论文库",
    "项目库",
    "当前论文",
    "项目论文",
    "已有论文",
    "papers",
    "library",
)
LIBRARY_REASONING_TOKENS = (
    "说明",
    "核心",
    "适合",
    "推荐",
    "优先",
    "为什么",
    "which",
    "why",
    "recommend",
    "core",
)
SEARCH_DIRECTION_PHRASES = (
    "检索方向",
    "继续检索的方向",
    "可以继续检索",
    "后续检索方向",
    "future search direction",
    "search direction",
)
GRAPH_QUERY_PHRASES = (
    "引用图谱",
    "关系图谱",
    "引用关系",
    "citation graph",
    "citation map",
    "citation network",
)
GRAPH_ACTION_TOKENS = (
    "刷新",
    "构建",
    "生成",
    "显示",
    "查看",
    "分析",
    "show",
    "build",
    "refresh",
)
GRAPH_SUBJECT_TOKENS = (
    "图谱",
    "引用关系",
    "关系图",
    "citation",
    "graph",
    "map",
    "network",
)
REPORT_TOKENS = (
    "报告",
    "章节",
    "综述",
    "总结",
    "草稿",
    "report",
    "section",
    "survey",
    "summary",
    "summarize",
)
DESTRUCTIVE_TOKENS = (
    "删除",
    "移除所有",
    "清空",
    "删掉",
    "覆盖报告",
    "delete",
    "remove all",
    "clear",
    "drop",
    "overwrite",
)


@dataclass(frozen=True)
class ToolPlanStep:
    name: str
    arguments: dict[str, Any] = field(default_factory=dict)
    summary: str = ""


@dataclass(frozen=True)
class ProjectIntent:
    name: str
    rationale: str
    tool_plan: tuple[ToolPlanStep, ...]
    blocked: bool = False

    @property
    def tool_names(self) -> list[str]:
        return [step.name for step in self.tool_plan]


def classify_project_intent(message: str, project_id: int) -> ProjectIntent:
    """Classify a chat message into an auditable, deterministic tool plan."""

    text = (message or "").strip()
    lowered = text.lower()
    if not text:
        return ProjectIntent("empty", "empty message", tuple(), blocked=True)

    if _has_any(lowered, DESTRUCTIVE_TOKENS):
        return ProjectIntent(
            "blocked_destructive",
            "destructive project changes require explicit UI/API action",
            tuple(),
            blocked=True,
        )

    wants_search = _wants_search(lowered)
    wants_search_direction = _has_any(lowered, SEARCH_DIRECTION_PHRASES) and not wants_search
    wants_list = _wants_list(lowered) and not wants_search
    wants_graph = _wants_graph(lowered)
    wants_report = _has_any(lowered, REPORT_TOKENS)
    wants_library_reasoning = wants_list and _has_any(lowered, LIBRARY_REASONING_TOKENS)

    plan: list[ToolPlanStep] = []
    if wants_search:
        query = _clean_search_query(text)
        max_results = _extract_limit(lowered, default=5)
        plan.extend(
            [
                ToolPlanStep(
                    "search_papers",
                    {"query": query, "max_results": max_results},
                    "search external scholarly sources",
                ),
                ToolPlanStep(
                    "add_papers_to_project",
                    {"project_id": project_id, "reason": f"Agent search for: {text[:120]}"},
                    "add search results to the project library",
                ),
            ]
        )

    if wants_list:
        plan.append(
            ToolPlanStep(
                "list_project_papers",
                {"project_id": project_id},
                "inspect project library",
            )
        )
        if wants_library_reasoning:
            plan.append(
                ToolPlanStep(
                    "query_project_rag",
                    {"project_id": project_id, "question": text, "k": 6},
                    "retrieve project evidence for library recommendation",
                )
            )

    if wants_graph:
        plan.append(
            ToolPlanStep(
                "get_project_citation_graph",
                {"project_id": project_id},
                "build project citation map",
            )
        )

    if wants_report:
        plan.extend(
            [
                ToolPlanStep(
                    "query_project_rag",
                    {"project_id": project_id, "question": text, "k": 8},
                    "retrieve project evidence for report writing",
                ),
                ToolPlanStep(
                    "draft_report_section",
                    {"project_id": project_id, "question": text},
                    "draft a grounded report section",
                ),
            ]
        )
    elif not wants_list and not wants_graph:
        plan.append(
            ToolPlanStep(
                "query_project_rag",
                {"project_id": project_id, "question": text, "k": 6},
                "answer from project evidence",
            )
        )
        if wants_search_direction:
            plan.append(
                ToolPlanStep(
                    "search_papers",
                    {"query": _clean_search_query(text), "max_results": 5},
                    "preview external papers for follow-up search directions",
                )
            )

    intent_name = _name_intent(wants_search, wants_list, wants_graph, wants_report, wants_search_direction)
    return ProjectIntent(intent_name, _rationale(intent_name), tuple(plan))


def _has_any(text: str, tokens: tuple[str, ...]) -> bool:
    return any(token in text for token in tokens)


def _wants_search(text: str) -> bool:
    if _has_any(text, SEARCH_DIRECTION_PHRASES):
        explicit_execution = (
            "加入" in text
            or "添加" in text
            or "入库" in text
            or "找 " in text
            or "找2" in text
            or "找 2" in text
            or "search " in text
            or "add " in text
            or "find " in text
        )
        if not explicit_execution:
            return False
    return _has_any(text, SEARCH_TOKENS)


def _wants_list(text: str) -> bool:
    if _has_any(text, LIST_QUERY_PHRASES):
        return True
    return _has_any(text, LIST_ACTION_TOKENS) and _has_any(text, LIST_SUBJECT_TOKENS)


def _wants_graph(text: str) -> bool:
    if _has_any(text, GRAPH_QUERY_PHRASES):
        return True
    return _has_any(text, GRAPH_ACTION_TOKENS) and _has_any(text, GRAPH_SUBJECT_TOKENS)


def _extract_limit(text: str, default: int) -> int:
    match = re.search(r"(\d{1,2})\s*(篇|papers?|results?)?", text)
    if not match:
        return default
    return max(1, min(10, int(match.group(1))))


def _clean_search_query(text: str) -> str:
    lowered = text.lower()
    if "局限" in lowered or "limitations" in lowered:
        return "long sequence modeling limitations Transformer Mamba benchmark comparison"
    if "mamba" in lowered:
        return "Mamba selective state space model follow-up work"
    if "retrieval augmented generation" in lowered or re.search(r"\brag\b", lowered):
        if "评测" in lowered or "evaluation" in lowered or "benchmark" in lowered:
            return "retrieval augmented generation evaluation benchmark"
        return "retrieval augmented generation"
    cleaned = re.sub(r"(继续|帮我|请|检索|搜索|查找|找|扩大|扩展|更多|深入|跟进|加入|添加|入库|论文|相关|dblp|openalex|arxiv|continue|expand|broaden|more)", " ", text, flags=re.I)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" ，,。.")
    return cleaned or text[:160]


def _name_intent(search: bool, list_: bool, graph: bool, report: bool, search_direction: bool = False) -> str:
    names = []
    if search:
        names.append("search_add")
    if search_direction:
        names.append("search_direction")
    if list_:
        names.append("library")
    if graph:
        names.append("graph")
    if report:
        names.append("report")
    return "+".join(names) if names else "answer"


def _rationale(name: str) -> str:
    return {
        "answer": "answer requires project-scoped RAG",
        "search_add": "message asks to search or add papers",
        "search_direction": "message asks for follow-up search directions without project insertion",
        "library": "message asks to inspect the project paper library",
        "graph": "message asks for citation or relationship analysis",
        "report": "message asks for report or section drafting",
    }.get(name, "message combines multiple project actions")
