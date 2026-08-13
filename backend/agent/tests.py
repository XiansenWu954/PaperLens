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
from rag.embedding import embedding_metadata
from agent.state import add


def _active_version(paper):
    """Test fixture: the CURRENT active index version for a paper.

    Reuses an existing active row (the DB enforces one active version per
    paper) or creates one carrying the fake provider's embedding metadata, so
    active_only retrieval (ING-B-CX-05) can find chunks attached to it.
    """
    from rag.models import PaperIndexVersion

    meta = embedding_metadata()
    version = PaperIndexVersion.objects.filter(
        paper=paper, status="active").order_by("-id").first()
    if version is None:
        version = PaperIndexVersion.objects.create(
            paper=paper, status="active",
            source_sha256="active-fixture",
            pipeline_signature="test-active-v1",
            parser_identity="test",
            chunk_config_hash="test-v1",
            embedding_model=meta["embedding_model"],
            embedding_version=meta["embedding_version"],
            embedding_dim=int(meta["embedding_dim"]),
        )
    return version


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
    """execute_tool 测试。默认完全离线(mock datasources);live-source 测试单独标记。"""

    def test_execute_search_papers(self):
        """离线:mock datasources 返回 fixture,不访问 OpenAlex/arXiv。"""
        from agent.tools import execute_tool
        from papers.models import Paper

        # Mock the datasource registry so no real HTTP calls happen.
        fixture_papers = [
            {"source": "openalex", "source_id": "W123", "title": "Mock Transformer Paper",
             "abstract": "mock abstract", "year": 2024, "authors": ["Mock Author"], "venue": "Mock"}
        ]

        async def _mock_search(query, max_results=5):
            return fixture_papers

        with mock.patch("datasources.registry.search", _mock_search):
            result_json = asyncio.run(
                execute_tool("search_papers", {"query": "transformer attention", "max_results": 2})
            )
        papers = json.loads(result_json)
        self.assertGreaterEqual(len(papers), 1)
        self.assertIn("title", papers[0])
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

        _v_slow = _active_version(self.paper)
        Text.objects.create(
            paper=self.paper,
            index_version=_v_slow,
            docname="Included Paper chunk 0",
            chunk_index=0,
            content="slow evidence",
            embedding=[1.0] + [0.0] * 1023,  # must be 1024-dim for pgvector
            embedding_model=embedding_metadata()["embedding_model"],
            embedding_dim=1024,
            embedding_version=embedding_metadata()["embedding_version"],
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

    def test_draft_report_section_uses_cite_marker_format(self):
        """报告章节的来源标记应统一为 [cite:标识] 格式，便于前端识别与信任标注。"""
        from agent.project_tools import draft_report_section

        result = asyncio.run(draft_report_section(self.project.id, "写综述"))
        # 应含 [cite: 前缀（而非裸括号）
        self.assertIn("[cite:", result["section"])

    def test_get_project_citation_graph_excludes_excluded_papers(self):
        from agent.project_tools import get_project_citation_graph

        result = asyncio.run(get_project_citation_graph(self.project.id))
        titles = {node["title"] for node in result["graph"]["nodes"]}

        self.assertEqual(titles, {"Included Paper", "Peer Paper"})
        self.assertGreaterEqual(len(result["graph"]["edges"]), 1)

    def test_execute_project_tool_logs_sanitized_start_and_completion(self):
        from agent.context import create_context
        from agent.project_tools import execute_project_tool

        with self.assertLogs("agent.project_tools", level="INFO") as logs:
            result = asyncio.run(
                execute_project_tool(
                    create_context(self.project.id),
                    "query_project_rag",
                    {"question": "x" * 200, "k": 2},
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
        from agent.context import create_context
        from agent.project_tools import execute_project_tool

        result = asyncio.run(
            execute_project_tool(create_context(self.project.id), "missing_tool", {})
        )

        self.assertEqual(result, {"error": "unknown tool missing_tool"})


class ComparePapersEvidenceTest(TransactionTestCase):
    """Evidence-based compare_papers: per-paper fulltext RAG, evidence_gap, metadata fallback."""

    def setUp(self):
        from api.models import ProjectPaper, ResearchProject
        from papers.models import Paper
        self.project = ResearchProject.objects.create(title="Compare evidence test", status="active")
        self.paper_a = Paper.objects.create(title="Paper A on selective state spaces", abstract="SSM abstract", year=2023, arxiv_id="a-compare")
        self.paper_b = Paper.objects.create(title="Paper B on self-attention", abstract="attention abstract", year=2017, arxiv_id="b-compare")
        self.paper_no_fulltext = Paper.objects.create(title="Paper C metadata only", abstract="no PDF", year=2024, arxiv_id="c-compare")
        ProjectPaper.objects.create(project=self.project, paper=self.paper_a, status="included")
        ProjectPaper.objects.create(project=self.project, paper=self.paper_b, status="included")
        ProjectPaper.objects.create(project=self.project, paper=self.paper_no_fulltext, status="included")
        # seed a couple of fulltext chunks for A and B (fake embeddings OK for structure test)
        from rag.models import Text
        _meta = embedding_metadata()
        _v_a = _active_version(self.paper_a)
        _v_b = _active_version(self.paper_b)
        Text.objects.create(paper=self.paper_a, index_version=_v_a, docname="a0", chunk_index=0, content="selective state space model scan mechanism",
                            embedding=[0.1]*1024, embedding_model=_meta["embedding_model"], embedding_dim=1024,
                            embedding_version=_meta["embedding_version"],
                            content_hash="h1", search_vector="Paper A selective state space", citation_key="pqac-a1")
        Text.objects.create(paper=self.paper_b, index_version=_v_b, docname="b0", chunk_index=0, content="self-attention scaled dot-product multi-head",
                            embedding=[0.2]*1024, embedding_model=_meta["embedding_model"], embedding_dim=1024,
                            embedding_version=_meta["embedding_version"],
                            content_hash="h2", search_vector="Paper B self-attention", citation_key="pqac-b1")

    def test_compare_returns_evidence_chunks_not_abstracts(self):
        from agent.project_tools import compare_papers
        result = asyncio.run(compare_papers(self.project.id, [self.paper_a.id, self.paper_b.id], "method"))
        self.assertNotIn("error", result)
        for p in result["papers"]:
            self.assertEqual(p["evidence_source"], "fulltext_hybrid_rag")
            self.assertGreater(len(p["chunks"]), 0)
            # chunks carry page/section/citation, not just abstract
            self.assertIn("citation", p["chunks"][0])

    def test_compare_flags_metadata_fallback_for_no_fulltext(self):
        from agent.project_tools import compare_papers
        result = asyncio.run(compare_papers(self.project.id, [self.paper_a.id, self.paper_no_fulltext.id], "method"))
        no_ft = next(p for p in result["papers"] if p["paper_id"] == self.paper_no_fulltext.id)
        self.assertEqual(no_ft["evidence_source"], "metadata_fallback")
        self.assertEqual(no_ft["chunks"], [])
        self.assertIn("evidence_gaps", result)
        self.assertTrue(any(g["paper_id"] == self.paper_no_fulltext.id for g in result["evidence_gaps"]))
        self.assertLess(result["paper_coverage"], 1.0)

    def test_compare_rejects_fewer_than_two_papers(self):
        from agent.project_tools import compare_papers
        result = asyncio.run(compare_papers(self.project.id, [self.paper_a.id], "method"))
        self.assertIn("error", result)

    def test_compare_project_isolation(self):
        """compare_papers must only return chunks for papers in the given project."""
        from agent.project_tools import compare_papers
        from api.models import ProjectPaper, ResearchProject
        other = ResearchProject.objects.create(title="Other project", status="active")
        # paper_a linked to self.project, NOT other — compare against other must not see it
        result = asyncio.run(compare_papers(other.id, [self.paper_a.id, self.paper_b.id], "method"))
        # papers not in the project → fewer than 2 resolved → error
        self.assertIn("error", result)



class ProjectHarnessToolRoutingTest(TransactionTestCase):
    def setUp(self):
        from api.models import ProjectPaper, ResearchProject
        from papers.models import Paper

        self.project = ResearchProject.objects.create(title="Routing")
        self.paper = Paper.objects.create(
            title="Mamba",
            abstract="Selective state spaces",
            year=2023,
            referenced_works=["W1", "W2"],
        )
        ProjectPaper.objects.create(project=self.project, paper=self.paper, status="included")

    def test_harness_routes_all_non_destructive_tool_intents(self):
        from agent.harness import ProjectAgentHarness

        async def fake_execute(context, name, args):
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
        # §30.3: the user message is never logged verbatim — only length/hash.
        self.assertGreaterEqual(started.message_chars, 120)
        self.assertTrue(started.message_hash)
        self.assertNotIn(long_message, "\n".join(captured.output))
        self.assertTrue(result["answer"].strip())

    def test_harness_logs_failed_run(self):
        from agent.harness import ProjectAgentHarness

        async def broken_execute(_context, _name, _args):
            raise RuntimeError("tool exploded")

        with self.assertLogs("agent.harness", level="INFO") as captured:
            result = asyncio.run(ProjectAgentHarness(self.project.id, tool_executor=broken_execute).run("Mamba 有什么特点？"))

        failed = next(record for record in captured.records if record.event == "project_agent_run_failed")
        self.assertEqual(failed.project_id, self.project.id)
        self.assertEqual(failed.status, "error")
        self.assertEqual(failed.error, "RuntimeError")
        # §31.1: the error event carries the stable code + fixed copy only —
        # the raw exception message is never serialized.
        error_ev = result["events"][-1]["data"]
        self.assertEqual(error_ev["error"], "RuntimeError")
        self.assertTrue(error_ev.get("error_hash"))
        self.assertNotIn("tool exploded", json.dumps(result["events"], default=str))

    def test_harness_emits_quality_check_for_grounded_answer(self):
        from agent.harness import ProjectAgentHarness

        # Task 4.x: this fixture has only metadata evidence (no fulltext), so
        # the factual contract fails closed — the quality event is still
        # structured and the answer is the standard abstention.
        result = asyncio.run(ProjectAgentHarness(self.project.id).run("Mamba 有什么特点？"))
        quality = next(event["data"] for event in result["events"] if event["event"] == "quality_check")
        self.assertIn(quality["verdict"], ("grounded", "needs_source_markers"))
        self.assertGreaterEqual(quality["evidence_count"], 1)
        self.assertEqual(quality["answer_mode"], "abstained",
                         "metadata-only factual must fail closed")
        self.assertIs(quality["safety_replaced"], True)
        self.assertIn("citations", quality)
        self.assertTrue(all("marker" in c and "verified" in c for c in quality["citations"]))

    def test_harness_tool_timeout_returns_partial_answer(self):
        from agent.harness import ProjectAgentHarness

        async def slow_execute(context, name, args):
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
        # §24.3: a tool error/timeout must not be presented as success — the
        # deterministic failure note replaces the model's answer.
        self.assertIn("未能成功完成", result["answer"])
        self.assertTrue(quality["action_failed"])

    def test_harness_live_llm_answer_uses_model_output(self):
        """ReAct 模式:LLM 先调 query_project_rag 取证据,再生成答案。

        Task 4.x: fixture 提供真实 ACTIVE fulltext chunk，使 factual 契约
        （resolved + bound）满足，答案不被替换。RCS/embed 用 deterministic mock。
        """
        from agent.harness import ProjectAgentHarness
        from rag.embedding import embedding_metadata
        from rag.models import Text
        from unittest import mock

        meta = embedding_metadata()
        _v_mamba = _active_version(self.paper)
        Text.objects.create(
            paper=self.paper, index_version=_v_mamba, docname="mamba chunk", chunk_index=0,
            content="Mamba 使用选择性状态空间模型。",
            embedding=[1.0] + [0.0] * 1023,
            embedding_model=meta["embedding_model"], embedding_dim=1024,
            embedding_version=meta["embedding_version"],
            content_hash="h_mamba", citation_key="pqac-mamba",
            search_vector="Mamba selective state space model",
        )
        call_count = [0]

        class FakeClient:
            def complete_with_tools(self, messages, tools, **kwargs):
                call_count[0] += 1
                if call_count[0] == 1:
                    # 第一轮:调 RAG 工具取证据(project_id 由 ChatAgentLoop 自动注入)
                    return {"content": "", "tool_calls": [
                        {"id": "call_1", "name": "query_project_rag",
                         "arguments": '{"question": "Mamba 特点", "k": 6}'}]}
                # 第二轮:基于证据生成最终答案(不再调工具)。§21.4: 绑定只认
                # [cite:<marker>] token —— 工具输出 source_marker 即 citation_key。
                return {"content": "Mamba 的重点是选择性状态空间，用 Mamba 作为来源回答。 [cite:pqac-mamba]",
                        "tool_calls": []}
            def complete(self, messages, **kwargs):
                return {"content": "", "usage": {}}

        async def fake_rcs(question, text):
            from rag.models import Evidence
            return Evidence(text=text, question=question,
                            summary=text.content[:100], score=8.0,
                            citation_key=text.citation_key)

        with mock.patch("llm.deepseek.DeepSeekClient") as mc, \
             mock.patch("rag.retrieval.embed", return_value=__import__("numpy").array([[1.0] + [0.0] * 1023])), \
             mock.patch("rag.retrieval._rcs_summary", new=mock.AsyncMock(side_effect=fake_rcs)):
            mc.return_value = FakeClient()
            result = asyncio.run(
                ProjectAgentHarness(self.project.id, use_llm=True).run("Mamba 有什么特点？")
            )
        events = [event["event"] for event in result["events"]]
        # ReAct 模式应发出 agent_mode + tool_call + tool_result
        self.assertIn("agent_mode", events)
        self.assertIn("tool_call", events)
        self.assertIn("选择性状态空间", result["answer"])
        quality = next(event["data"] for event in result["events"] if event["event"] == "quality_check")
        self.assertEqual(quality["verdict"], "grounded")
        self.assertEqual(quality["answer_mode"], "answered",
                         "resolved+bound factual answer must not be replaced")

    def test_harness_live_llm_failure_falls_back(self):
        """ReAct 模式 LLM 失败时,harness 应捕获异常并给出可恢复的答案。"""
        from agent.harness import ProjectAgentHarness
        from unittest import mock

        class BrokenClient:
            def complete_with_tools(self, *a, **kw):
                raise RuntimeError("model unavailable")
            def complete(self, *a, **kw):
                raise RuntimeError("model unavailable")

        with mock.patch("llm.deepseek.DeepSeekClient") as mc:
            mc.return_value = BrokenClient()
            result = asyncio.run(
                ProjectAgentHarness(self.project.id, use_llm=True).run("Mamba 有什么特点？")
            )
        # 失败时应 emit error 事件,answer 可能为空但有 done/error 事件
        events = [event["event"] for event in result["events"]]
        self.assertTrue("done" in events or "error" in events)

    def test_react_loop_round_trips_reasoning_content(self):
        """B1 (§3.3): DeepSeek thinking 模式工具调用后,下一轮请求必须带回
        reasoning_content,否则 API 可能返回 400。验证 ChatAgentLoop 把模型
        返回的 reasoning_content 原样回填到 assistant 消息。"""
        from agent.chat_loop import ChatAgentLoop

        captured_messages = []
        call_count = [0]

        class FakeClient:
            def complete_with_tools(self, messages, tools, **kwargs):
                call_count[0] += 1
                # 捕获每次请求的 messages,用于断言回填
                captured_messages.append([dict(m) for m in messages])
                if call_count[0] == 1:
                    return {
                        "content": "",
                        "reasoning_content": "I should query the project evidence first.",
                        "tool_calls": [{"id": "call_1", "name": "query_project_rag",
                                        "arguments": '{"question": "Mamba", "k": 6}'}],
                    }
                # 第二轮:基于证据回答,不再调工具
                return {"content": "Mamba 用选择性状态空间机制(来源:pqac-demo)。",
                        "reasoning_content": "Now I can answer.",
                        "tool_calls": []}

        import asyncio as _asyncio

        async def fake_executor(context, name, args):
            # Minimal evidence so the loop's tool-result message is well-formed;
            # the test only asserts reasoning_content round-trip, not tool behavior.
            if name == "query_project_rag":
                return {"evidence": [{"title": "Mamba", "summary": "selective SSM",
                                      "citation": "pqac-demo"}]}
            return {}

        async def drive():
            loop = ChatAgentLoop(self.project.id, tool_executor=fake_executor)
            events = []
            async for ev in loop.run("Mamba 有什么特点？", history=None):
                events.append(ev)
            return events

        with mock.patch("llm.deepseek.DeepSeekClient") as mc:
            mc.return_value = FakeClient()
            _asyncio.run(drive())

        # 第二次请求的 messages 应包含第一轮的 assistant 消息,且带 reasoning_content。
        self.assertGreaterEqual(len(captured_messages), 2)
        second_request_msgs = captured_messages[1]
        assistant_msgs = [m for m in second_request_msgs if m.get("role") == "assistant"]
        self.assertTrue(any("reasoning_content" in m for m in assistant_msgs),
                        "assistant 消息必须回填 reasoning_content 供下一轮请求")


class SecurityAdversarialTest(TransactionTestCase):
    """T1-T3: adversarial tests for project_id force-override + no-evidence gate."""

    def setUp(self):
        from api.models import ProjectPaper, ResearchProject
        from papers.models import Paper
        self.project = ResearchProject.objects.create(title="Security test project", status="active")
        self.other_project = ResearchProject.objects.create(title="Other project", status="active")
        # paper in OTHER project only
        self.other_paper = Paper.objects.create(title="Secret other-project paper", abstract="secret", year=2024)
        ProjectPaper.objects.create(project=self.other_project, paper=self.other_paper, status="included")
        # paper in THIS project
        self.own_paper = Paper.objects.create(title="Own project paper", abstract="public", year=2024)
        ProjectPaper.objects.create(project=self.project, paper=self.own_paper, status="included")

    def test_t1_project_id_force_override_blocks_cross_project_access(self):
        """T1: model sends project_id=other_project, but the executor receives
        the frozen trusted context of THIS project and no auth fields."""
        from agent.chat_loop import ChatAgentLoop
        from agent.context import ToolExecutionContext
        from unittest import mock

        captured_args = []

        async def fake_executor(context, name, args):
            # tool_executor contract is (context, name, args); the project
            # identity comes exclusively from the frozen context.
            captured_args.append({"name": name, "context": context, "args": dict(args)})
            if name == "query_project_rag":
                return {"evidence": [{"title": "Own paper", "summary": "public info", "citation": "pqac-own"}]}
            if name == "list_project_papers":
                return {"papers": [{"title": "Own project paper"}], "count": 1}
            return {}

        class FakeClient:
            def complete_with_tools(self, messages, tools, **kwargs):
                # Model tries to pass project_id = OTHER project (999)
                return {"content": "", "reasoning_content": "thinking",
                        "tool_calls": [{"id": "c1", "name": "list_project_papers",
                                        "arguments": '{"project_id": 999}'}]}
            def complete(self, *a, **kw):
                return {"content": "done", "usage": {}}

        import asyncio as _asyncio

        async def drive():
            loop = ChatAgentLoop(self.project.id, tool_executor=fake_executor)
            events = []
            async for ev in loop.run("list papers", history=None):
                events.append(ev)
            return events

        with mock.patch("llm.deepseek.DeepSeekClient") as mc:
            mc.return_value = FakeClient()
            _asyncio.run(drive())

        # T1 core assertion: executor received the frozen context of THIS project
        self.assertTrue(captured_args, "executor was never called")
        for ca in captured_args:
            self.assertIsInstance(ca["context"], ToolExecutionContext)
            self.assertEqual(ca["context"].project_id, self.project.id,
                             f"executor received project_id={ca['context'].project_id} instead of {self.project.id}")
            self.assertNotIn("project_id", ca["args"],
                             "smuggled project_id must not reach the tool implementation")

    def test_t2_no_evidence_safety_gate_replaces_domain_knowledge_answer(self):
        """T2: when evidence_count=0 and task needs evidence, harness replaces with abstention."""
        from agent.harness import ProjectAgentHarness
        from unittest import mock

        class FakeClient:
            def complete_with_tools(self, messages, tools, **kwargs):
                # Model answers with domain knowledge, no tool calls
                return {"content": "Mamba uses selective state space models for linear-time computation. " * 5,
                        "reasoning_content": "", "tool_calls": []}
            def complete(self, *a, **kw):
                return {"content": "", "usage": {}}

        with mock.patch("llm.deepseek.DeepSeekClient") as mc:
            mc.return_value = FakeClient()
            result = asyncio.run(
                ProjectAgentHarness(self.project.id, use_llm=True).run("Mamba 有什么特点？")
            )
        answer = result.get("answer", "")
        quality = next(e["data"] for e in result["events"] if e["event"] == "quality_check")
        # S2: answer should be replaced with abstention (not domain knowledge)
        self.assertIn("暂无相关证据", answer)
        self.assertNotIn("selective state space", answer)
        self.assertEqual(quality.get("answer_mode"), "abstained")
