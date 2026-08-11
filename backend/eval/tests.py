"""eval 测试：dataset + recall_at_k（纯逻辑，不依赖外网/LLM）。"""
from unittest import mock

from django.test import TestCase, TransactionTestCase

from api.models import ProjectPaper, ResearchProject
from eval.agent_harness import PROJECT_AGENT_EVAL_CASES, run_project_agent_eval_sync, tool_policy_summary
from eval.agent_quality import run_agent_quality_eval
from eval.dataset import EVAL_ITEMS, type_distribution
from eval.intent_eval import INTENT_EVAL_CASES, evaluate_intent_classifier
from eval.live_agent import _evaluate_graph_case
from eval.metrics import recall_at_k
from eval.pdf_rag import run_pdf_rag_eval
from papers.models import Paper


class DatasetTest(TestCase):
    def test_at_least_10_items(self):
        self.assertGreaterEqual(len(EVAL_ITEMS), 10)

    def test_covers_three_types(self):
        dist = type_distribution()
        for t in ("factual", "recent", "compare"):
            self.assertIn(t, dist)
            self.assertGreaterEqual(dist[t], 1)

    def test_each_item_has_gold(self):
        for it in EVAL_ITEMS:
            self.assertTrue(it.question)
            self.assertTrue(it.gold_queries)
            self.assertTrue(it.gold_titles)


class RecallAtKTest(TestCase):
    def test_hit_returns_one(self):
        titles = ["Attention Is All You Need", "Other Paper"]
        self.assertEqual(recall_at_k(titles, ["attention is all you need"], k=10), 1.0)

    def test_miss_returns_zero(self):
        titles = ["Some Paper", "Another Paper"]
        self.assertEqual(recall_at_k(titles, ["attention is all you need"], k=10), 0.0)

    def test_only_checks_top_k(self):
        # gold 在第 11 位，k=10 应 miss
        titles = [f"Paper {i}" for i in range(10)] + ["Attention Is All You Need"]
        self.assertEqual(recall_at_k(titles, ["attention is all you need"], k=10), 0.0)
        # k=11 命中
        self.assertEqual(recall_at_k(titles, ["attention is all you need"], k=11), 1.0)

    def test_partial_keyword_match(self):
        # gold 关键词 "mamba" 在标题里
        titles = ["Vision Mamba", "Other"]
        self.assertEqual(recall_at_k(titles, ["mamba"], k=10), 1.0)

    def test_empty_gold(self):
        self.assertEqual(recall_at_k(["x"], [], k=10), 0.0)

    def test_case_insensitive(self):
        titles = ["ATTENTION IS ALL YOU NEED"]
        self.assertEqual(recall_at_k(titles, ["attention is all you need"], k=10), 1.0)


class IntentEvalTest(TestCase):
    def test_intent_golden_matrix_passes(self):
        result = evaluate_intent_classifier(project_id=9)
        self.assertTrue(result["passed"], result["failed"])
        self.assertEqual(result["total"], len(INTENT_EVAL_CASES))
        self.assertGreaterEqual(result["total"], 30)

    def test_intent_eval_reports_diagnostics(self):
        result = evaluate_intent_classifier(project_id=9)
        first = result["cases"][0]
        self.assertIn("expected_intent", first)
        self.assertIn("actual_intent", first)
        self.assertIn("expected_tools", first)
        self.assertIn("actual_tools", first)
        self.assertIn("rationale", first)


