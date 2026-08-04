"""agent 测试套件（纯逻辑，mock LLM，不烧 token 不依赖外网）。

覆盖：
- state reducer（add 累加 / 不上浮 messages）
- planner 解析（mock DeepSeek 返回固定 JSON）
- tools.execute_tool（真实 datasources openalex）
- researcher（mock complete_with_tools，验证 ReAct 循环 + 收集 sources）
- graph 编译（结构正确）
"""
import asyncio
import json
from unittest import mock

from django.test import TransactionTestCase

from agent.config import DEFAULT_CONFIG
from agent.state import add


class StateReducerTest(TransactionTestCase):
    def test_add_concatenates_lists(self):
        self.assertEqual(add(["a"], ["b"]), ["a", "b"])

    def test_add_handles_none(self):
        self.assertEqual(add(None, ["x"]), ["x"])
        self.assertEqual(add(["x"], None), ["x"])

    def test_add_empty(self):
        self.assertEqual(add([], []), [])


class PlannerParseTest(TransactionTestCase):
    """planner 的 _parse_plan 容错解析。"""

    def test_parse_valid_json(self):
        from agent.nodes.planner import _parse_plan
        result = _parse_plan('{"sub_queries": ["q1", "q2"]}', max_sub_queries=3)
        self.assertEqual(result, ["q1", "q2"])

    def test_parse_markdown_wrapped(self):
        from agent.nodes.planner import _parse_plan
        text = '```json\n{"sub_queries": ["q1"]}\n```'
        result = _parse_plan(text, max_sub_queries=3)
        self.assertEqual(result, ["q1"])

    def test_parse_caps_at_max(self):
        from agent.nodes.planner import _parse_plan
        result = _parse_plan('{"sub_queries": ["a","b","c","d"]}', max_sub_queries=2)
        self.assertEqual(len(result), 2)

    def test_parse_fallback_line_split(self):
        from agent.nodes.planner import _parse_plan
        # 非 JSON 时按行分割
        result = _parse_plan("first query\nsecond query", max_sub_queries=3)
        self.assertEqual(len(result), 2)


class PlannerNodeTest(TransactionTestCase):
    """planner 节点 mock DeepSeek。"""

    def test_planner_returns_subqueries(self):
        from agent.nodes.planner import planner

        fake_client = mock.MagicMock()
        fake_client.complete.return_value = {"content": '{"sub_queries": ["q1","q2"]}'}
        with mock.patch("llm.deepseek.DeepSeekClient", return_value=fake_client):
            result = asyncio.run(planner({"question": "test"}, DEFAULT_CONFIG))
        self.assertIn("plan", result)
        self.assertEqual(result["plan"], ["q1", "q2"])


class ToolsExecuteTest(TransactionTestCase):
    """execute_tool 真实调 datasources（openalex），验证返回 JSON + 入库。"""

    def test_execute_search_papers(self):
        from agent.tools import execute_tool
        from papers.models import Paper

        result_json = asyncio.run(
            execute_tool("search_papers", {"query": "transformer attention", "max_results": 2})
        )
        papers = json.loads(result_json)
        self.assertGreaterEqual(len(papers), 1)
        self.assertIn("title", papers[0])
        # 验证入库
        self.assertGreater(Paper.objects.count(), 0)

    def test_execute_unknown_tool(self):
        from agent.tools import execute_tool
        result_json = asyncio.run(execute_tool("nonexistent", {}))
        self.assertIn("error", json.loads(result_json))

    def test_execute_missing_query(self):
        from agent.tools import execute_tool
        result_json = asyncio.run(execute_tool("search_papers", {}))
        self.assertIn("error", json.loads(result_json))


