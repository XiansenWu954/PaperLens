"""Golden intent matrix for project Agent routing."""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class IntentEvalCase:
    id: str
    message: str
    expected_intent: str
    expected_tools: tuple[str, ...]
    expected_blocked: bool = False


INTENT_EVAL_CASES = (
    IntentEvalCase(
        "answer_basic_cn",
        "Mamba 有什么特点？",
        "answer",
        ("query_project_rag",),
    ),
    IntentEvalCase(
        "answer_compare_en",
        "compare these papers",
        "compare",
        ("compare_papers",),
    ),
    IntentEvalCase(
        "answer_compare_papers_en",
        "compare Mamba and Transformer papers",
        "compare",
        ("compare_papers",),
    ),
    IntentEvalCase(
        "answer_what_papers_say_en",
        "what do the papers say about linear attention?",
        "answer",
        ("query_project_rag",),
    ),
    IntentEvalCase(
        "answer_limitations_cn",
        "这些论文有什么共同局限？",
        "answer",
        ("query_project_rag",),
    ),
    IntentEvalCase(
        "answer_limitations_with_search_directions_cn",
        "这些论文共同局限是什么？请给出可以继续检索的方向。",
        "search_direction",
        ("query_project_rag", "search_papers"),
    ),
    IntentEvalCase(
        "answer_compare_cn",
        "基于这些论文对比 Mamba 和 Transformer。",
        "compare",
        ("compare_papers",),
    ),
    IntentEvalCase(
        "answer_citation_request_not_graph",
        "回答时请引用论文来源。",
        "answer",
        ("query_project_rag",),
    ),
    IntentEvalCase(
        "report_summarize_en",
        "summarize papers about Mamba",
        "report",
        ("query_project_rag", "draft_report_section"),
    ),
    IntentEvalCase(
        "report_section_cn",
        "生成一个报告章节",
        "report",
        ("query_project_rag", "draft_report_section"),
    ),
    IntentEvalCase(
        "report_survey_cn",
        "基于项目库写一段综述草稿",
        "report",
        ("query_project_rag", "draft_report_section"),
    ),
    IntentEvalCase(
        "library_list_en",
        "list project papers",
        "library",
        ("list_project_papers",),
    ),
    IntentEvalCase(
        "library_show_en",
        "show project papers",
        "library",
        ("list_project_papers",),
    ),
    IntentEvalCase(
        "library_inventory_en",
        "library inventory",
        "library",
        ("list_project_papers",),
    ),
    IntentEvalCase(
        "library_view_cn",
        "查看当前项目论文库",
        "library",
        ("list_project_papers",),
    ),
    IntentEvalCase(
        "library_which_cn",
        "当前项目有哪些论文？",
        "library",
        ("list_project_papers",),
    ),
    IntentEvalCase(
        "library_core_recommendation_cn",
        "列出当前项目论文，并说明哪些更适合作为核心论文。",
        "library_reasoning",
        ("list_project_papers", "query_project_rag"),
    ),
    IntentEvalCase(
        "graph_show_en",
        "show citation graph",
        "graph",
        ("get_project_citation_graph",),
    ),
    IntentEvalCase(
        "graph_map_en",
        "open the citation map",
        "graph",
        ("get_project_citation_graph",),
    ),
    IntentEvalCase(
        "graph_refresh_cn",
        "刷新引用关系图谱",
        "graph",
        ("get_project_citation_graph",),
    ),
    IntentEvalCase(
        "graph_analyze_cn",
        "分析项目论文的引用关系",
        "graph",
        ("get_project_citation_graph",),
    ),
    IntentEvalCase(
        "search_dblp_cn",
        "继续检索 DBLP 中 Mamba 后续工作并加入项目库",
        "search_add",
        ("search_papers", "add_papers_to_project", "query_project_rag"),
    ),
    IntentEvalCase(
        "search_expand_cn",
        "继续扩大这个方向的论文范围",
        "search_add",
        ("search_papers", "add_papers_to_project", "query_project_rag"),
    ),
    IntentEvalCase(
        "search_count_en",
        "search 3 papers about retrieval augmented generation",
        "search_add",
        ("search_papers", "add_papers_to_project", "query_project_rag"),
    ),
    IntentEvalCase(
        "search_count_cn",
        "找 2 篇 RAG 评测论文并加入项目库",
        "search_add",
        ("search_papers", "add_papers_to_project", "query_project_rag"),
    ),
    IntentEvalCase(
        "search_arxiv_en",
        "find arxiv papers about efficient sequence modeling",
        "search_add",
        ("search_papers", "add_papers_to_project", "query_project_rag"),
    ),
    IntentEvalCase(
        "search_openalex_cn",
        "用 OpenAlex 补充 state space model 论文",
        "search_add",
        ("search_papers", "add_papers_to_project", "query_project_rag"),
    ),
    IntentEvalCase(
        "combo_library_graph_cn",
        "列出当前论文并刷新引用图谱",
        "library+graph",
        ("list_project_papers", "get_project_citation_graph"),
    ),
    IntentEvalCase(
        "combo_search_report_cn",
        "检索 Mamba 后续工作并生成综述章节",
        "search_add+report",
        ("search_papers", "add_papers_to_project", "query_project_rag", "draft_report_section"),
    ),
    IntentEvalCase(
        "combo_search_graph_en",
        "search 4 papers about RAG and show citation graph",
        "search_add+graph",
        ("search_papers", "add_papers_to_project", "get_project_citation_graph"),
    ),
    IntentEvalCase(
        "blocked_delete_cn",
        "清空项目并删除所有论文",
        "blocked_destructive",
        tuple(),
        True,
    ),
    IntentEvalCase(
        "blocked_overwrite_en",
        "overwrite the current report",
        "blocked_destructive",
        tuple(),
        True,
    ),
    IntentEvalCase(
        "empty",
        "",
        "empty",
        tuple(),
        False,  # §25.2: an empty message is a CLARIFY request, not blocked
    ),
)


def evaluate_intent_classifier(project_id: int = 1) -> dict[str, Any]:
    from agent.intent import classify_project_intent

    results: list[dict[str, Any]] = []
    for case in INTENT_EVAL_CASES:
        actual = classify_project_intent(case.message, project_id=project_id)
        passed = (
            actual.name == case.expected_intent
            and tuple(actual.tool_names) == case.expected_tools
            and actual.blocked == case.expected_blocked
        )
        results.append(
            {
                "id": case.id,
                "passed": passed,
                "message": case.message,
                "expected_intent": case.expected_intent,
                "actual_intent": actual.name,
                "expected_tools": list(case.expected_tools),
                "actual_tools": actual.tool_names,
                "expected_blocked": case.expected_blocked,
                "actual_blocked": actual.blocked,
                "rationale": actual.rationale,
            }
        )
    return {
        "passed": all(item["passed"] for item in results),
        "total": len(results),
        "passed_count": sum(1 for item in results if item["passed"]),
        "failed": [item for item in results if not item["passed"]],
        "cases": results,
    }


def dumps_intent_eval(result: dict[str, Any]) -> str:
    return json.dumps(result, ensure_ascii=True, indent=2, default=str)