class ProjectAgentHarnessEvalTest(TransactionTestCase):
    def setUp(self):
        self.project = ResearchProject.objects.create(title="Harness eval")
        paper = Paper.objects.create(
            title="Mamba: Linear-Time Sequence Modeling with Selective State Spaces",
            abstract="Selective state spaces make long sequence modeling efficient.",
            year=2023,
        )
        ProjectPaper.objects.create(project=self.project, paper=paper, status="included")

    def test_tool_policy_blocks_destructive_surface(self):
        result = tool_policy_summary()
        self.assertTrue(result["passed"])
        self.assertEqual(result["destructive_tools_exposed"], [])

    def test_project_agent_eval_runs_without_network(self):
        result = run_project_agent_eval_sync(self.project.id, include_network=False)
        self.assertTrue(result["passed"])
        self.assertEqual(len(result["cases"]), len(PROJECT_AGENT_EVAL_CASES))

        case_ids = {case["id"] for case in result["cases"]}
        self.assertIn("project_rag_grounded_answer", case_ids)
        self.assertIn("project_library_inventory", case_ids)
        self.assertIn("project_citation_graph", case_ids)
        self.assertIn("search_add_report_combined", case_ids)
        self.assertIn("destructive_action_blocked", case_ids)

        search_case = next(case for case in result["cases"] if case["id"] == "search_add_function_call_policy")
        self.assertTrue(search_case["passed"])
        self.assertEqual(search_case["mode"], "offline-fixture")
        self.assertIn("search_papers", search_case["tools"])
        self.assertIn("add_papers_to_project", search_case["tools"])
        self.assertEqual(search_case["quality_verdict"], "grounded")

        rag_case = next(case for case in result["cases"] if case["id"] == "project_rag_grounded_answer")
        # Task 4.x: the offline fixture has only metadata evidence, so the
        # factual contract fails closed (compliant abstention) while the
        # evidence was still retrieved.
        self.assertTrue(rag_case["safety_replaced"],
                        "metadata-only factual must fail closed under the capability policy")
        self.assertGreater(rag_case["evidence_count"], 0)

        blocked_case = next(case for case in result["cases"] if case["id"] == "destructive_action_blocked")
        self.assertTrue(blocked_case["blocked"])
        self.assertEqual(blocked_case["tools"], [])
        self.assertEqual(blocked_case["forbidden_tools_observed"], [])

    def test_project_agent_eval_reports_diagnostics_for_failures(self):
        result = run_project_agent_eval_sync(self.project.id, include_network=False)
        for case in result["cases"]:
            with self.subTest(case=case["id"]):
                self.assertIn("expected_intent", case)
                self.assertIn("detected_intent", case)
                self.assertIn("missing_events", case)
                self.assertIn("missing_tools", case)
                self.assertIn("forbidden_tools_observed", case)
                self.assertIn("source_marker_present", case)
                self.assertIn("duration_ms", case)
                self.assertIn("event_count", case)


class AgentQualityEvalTest(TransactionTestCase):
    def setUp(self):
        self.project = ResearchProject.objects.create(title="Agent quality")
        paper = Paper.objects.create(
            title="Mamba: Linear-Time Sequence Modeling with Selective State Spaces",
            abstract="Selective state spaces make long sequence modeling efficient.",
            year=2023,
            referenced_works=["W1", "W2"],
        )
        peer = Paper.objects.create(
            title="Attention Is All You Need",
            abstract="The Transformer architecture replaces recurrence with self-attention.",
            year=2017,
            referenced_works=["W1", "W3"],
        )
        ProjectPaper.objects.create(project=self.project, paper=paper, status="included")
        ProjectPaper.objects.create(project=self.project, paper=peer, status="included")

    def test_agent_quality_eval_reports_resume_metrics(self):
        result = run_agent_quality_eval(self.project.id, include_network=False)

        self.assertTrue(result["passed"])
        self.assertGreaterEqual(result["score"], 0.95)
        metrics = result["metrics"]
        self.assertEqual(metrics["intent_routing"]["intent_accuracy"], 1.0)
        self.assertEqual(metrics["function_calling"]["tool_trajectory_accuracy"], 1.0)
        self.assertTrue(metrics["function_calling"]["safe_tool_policy"])
        self.assertTrue(metrics["function_calling"]["search_expand_routes_to_tools"])
        self.assertEqual(metrics["rag_grounding"]["partial_answer_rate"], 0)
        self.assertEqual(metrics["prompt_engineering"]["contract_coverage"], 1.0)
        self.assertTrue(metrics["execution_harness"]["timeout_recovery_passed"])
        self.assertEqual(metrics["execution_harness"]["quality_check_event_rate"], 1.0)
        self.assertTrue(metrics["mcp_surface"]["safe_surface"])
        self.assertTrue(metrics["data_sources"]["dblp_default"])

    def test_agent_quality_eval_emits_structured_logs(self):
        with self.assertLogs("eval.agent_quality", level="INFO") as logs:
            run_agent_quality_eval(self.project.id, include_network=False)

        events = [getattr(record, "event", "") for record in logs.records]
        self.assertIn("agent_quality_evaluation_started", events)
        self.assertIn("agent_quality_evaluation_completed", events)
        completed = next(record for record in logs.records if record.event == "agent_quality_evaluation_completed")
        self.assertEqual(completed.status, "passed")
        self.assertEqual(completed.intent_accuracy, 1.0)
        self.assertEqual(completed.tool_trajectory_accuracy, 1.0)