class ResearcherTest(TransactionTestCase):
    """researcher mock complete_with_tools，验证 ReAct 循环 + 收集 sources。"""

    def test_researcher_collects_sources(self):
        from agent.nodes.researcher import researcher

        # mock DeepSeekClient：第一次返回 tool_call，第二次返回笔记
        fake_client = mock.MagicMock()
        tool_result = '[{"title":"Paper A","year":2024,"authors":["X"],"citation_count":10,"doi":"10.1/a"}]'

        call_seq = {"n": 0}

        def fake_complete_with_tools(messages, tools, **kw):
            call_seq["n"] += 1
            if call_seq["n"] == 1:
                return {"content": "", "tool_calls": [{"id": "c1", "name": "search_papers", "arguments": '{"query":"test"}'}], "usage": {}}
            # 第二次：不再调工具
            return {"content": "总结", "tool_calls": [], "usage": {}}

        def fake_complete(messages, **kw):
            return {"content": "- 笔记1 (Paper A)\n- 笔记2"}

        fake_client.complete_with_tools = fake_complete_with_tools
        fake_client.complete = fake_complete

        # mock execute_tool 避免真实网络
        async def fake_execute_tool(name, args):
            return tool_result

        with mock.patch("llm.deepseek.DeepSeekClient", return_value=fake_client), \
             mock.patch("agent.nodes.researcher.execute_tool", fake_execute_tool):
            result = asyncio.run(researcher({"sub_query": "test query"}, DEFAULT_CONFIG))

        self.assertIn("notes", result)
        self.assertGreaterEqual(len(result["notes"]), 1)
        self.assertGreaterEqual(len(result["sources"]), 1)
        self.assertEqual(result["sources"][0]["title"], "Paper A")

    def test_researcher_tool_budget_stops(self):
        """工具调用达预算应停止。"""
        from agent.nodes.researcher import researcher

        fake_client = mock.MagicMock()
        # 每次都返回 tool_call（模拟模型一直想调）
        fake_client.complete_with_tools = lambda m, t, **k: {
            "content": "", "tool_calls": [{"id": "c", "name": "search_papers", "arguments": '{"query":"x"}'}], "usage": {}
        }
        fake_client.complete = lambda m, **k: {"content": "笔记"}

        async def fake_execute_tool(name, args):
            return "[]"

        with mock.patch("llm.deepseek.DeepSeekClient", return_value=fake_client), \
             mock.patch("agent.nodes.researcher.execute_tool", fake_execute_tool):
            cfg = DEFAULT_CONFIG
            result = asyncio.run(researcher({"sub_query": "q"}, cfg))
        # 预算 max_tool_calls_per_researcher=3，不应无限循环
        self.assertIn("notes", result)


class SynthesizerTest(TransactionTestCase):
    """synthesizer mock DeepSeek。"""

    def test_synthesizer_produces_report(self):
        from agent.nodes.synthesizer import synthesizer

        fake_client = mock.MagicMock()
        fake_client.complete.return_value = {"content": "# 综述\n内容..."}
        with mock.patch("llm.deepseek.DeepSeekClient", return_value=fake_client):
            result = asyncio.run(synthesizer({
                "question": "test",
                "notes": ["笔记1"],
                "sources": [{"title": "P1", "year": 2024, "authors": ["A"], "citation_count": 5}],
            }, DEFAULT_CONFIG))
        self.assertEqual(result["final_report"], "# 综述\n内容...")


class GraphCompileTest(TransactionTestCase):
    """graph 编译成功，结构正确。"""

    def test_graph_compiles(self):
        from agent.graph import build_graph
        graph = build_graph(DEFAULT_CONFIG)
        self.assertIsNotNone(graph)

    def test_fan_out_parallel_with_mock_researchers(self):
        """fan_out 并行 + 单源失败不阻断。"""
        from agent.graph import fan_out_researchers

        async def fake_researcher(state, config):
            return {"notes": [f"note for {state['sub_query']}"], "sources": [{"title": state['sub_query']}]}

        with mock.patch("agent.graph.researcher", fake_researcher):
            result = asyncio.run(fan_out_researchers(
                {"plan": ["q1", "q2", "q3"]}, DEFAULT_CONFIG
            ))
        self.assertEqual(len(result["notes"]), 3)
        self.assertEqual(len(result["sources"]), 3)

    def test_fan_out_tolerates_researcher_failure(self):
        from agent.graph import fan_out_researchers

        async def fake_researcher(state, config):
            if state["sub_query"] == "fail":
                raise RuntimeError("boom")
            return {"notes": ["ok"], "sources": [{"title": "ok"}]}

        with mock.patch("agent.graph.researcher", fake_researcher):
            result = asyncio.run(fan_out_researchers(
                {"plan": ["ok", "fail"]}, DEFAULT_CONFIG
            ))
        # 失败的不阻断，成功的仍合并
        self.assertEqual(len(result["notes"]), 1)
from django.test import TestCase

