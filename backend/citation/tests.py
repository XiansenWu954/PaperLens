"""citation 测试套件（纯逻辑，无外网无 LLM）。

覆盖：norm_oid 归一化、build_similarity_graph 共参考边权重、label_nodes 三类标注、
to_vis_data 结构、summarize_for_synthesis。
"""
import networkx as nx
from django.test import TransactionTestCase

from citation.analyze import _percentile, label_nodes
from citation.graph_build import build_similarity_graph, norm_oid
from citation.visualize import summarize_for_synthesis, to_vis_data


class NormOidTest(TransactionTestCase):
    def test_url_to_id(self):
        self.assertEqual(norm_oid("https://openalex.org/W123"), "W123")

    def test_already_short(self):
        self.assertEqual(norm_oid("W456"), "W456")

    def test_empty(self):
        self.assertEqual(norm_oid(""), "")


class _FakePaper:
    """轻量假 Paper（避免建真 ORM 记录也能测图算法）。"""

    def __init__(self, pid, refs, title="t", year=2024, citation_count=10):
        self.id = pid
        self.referenced_works = refs
        self.title = title
        self.year = year
        self.citation_count = citation_count
        self.arxiv_id = None
        self.doi = None


class BuildGraphTest(TransactionTestCase):
    def test_coupling_edge_weight(self):
        # A 引 [W1,W2], B 引 [W1,W2,W3] → 共享 W1,W2，权重=2
        A = _FakePaper(1, ["W1", "W2"], "A")
        B = _FakePaper(2, ["W1", "W2", "W3"], "B")
        C = _FakePaper(3, ["W4"], "C")  # 无共享
        G = build_similarity_graph([A, B, C])
        self.assertEqual(G.number_of_nodes(), 3)
        self.assertTrue(G.has_edge(1, 2))
        self.assertEqual(G[1][2]["weight"], 2)
        self.assertFalse(G.has_edge(1, 3))  # C 无共享

    def test_url_refs_normalized(self):
        A = _FakePaper(1, ["https://openalex.org/W1"])
        B = _FakePaper(2, ["https://openalex.org/W1"])
        G = build_similarity_graph([A, B])
        self.assertTrue(G.has_edge(1, 2))
        self.assertEqual(G[1][2]["weight"], 1)

    def test_empty_refs(self):
        A = _FakePaper(1, [])
        B = _FakePaper(2, [])
        G = build_similarity_graph([A, B])
        self.assertEqual(G.number_of_edges(), 0)


class LabelNodesTest(TransactionTestCase):
    def test_labels_keys(self):
        # 造一个有边的图
        A = _FakePaper(1, ["W1", "W2"], "A", year=2020, citation_count=100)
        B = _FakePaper(2, ["W1", "W2"], "B", year=2024, citation_count=50)
        C = _FakePaper(3, ["W1"], "C", year=2024, citation_count=10)
        G = build_similarity_graph([A, B, C])
        labels = label_nodes(G, current_year=2026)
        self.assertEqual(set(labels.keys()), {1, 2, 3})
        for n, lab in labels.items():
            self.assertIn("seminal", lab)
            self.assertIn("frontier", lab)
            self.assertIn("cluster", lab)
            self.assertIn("is_root", lab)
            self.assertIn("is_frontier", lab)

    def test_frontier_decays_with_age(self):
        # 同 seminal，年份老的 frontier 更低
        A = _FakePaper(1, ["W1"], "A", year=2020)
        B = _FakePaper(2, ["W1"], "B", year=2025)
        G = build_similarity_graph([A, B])
        labels = label_nodes(G, current_year=2026)
        self.assertGreater(labels[2]["frontier"], labels[1]["frontier"])

    def test_empty_graph(self):
        labels = label_nodes(nx.Graph())
        self.assertEqual(labels, {})


class PercentileTest(TransactionTestCase):
    def test_p80(self):
        self.assertAlmostEqual(_percentile([1, 2, 3, 4, 5], 80), 5)

    def test_empty(self):
        self.assertEqual(_percentile([], 80), 0)


class VisDataTest(TransactionTestCase):
    def test_vis_structure(self):
        A = _FakePaper(1, ["W1", "W2"], "A", year=2024, citation_count=42)
        B = _FakePaper(2, ["W1", "W2"], "B", year=2024, citation_count=10)
        G = build_similarity_graph([A, B])
        labels = label_nodes(G)
        vis = to_vis_data(G, labels)
        self.assertIn("nodes", vis)
        self.assertIn("edges", vis)
        self.assertEqual(len(vis["nodes"]), 2)
        node = vis["nodes"][0]
        # 必需字段
        for key in ("id", "title", "year", "citation_count", "size", "cluster", "x", "y"):
            self.assertIn(key, node)
        # size ∝ citation_count
        sizes = sorted(n["size"] for n in vis["nodes"])
        self.assertEqual(sizes[-1], 42)
        # 边有权重
        self.assertEqual(vis["edges"][0]["weight"], 2)


class SummarizeTest(TransactionTestCase):
    def test_summarize_returns_text(self):
        A = _FakePaper(1, ["W1", "W2"], "PaperA", year=2024, citation_count=50)
        B = _FakePaper(2, ["W1", "W2"], "PaperB", year=2025, citation_count=30)
        G = build_similarity_graph([A, B])
        labels = label_nodes(G)
        summary = summarize_for_synthesis(labels, {1: A, 2: B})
        self.assertIn("引用图谱分析", summary)
        self.assertTrue(len(summary) > 10)

    def test_summarize_no_roots(self):
        summary = summarize_for_synthesis({}, {})
        self.assertIn("无明显聚类", summary)