class PdfRagQualityEvalTest(TransactionTestCase):
    def test_pdf_rag_eval_runs_local_fulltext_path(self):
        # Mock Docling to skip HF model download — tests use pypdf fallback only.
        # Real Docling/BGE-M3 testing is in the live integration suite (not here).
        from rag.ingest import _parse_pdf_with_pypdf
        with mock.patch("rag.ingest._parse_pdf_with_docling",
                        return_value=("", [])):
            result = run_pdf_rag_eval(include_live_agent=False, use_production_embedding=False)

        self.assertTrue(result["passed"])
        self.assertEqual(result["embedding_mode"], "lexical_fixture")
        self.assertEqual(result["summary"]["papers_ingested"], 3)
        self.assertGreaterEqual(result["summary"]["chunks_ingested"], 3)
        self.assertEqual(result["summary"]["rag_cases"], 3)
        self.assertEqual(result["summary"]["rag_cases_passed"], 3)
        for case in result["rag_cases"]:
            self.assertTrue(case["passed"], case)
            self.assertGreater(case["evidence_count"], 0)
            self.assertEqual(case["fallback"], "")
            self.assertGreater(case["source_marker_count"], 0)


class LiveAgentStructuredEvalTest(TestCase):
    def test_graph_case_uses_structured_artifacts(self):
        critic = _evaluate_graph_case(
            "已刷新项目 Citation Map：2 个节点，1 条关系。"
            "Attention Is All You Need 和 Mamba 共享 referenced_works。",
            {
                "graph": {
                    "node_count": 2,
                    "edge_count": 1,
                    "nodes": [
                        {"title": "Attention Is All You Need"},
                        {"title": "Mamba"},
                    ],
                    "edges": [
                        {
                            "source_title": "Attention Is All You Need",
                            "target_title": "Mamba",
                        }
                    ],
                }
            },
        )

        self.assertTrue(critic["passed"])
        self.assertGreaterEqual(critic["score"], 0.9)


class ToolMetricsTest(TestCase):
    """Pure-function tests for the §6.2 tool-decision indicators."""

    def test_precision_recall_when_actual_equals_gold(self):
        from eval.tool_metrics import set_precision, set_recall

        gold = ["query_project_rag", "draft_report_section"]
        self.assertEqual(set_precision(gold, gold), 1.0)
        self.assertEqual(set_recall(gold, gold), 1.0)

    def test_precision_drops_when_extra_tools_called(self):
        from eval.tool_metrics import set_precision

        actual = ["query_project_rag", "draft_report_section", "search_papers"]
        gold = ["query_project_rag", "draft_report_section"]
        # 2 of 3 called tools were gold -> 0.667.
        self.assertAlmostEqual(set_precision(actual, gold), 2 / 3, places=4)

    def test_recall_zero_when_nothing_called(self):
        from eval.tool_metrics import set_recall

        self.assertEqual(set_recall([], ["query_project_rag"]), 0.0)

    def test_ordering_satisfied_and_violated(self):
        from eval.tool_metrics import ordering_accuracy

        # search before add before rag: correct order -> 1.0.
        self.assertEqual(
            ordering_accuracy(["search_papers", "add_papers_to_project", "query_project_rag"],
                              ("search_papers", "add_papers_to_project", "query_project_rag")),
            1.0,
        )
        # Fully reversed: every adjacent pair is violated -> 0.0.
        self.assertEqual(
            ordering_accuracy(["query_project_rag", "add_papers_to_project", "search_papers"],
                              ("search_papers", "add_papers_to_project", "query_project_rag")),
            0.0,
        )
        # No constraint -> 1.0.
        self.assertEqual(ordering_accuracy(["x"], None), 1.0)

    def test_redundant_call_rate_detects_repeats(self):
        from eval.tool_metrics import redundant_call_rate

        calls = [
            {"name": "search_papers", "arguments": {"query": "mamba"}},
            {"name": "search_papers", "arguments": {"query": "mamba"}},  # repeat -> redundant
            {"name": "query_project_rag", "arguments": {"question": "q"}},
        ]
        self.assertAlmostEqual(redundant_call_rate(calls), 1 / 3, places=4)
        # Different arguments -> not redundant.
        unique = [
            {"name": "search_papers", "arguments": {"query": "mamba"}},
            {"name": "search_papers", "arguments": {"query": "transformer"}},
        ]
        self.assertEqual(redundant_call_rate(unique), 0.0)

    def test_argument_validity_requires_correct_project_id(self):
        from eval.tool_metrics import argument_validity

        calls = [
            {"name": "query_project_rag", "arguments": {"project_id": 7, "question": "q"}},
            {"name": "search_papers", "arguments": {"project_id": 9, "query": "x"}},  # wrong pid
        ]
        self.assertAlmostEqual(argument_validity(calls, 7), 0.5, places=4)
        # All correct.
        good = [{"name": "rag", "arguments": {"project_id": 7, "q": "q"}}]
        self.assertEqual(argument_validity(good, 7), 1.0)

    def test_loop_exhaustion_rate(self):
        from eval.tool_metrics import loop_exhaustion_rate

        self.assertEqual(loop_exhaustion_rate([2, 8, 3, 8], max_iterations=8), 0.5)
        self.assertEqual(loop_exhaustion_rate([2, 3], max_iterations=8), 0.0)

    def test_aggregate_over_cases(self):
        from eval.tool_metrics import aggregate_tool_metrics

        cases = [
            {
                "tools": ["query_project_rag", "draft_report_section"],
                "expected_tools": ["query_project_rag", "draft_report_section"],
                "expected_order": ("query_project_rag", "draft_report_section"),
                "tool_calls": [{"name": "rag", "arguments": {"project_id": 1, "q": "x"}}],
                "project_id": 1,
            },
            {
                "tools": [],
                "expected_tools": [],
                "expected_order": None,
                "tool_calls": [],
                "project_id": 1,
            },
        ]
        agg = aggregate_tool_metrics(cases)
        self.assertEqual(agg["tool_selection_precision"], 1.0)
        self.assertEqual(agg["tool_selection_recall"], 1.0)
        self.assertEqual(agg["ordering_accuracy"], 1.0)
        self.assertEqual(agg["argument_validity"], 1.0)
        self.assertEqual(agg["redundant_call_rate"], 0.0)