from agent.project_tools import available_tool_names


class ProjectAgentToolPolicyTest(TestCase):
    def test_no_destructive_project_tools(self):
        tools = available_tool_names()
        self.assertIn("query_project_rag", tools)
        self.assertIn("search_papers", tools)
        self.assertNotIn("delete_project_paper", tools)
        self.assertNotIn("clear_project", tools)


class ProjectPromptContractTest(TestCase):
    def test_project_prompts_define_grounding_and_tool_boundaries(self):
        from agent import prompts

        self.assertIn("项目论文库证据", prompts.PROJECT_CHAT_RESPONDER_SYSTEM)
        self.assertIn("不能编造 pqac key", prompts.PROJECT_CHAT_RESPONDER_SYSTEM)
        self.assertIn("不直接覆盖任何 ReportVersion", prompts.PROJECT_REPORT_WRITER_SYSTEM)
        self.assertIn("破坏性工具", prompts.PROJECT_CRITIC_SYSTEM)


class ProjectIntentClassifierTest(TestCase):
    def test_answer_intent_uses_project_rag(self):
        from agent.intent import classify_project_intent

        intent = classify_project_intent("Mamba 有什么特点？", project_id=7)
        self.assertEqual(intent.name, "answer")
        self.assertEqual(intent.tool_names, ["query_project_rag"])

    def test_search_add_intent_uses_search_add_and_rag(self):
        from agent.intent import classify_project_intent

        intent = classify_project_intent("继续检索 DBLP 中 Mamba 后续工作并加入项目库", project_id=7)
        self.assertIn("search_papers", intent.tool_names)
        self.assertIn("add_papers_to_project", intent.tool_names)
        self.assertIn("query_project_rag", intent.tool_names)

    def test_expand_intent_triggers_search_add(self):
        from agent.intent import classify_project_intent

        intent = classify_project_intent("继续扩大这个方向的论文范围", project_id=7)
        self.assertEqual(intent.name, "search_add")
        self.assertEqual(intent.tool_names, ["search_papers", "add_papers_to_project", "query_project_rag"])

    def test_library_graph_report_intents_use_distinct_tools(self):
        from agent.intent import classify_project_intent

        library = classify_project_intent("列出当前论文库有哪些论文", project_id=7)
        graph = classify_project_intent("刷新引用关系图谱", project_id=7)
        report = classify_project_intent("生成一个报告章节", project_id=7)
        self.assertEqual(library.tool_names, ["list_project_papers"])
        self.assertEqual(graph.tool_names, ["get_project_citation_graph"])
        self.assertEqual(report.tool_names, ["query_project_rag", "draft_report_section"])

    def test_destructive_intent_is_blocked(self):
        from agent.intent import classify_project_intent

        intent = classify_project_intent("清空项目并删除所有论文", project_id=7)
        self.assertTrue(intent.blocked)
        self.assertEqual(intent.tool_names, [])

    def test_intent_matrix_covers_common_project_chat_requests(self):
        from agent.intent import classify_project_intent
        from eval.intent_eval import INTENT_EVAL_CASES

        for case in INTENT_EVAL_CASES:
            with self.subTest(case=case.id):
                intent = classify_project_intent(case.message, project_id=9)
                self.assertEqual(intent.name, case.expected_intent)
                self.assertEqual(intent.tool_names, list(case.expected_tools))
                self.assertEqual(intent.blocked, case.expected_blocked)

    def test_generic_papers_word_does_not_force_library_intent(self):
        from agent.intent import classify_project_intent

        cases = [
            "compare these papers",
            "what do the papers say about linear attention?",
            "summarize papers about Mamba",
        ]
        for message in cases:
            with self.subTest(message=message):
                intent = classify_project_intent(message, project_id=9)
                self.assertNotIn("list_project_papers", intent.tool_names)

    def test_citation_source_request_does_not_force_graph_intent(self):
        from agent.intent import classify_project_intent

        intent = classify_project_intent("回答时请引用论文来源。", project_id=9)
        self.assertEqual(intent.name, "answer")
        self.assertEqual(intent.tool_names, ["query_project_rag"])

    def test_search_limit_is_extracted_and_capped(self):
        from agent.intent import classify_project_intent

        three = classify_project_intent("search 3 papers about RAG", project_id=9)
        many = classify_project_intent("search 99 papers about RAG", project_id=9)
        self.assertEqual(three.tool_plan[0].arguments["max_results"], 3)
        self.assertEqual(many.tool_plan[0].arguments["max_results"], 10)

    def test_search_query_rewrite_handles_chinese_agent_requests(self):
        from agent.intent import classify_project_intent

        mamba = classify_project_intent(
            "继续扩大 Mamba 后续工作范围，优先补充 DBLP/OpenAlex 能找到的论文。",
            project_id=9,
        )
        rag = classify_project_intent(
            "找 2 篇 retrieval augmented generation 评测论文并加入项目库",
            project_id=9,
        )
        self.assertEqual(
            mamba.tool_plan[0].arguments["query"],
            "Mamba selective state space model follow-up work",
        )
        self.assertEqual(
            rag.tool_plan[0].arguments["query"],
            "retrieval augmented generation evaluation benchmark",
        )

    def test_search_direction_request_searches_without_adding(self):
        from agent.intent import classify_project_intent

        intent = classify_project_intent("这些论文共同局限是什么？请给出可以继续检索的方向。", project_id=9)
        self.assertEqual(intent.name, "search_direction")
        self.assertEqual(intent.tool_names, ["query_project_rag", "search_papers"])
        self.assertEqual(
            intent.tool_plan[1].arguments["query"],
            "long sequence modeling limitations Transformer Mamba benchmark comparison",
        )
        self.assertNotIn("add_papers_to_project", intent.tool_names)


