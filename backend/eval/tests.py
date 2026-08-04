"""eval 测试：dataset + recall_at_k（纯逻辑，不依赖外网/LLM）。"""
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
        self.assertTrue(rag_case["source_marker_present"])
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