class AgentHarnessToolDecisionFieldsTest(TransactionTestCase):
    """The harness result now records tool_calls / expected_order for §6.2.

    Uses TransactionTestCase (like the other harness eval tests) because
    run_project_agent_eval_sync runs an asyncio loop that writes runs/events,
    which conflicts with TestCase's wrapped transaction on SQLite.
    """

    def test_eval_cases_carry_expected_order_where_it_matters(self):
        order_cases = [c for c in PROJECT_AGENT_EVAL_CASES if c.expected_order]
        # The 3 multi-step cases (report + 2 search_add) declare an order.
        self.assertEqual(len(order_cases), 3)
        for c in order_cases:
            # expected_order must be a subset of expected_tools.
            self.assertTrue(all(t in c.expected_tools for t in c.expected_order))

    def test_run_project_agent_eval_records_tool_calls_and_metrics(self):
        from datetime import datetime
        from api.demo import seed_demo_project

        title = f"tool-decision eval {datetime.utcnow().strftime('%Y%m%d-%H%M%S')}"
        pid = seed_demo_project(title, reuse=False, status="archived")["project"]["id"]
        result = run_project_agent_eval_sync(pid, include_network=False)
        for case in result["cases"]:
            if case["expected_tools"]:
                # Each eligible case now records full per-call records, not just names.
                self.assertIn("tool_calls", case)
                self.assertIsInstance(case["tool_calls"], list)
                self.assertIn("expected_order", case)
                self.assertIn("iteration_count", case)