class ProjectToolsExecutionTest(TransactionTestCase):
    def setUp(self):
        from api.models import ProjectPaper, ResearchProject
        from papers.models import Paper

        self.project = ResearchProject.objects.create(title="Tool project")
        self.other_project = ResearchProject.objects.create(title="Other project")
        self.paper = Paper.objects.create(
            title="Included Paper",
            abstract="Included evidence",
            year=2024,
            citation_count=20,
            referenced_works=["W1", "W2"],
        )
        self.peer = Paper.objects.create(
            title="Peer Paper",
            abstract="Peer evidence",
            year=2023,
            citation_count=10,
            referenced_works=["W1", "W2"],
        )
        self.excluded = Paper.objects.create(
            title="Excluded Paper",
            abstract="Excluded evidence",
            year=2025,
            citation_count=100,
            referenced_works=["W1", "W2"],
        )
        self.other = Paper.objects.create(
            title="Other Project Paper",
            abstract="Other project evidence",
            year=2026,
            citation_count=999,
        )
        ProjectPaper.objects.create(project=self.project, paper=self.paper, status="included")
        ProjectPaper.objects.create(project=self.project, paper=self.peer, status="candidate")
        ProjectPaper.objects.create(project=self.project, paper=self.excluded, status="excluded")
        ProjectPaper.objects.create(project=self.other_project, paper=self.other, status="included")

    def test_add_papers_to_project_is_idempotent_and_keeps_global_paper(self):
        from agent.project_tools import add_papers_to_project
        from api.models import ProjectPaper
        from papers.models import Paper

        payload = [
            {
                "title": "New Tool Paper",
                "abstract": "Tool paper abstract",
                "year": 2026,
                "arxiv_id": "2601.00042",
                "citation_count": 3,
            }
        ]
        first = asyncio.run(add_papers_to_project(self.project.id, payload, "first add"))
        second = asyncio.run(add_papers_to_project(self.project.id, payload, "updated reason"))

        self.assertEqual(first["count"], 1)
        self.assertTrue(first["added"][0]["created"])
        self.assertEqual(second["count"], 1)
        self.assertFalse(second["added"][0]["created"])
        paper = Paper.objects.get(arxiv_id="2601.00042")
        link = ProjectPaper.objects.get(project=self.project, paper=paper)
        self.assertEqual(link.source_reason, "updated reason")
        self.assertEqual(ProjectPaper.objects.filter(project=self.project, paper=paper).count(), 1)
        self.assertTrue(Paper.objects.filter(id=paper.id).exists())

    def test_list_project_papers_returns_project_scope_and_statuses(self):
        from agent.project_tools import list_project_papers

        result = asyncio.run(list_project_papers(self.project.id))
        titles = [paper["title"] for paper in result["papers"]]

        self.assertEqual(result["count"], 3)
        self.assertEqual(titles[0], "Excluded Paper")
        self.assertNotIn("Other Project Paper", titles)
        statuses = {paper["title"]: paper["status"] for paper in result["papers"]}
        self.assertEqual(statuses["Included Paper"], "included")
        self.assertEqual(statuses["Excluded Paper"], "excluded")

    def test_project_paper_ids_excludes_excluded_and_other_projects(self):
        from agent.project_tools import project_paper_ids

        ids = asyncio.run(project_paper_ids(self.project.id))

        self.assertIn(self.paper.id, ids)
        self.assertIn(self.peer.id, ids)
        self.assertNotIn(self.excluded.id, ids)
        self.assertNotIn(self.other.id, ids)

    def test_query_project_rag_metadata_fallback_uses_project_scope(self):
        from agent.project_tools import query_project_rag

        result = asyncio.run(query_project_rag(self.project.id, "what evidence?", k=5))
        titles = [item["title"] for item in result["evidence"]]

        self.assertEqual(result["fallback"], "项目论文尚未完成全文向量入库，已使用元数据回答。")
        self.assertIn("Included Paper", titles)
        self.assertIn("Peer Paper", titles)
        self.assertNotIn("Excluded Paper", titles)
        self.assertNotIn("Other Project Paper", titles)
        self.assertTrue(all(item["source_marker"] for item in result["evidence"]))

    def test_search_result_filter_prefers_query_anchored_titles(self):
        from agent.project_tools import _rank_and_filter_search_results

        papers = [
            {"title": "A Survey of Large Language Models", "abstract": "transformer models", "citation_count": 999, "year": 2026},
            {"title": "A Survey on Visual Mamba", "abstract": "vision state space models", "citation_count": 30, "year": 2024},
            {"title": "Long Sequence Modeling Benchmark for Transformers", "abstract": "evaluation", "citation_count": 10, "year": 2025},
        ]
        result = _rank_and_filter_search_results(
            "long sequence modeling limitations Transformer Mamba benchmark comparison",
            papers,
            max_results=2,
        )
        titles = [paper["title"] for paper in result]

        self.assertEqual(len(titles), 2)
        self.assertNotIn("A Survey of Large Language Models", titles)

    def test_query_project_rag_rcs_timeout_uses_metadata_source_markers(self):
        from agent.project_tools import query_project_rag
        from rag.models import Text

        Text.objects.create(
            paper=self.paper,
            docname="Included Paper chunk 0",
            chunk_index=0,
            content="slow evidence",
            embedding=[1.0, 0.0],
            citation_key="pqac-slow",
        )

        async def slow_retrieve(*_args, **_kwargs):
            await asyncio.sleep(0.05)
            return []

        with (
            mock.patch("agent.project_tools.PROJECT_RAG_RCS_TIMEOUT_SECONDS", 0.001),
            mock.patch("rag.retrieval.retrieve_evidence", slow_retrieve),
            self.assertLogs("agent.project_tools", level="WARNING") as logs,
        ):
            result = asyncio.run(query_project_rag(self.project.id, "Mamba evidence", k=3))

        self.assertEqual(result["fallback"], "全文 RAG 评分超时，已使用项目论文元数据回答。")
        self.assertGreaterEqual(len(result["evidence"]), 1)
        self.assertTrue(all(item["source_marker"] for item in result["evidence"]))
        self.assertIn("project_rag_rcs_timeout", [record.event for record in logs.records])

    def test_draft_report_section_includes_metadata_source_markers(self):
        from agent.project_tools import draft_report_section

        result = asyncio.run(draft_report_section(self.project.id, "写一段项目综述"))

        self.assertIn("Included Paper", result["section"])
        self.assertIn("Peer Paper", result["section"])
        self.assertNotIn("Excluded Paper", result["section"])

    def test_get_project_citation_graph_excludes_excluded_papers(self):
        from agent.project_tools import get_project_citation_graph

        result = asyncio.run(get_project_citation_graph(self.project.id))
        titles = {node["title"] for node in result["graph"]["nodes"]}

        self.assertEqual(titles, {"Included Paper", "Peer Paper"})
        self.assertGreaterEqual(len(result["graph"]["edges"]), 1)

    def test_execute_project_tool_logs_sanitized_start_and_completion(self):
        from agent.project_tools import execute_project_tool

        with self.assertLogs("agent.project_tools", level="INFO") as logs:
            result = asyncio.run(
                execute_project_tool(
                    "query_project_rag",
                    {"project_id": self.project.id, "question": "x" * 200, "k": 2},
                )
            )

        self.assertEqual(len(result["evidence"]), 2)
        output = "\n".join(logs.output)
        self.assertIn("project tool started", output)
        self.assertIn("project tool completed", output)
        tool_names = [getattr(record, "tool_name", "") for record in logs.records]
        previews = [getattr(record, "question_preview", "") for record in logs.records]
        self.assertEqual(tool_names, ["query_project_rag", "query_project_rag"])
        self.assertTrue(all(len(preview) <= 120 for preview in previews if preview))
        for preview in previews:
            self.assertNotIn("x" * 160, preview)

    def test_execute_project_tool_unknown_tool_returns_error(self):
        from agent.project_tools import execute_project_tool

        result = asyncio.run(execute_project_tool("missing_tool", {}))

        self.assertEqual(result, {"error": "unknown tool missing_tool"})


class ProjectHarnessToolRoutingTest(TransactionTestCase):
    def setUp(self):
        from api.models import ProjectPaper, ResearchProject
        from papers.models import Paper

        self.project = ResearchProject.objects.create(title="Routing")
        paper = Paper.objects.create(
            title="Mamba",
            abstract="Selective state spaces",
            year=2023,
            referenced_works=["W1", "W2"],
        )
        ProjectPaper.objects.create(project=self.project, paper=paper, status="included")

    def test_harness_routes_all_non_destructive_tool_intents(self):
        from agent.harness import ProjectAgentHarness

        async def fake_execute(name, args):
            if name == "search_papers":
                return {"papers": [{"title": "Paper A", "year": 2025}], "count": 1}
            if name == "add_papers_to_project":
                return {"added": [{"title": "Paper A", "created": True}], "count": 1}
            if name == "query_project_rag":
                return {"evidence": [{"title": "Paper A", "summary": "Evidence"}], "fallback": ""}
            if name == "list_project_papers":
                return {"papers": [{"title": "Paper A", "year": 2025, "venue": "DBLP", "status": "candidate"}], "count": 1}
            if name == "get_project_citation_graph":
                return {"graph": {"nodes": [{"id": 1}], "edges": []}}
            if name == "draft_report_section":
                return {"section": "## Section\n\n- Evidence"}
            return {"error": "unknown"}

        cases = {
            "继续检索 DBLP 中 Mamba 后续工作并加入项目库": ["search_papers", "add_papers_to_project", "query_project_rag"],
            "列出当前论文库有哪些论文": ["list_project_papers"],
            "刷新引用关系图谱": ["get_project_citation_graph"],
            "生成一个报告章节": ["query_project_rag", "draft_report_section"],
        }
        with mock.patch("agent.harness.execute_project_tool", fake_execute):
            for message, expected in cases.items():
                result = asyncio.run(ProjectAgentHarness(self.project.id).run(message))
                tools = [
                    event["data"]["name"]
                    for event in result["events"]
                    if event["event"] == "tool_call"
                ]
                self.assertEqual(tools, expected)
                self.assertTrue(result["answer"].strip())

    def test_harness_blocks_destructive_intent_without_tool_call(self):
        from agent.harness import ProjectAgentHarness

        with mock.patch("agent.harness.execute_project_tool") as execute:
            result = asyncio.run(ProjectAgentHarness(self.project.id).run("清空项目并删除所有论文"))
        execute.assert_not_called()
        self.assertIn("不会自主执行", result["answer"])

    def test_harness_logs_run_lifecycle_without_full_message(self):
        from agent.harness import ProjectAgentHarness

        long_message = "Mamba 有什么特点？" + "需要详细分析" * 30
        with self.assertLogs("agent.harness", level="INFO") as captured:
            result = asyncio.run(ProjectAgentHarness(self.project.id).run(long_message))

        started = next(record for record in captured.records if record.event == "project_agent_run_started")
        completed = next(record for record in captured.records if record.event == "project_agent_run_completed")
        self.assertEqual(started.project_id, self.project.id)
        self.assertEqual(completed.project_id, self.project.id)
        self.assertGreater(completed.run_id, 0)
        self.assertGreater(completed.session_id, 0)
        self.assertEqual(completed.status, "done")
        self.assertEqual(completed.intent, "answer")
        self.assertGreater(completed.answer_chars, 0)
        self.assertLessEqual(len(started.message_preview), 120)
        self.assertNotIn(long_message, "\n".join(captured.output))
        self.assertTrue(result["answer"].strip())

    def test_harness_logs_failed_run(self):
        from agent.harness import ProjectAgentHarness

        async def broken_execute(_name, _args):
            raise RuntimeError("tool exploded")

        with self.assertLogs("agent.harness", level="INFO") as captured:
            result = asyncio.run(ProjectAgentHarness(self.project.id, tool_executor=broken_execute).run("Mamba 有什么特点？"))

        failed = next(record for record in captured.records if record.event == "project_agent_run_failed")
        self.assertEqual(failed.project_id, self.project.id)
        self.assertEqual(failed.status, "error")
        self.assertEqual(failed.error, "RuntimeError")
        self.assertIn("tool exploded", result["events"][-1]["data"]["message"])

    def test_harness_emits_quality_check_for_grounded_answer(self):
        from agent.harness import ProjectAgentHarness

        result = asyncio.run(ProjectAgentHarness(self.project.id).run("Mamba 有什么特点？"))
        quality = next(event["data"] for event in result["events"] if event["event"] == "quality_check")
        self.assertEqual(quality["verdict"], "grounded")
        self.assertGreaterEqual(quality["evidence_count"], 1)
        self.assertGreaterEqual(quality["source_marker_count"], 1)

    def test_harness_tool_timeout_returns_partial_answer(self):
        from agent.harness import ProjectAgentHarness

        async def slow_execute(name, args):
            await asyncio.sleep(0.05)
            return {"papers": [], "count": 0}

        result = asyncio.run(
            ProjectAgentHarness(
                self.project.id,
                tool_executor=slow_execute,
                tool_timeout_seconds=0.001,
            ).run("继续检索 Mamba 后续论文")
        )
        tool_result = next(event["data"] for event in result["events"] if event["event"] == "tool_result")
        quality = next(event["data"] for event in result["events"] if event["event"] == "quality_check")
        self.assertEqual(tool_result["status"], "error")
        self.assertEqual(tool_result["error"], "tool_timeout")
        self.assertEqual(quality["verdict"], "partial")
        self.assertIn("超时", result["answer"])

    def test_harness_live_llm_answer_uses_model_output(self):
        from agent.harness import ProjectAgentHarness

        class FakeClient:
            def complete(self, messages, **kwargs):
                payload = messages[-1]["content"]
                assert "project_id" in payload
                return {
                    "content": "Mamba 的重点是选择性状态空间，用 Mamba 作为来源回答。",
                    "usage": {"prompt_tokens": 10, "completion_tokens": 8, "total_tokens": 18},
                }

        result = asyncio.run(
            ProjectAgentHarness(
                self.project.id,
                use_llm=True,
                llm_client_factory=lambda: FakeClient(),
            ).run("Mamba 有什么特点？")
        )
        events = [event["event"] for event in result["events"]]
        self.assertIn("llm_call", events)
        self.assertIn("llm_result", events)
        self.assertIn("选择性状态空间", result["answer"])
        llm_result = next(event["data"] for event in result["events"] if event["event"] == "llm_result")
        self.assertEqual(llm_result["status"], "ok")
        quality = next(event["data"] for event in result["events"] if event["event"] == "quality_check")
        self.assertEqual(quality["verdict"], "grounded")

    def test_harness_live_llm_failure_falls_back(self):
        from agent.harness import ProjectAgentHarness

        class BrokenClient:
            def complete(self, messages, **kwargs):
                raise RuntimeError("model unavailable")

        result = asyncio.run(
            ProjectAgentHarness(
                self.project.id,
                use_llm=True,
                llm_client_factory=lambda: BrokenClient(),
            ).run("Mamba 有什么特点？")
        )
        llm_result = next(event["data"] for event in result["events"] if event["event"] == "llm_result")
        self.assertEqual(llm_result["status"], "fallback")
        self.assertTrue(result["answer"].strip())
        quality = next(event["data"] for event in result["events"] if event["event"] == "quality_check")
        self.assertIn(quality["verdict"], {"grounded", "needs_source_markers"})