class ConversationEvalScaffoldTest(TransactionTestCase):
    """Validate the multi-turn conversation scaffold (manual §5.5/§6.6).

    These tests confirm the driver mechanics (turn advance, per-turn trajectory
    capture, session reuse) work offline. The deterministic router's
    multi-turn *understanding* (reference resolution, constraint retention) is
    intentionally NOT asserted here — that is LLM-level behaviour covered by the
    real-model release gate. Router quality gaps surfaced by the scaffold are
    recorded in docs/internal/product-agent-report-20260808.md as known risks.
    """

    def test_conversation_cases_are_well_formed(self):
        from eval.conversation_eval import CONVERSATION_CASES

        self.assertGreaterEqual(len(CONVERSATION_CASES), 3)
        for case in CONVERSATION_CASES:
            self.assertGreaterEqual(len(case.turns), 2, f"{case.id} must have >=2 turns")
            for turn in case.turns:
                self.assertTrue(turn.message.strip())
                # expect_no_search and forbid_tools are the two anti-over-search levers.
                self.assertIsInstance(turn.expect_tools, tuple)

    def test_driver_advances_turns_and_captures_trajectory(self):
        from datetime import datetime
        from api.demo import seed_demo_project
        from eval.conversation_eval import CONVERSATION_CASES, run_conversation_eval_sync

        pid = seed_demo_project(
            f"conv-scaffold {datetime.utcnow().strftime('%Y%m%d-%H%M%S')}",
            reuse=False, status="archived",
        )["project"]["id"]
        result = run_conversation_eval_sync(pid, use_llm=False)

        # The driver must run every case and every turn.
        self.assertEqual(result["case_count"], len(CONVERSATION_CASES))
        self.assertEqual(result["total_turns"], sum(len(c.turns) for c in CONVERSATION_CASES))
        for case_result in result["cases"]:
            case = next(c for c in CONVERSATION_CASES if c.id == case_result["id"])
            self.assertEqual(len(case_result["turns"]), len(case.turns))
            for turn in case_result["turns"]:
                # Each turn records a real tool list (possibly empty) and a verdict.
                self.assertIsInstance(turn["actual_tools"], list)
                self.assertIn("passed", turn)
                self.assertIn("answer_preview", turn)


class EvidenceTierClassificationTest(TestCase):
    """GPT v5: reverse tests for evidence tier + citation reference logic."""

    def test_evidence_available_but_no_citation_is_not_fulltext(self):
        """有 evidence、无引用 → answer_evidence_tier=none(unbound),不是 fulltext。"""
        from eval.wave1a_runner import _classify_answer_evidence_tier, _classify_citation_binding
        quality = {"answer_mode": "answered", "evidence_count": 5, "resolved_citation_count": 0,
                   "citations": [{"marker": "pqac-x", "citation_marker_status": "absent", "reference_resolved": False}]}
        self.assertEqual(_classify_answer_evidence_tier(quality), "none")
        self.assertEqual(_classify_citation_binding(quality), "unbound")

    def test_marker_present_but_reference_unresolved_is_unbound(self):
        """marker 出现在答案但无法解析到 paper/chunk → unbound,不是 fulltext。"""
        from eval.wave1a_runner import _classify_answer_evidence_tier, _classify_citation_binding
        quality = {"answer_mode": "answered", "evidence_count": 3, "resolved_citation_count": 0,
                   "citations": [{"marker": "pqac-fake", "citation_marker_status": "present", "reference_resolved": False}]}
        self.assertEqual(_classify_answer_evidence_tier(quality), "none")
        self.assertEqual(_classify_citation_binding(quality), "unbound")

    def test_metadata_only_action_result_not_fulltext(self):
        """action_result(metadata 层)不得判 fulltext。"""
        from eval.wave1a_runner import _classify_answer_evidence_tier
        quality = {"answer_mode": "action_result", "evidence_count": 0, "resolved_citation_count": 0}
        self.assertEqual(_classify_answer_evidence_tier(quality), "metadata")

    def test_partial_claims_resolved_is_partially_bound(self):
        """部分引用解析到 chunk、部分未解析 → partially_bound。"""
        from eval.wave1a_runner import _classify_citation_binding
        quality = {"answer_mode": "answered", "evidence_count": 3, "resolved_citation_count": 2,
                   "citations": [
                       {"marker": "a", "citation_marker_status": "present", "reference_resolved": True},
                       {"marker": "b", "citation_marker_status": "present", "reference_resolved": True},
                       {"marker": "c", "citation_marker_status": "present", "reference_resolved": False},
                   ]}
        self.assertEqual(_classify_citation_binding(quality), "partially_bound")

    def test_safety_replaced_abstention_is_none_tier(self):
        """安全门替换后的拒答 → answer_evidence_tier=none。"""
        from eval.wave1a_runner import _classify_answer_evidence_tier
        quality = {"answer_mode": "abstained", "evidence_count": 0, "resolved_citation_count": 0}
        self.assertEqual(_classify_answer_evidence_tier(quality), "none")

    def test_fully_bound_when_all_resolved(self):
        """全部引用解析到真实 chunk → fully_bound。"""
        from eval.wave1a_runner import _classify_citation_binding
        quality = {"answer_mode": "answered", "evidence_count": 2, "resolved_citation_count": 2,
                   "citations": [
                       {"marker": "a", "citation_marker_status": "present", "reference_resolved": True},
                       {"marker": "b", "citation_marker_status": "present", "reference_resolved": True},
                   ]}
        self.assertEqual(_classify_citation_binding(quality), "fully_bound")
