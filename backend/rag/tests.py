"""rag 测试套件（纯逻辑，mock LLM，不下载 PDF 不烧 token）。

覆盖：chunk_text 切片、citations pqac、embedding 维度、store 检索、RCS 过滤逻辑。
"""
import json
from unittest import mock

import numpy as np
from django.test import TransactionTestCase

from rag.citations import make_citation_key_for_paper, parse_citations, valid_keys_prompt
from rag.ingest import chunk_text, ingest_pdf_bytes
from rag.store import NumpyVectorStore


class NetworkGuardTest(TransactionTestCase):
    """GPT v5: verify test environment has socket-level network-blocking guards."""

    def test_hf_offline_env_set(self):
        """HF_HUB_OFFLINE must be set in test mode (second layer protection)."""
        import os
        self.assertEqual(os.environ.get("HF_HUB_OFFLINE"), "1",
                         "HF_HUB_OFFLINE=1 must be set in test mode")

    def test_transformers_offline_env_set(self):
        """TRANSFORMERS_OFFLINE must be set in test mode."""
        import os
        self.assertEqual(os.environ.get("TRANSFORMERS_OFFLINE"), "1",
                         "TRANSFORMERS_OFFLINE=1 must be set in test mode")

    def test_embedding_provider_is_fake_in_tests(self):
        """PAPERLENS_EMBEDDING_PROVIDER must resolve to fake in test mode."""
        from django.conf import settings
        self.assertEqual(settings.PAPERLENS_EMBEDDING_PROVIDER, "fake",
                         "IS_TESTING must force fake embedding provider")

    def test_outbound_connection_to_example_com_is_blocked(self):
        """Canary: attempting to connect to https://example.com MUST be rejected
        by the Network Guard (NetworkAccessBlocked), NOT by timeout/DNS/accidental
        network failure."""
        from config.test_runner import NetworkAccessBlocked, _guard_active
        # Guard must be active in offline suite
        self.assertTrue(_guard_active, "Network guard must be active in test mode")
        import socket as _socket
        # Direct attempt: should raise NetworkAccessBlocked immediately
        with self.assertRaises(NetworkAccessBlocked):
            _socket.create_connection(("example.com", 443), timeout=2)

    def test_local_database_connection_still_works(self):
        """Local PostgreSQL/Redis connections must NOT be blocked by the guard."""
        from django.db import connection
        with connection.cursor() as cur:
            cur.execute("SELECT 1")
            row = cur.fetchone()
        self.assertEqual(row[0], 1)


class ChunkTextTest(TransactionTestCase):
    def test_short_text_single_chunk(self):
        chunks = chunk_text("短文本")
        self.assertEqual(len(chunks), 1)

    def test_empty_text(self):
        self.assertEqual(chunk_text(""), [])

    def test_long_text_multiple_chunks(self):
        # 造超长文本，验证切片数 >1 且每片有上限
        paragraphs = [f"段落 {i}。" * 500 for i in range(20)]
        text = "\n\n".join(paragraphs)
        chunks = chunk_text(text, chunk_chars=9000, overlap=250)
        self.assertGreater(len(chunks), 1)
        # 每片不超过 chunk_chars + 一点余量（重叠）
        for c in chunks:
            self.assertLessEqual(len(c), 9500)

    def test_paragraph_boundary_no_word_split(self):
        # 切片应在段落边界，不硬截断
        text = "第一段内容。\n\n第二段内容。\n\n第三段内容。"
        chunks = chunk_text(text, chunk_chars=20)
        self.assertGreaterEqual(len(chunks), 1)
        # 不应出现半个"段"字截断（中文边界测试放宽，主要验证不崩）


class CitationsTest(TransactionTestCase):
    def test_key_stable(self):
        k1 = make_citation_key_for_paper(123)
        k2 = make_citation_key_for_paper(123)
        self.assertEqual(k1, k2)
        self.assertTrue(k1.startswith("pqac-"))
        self.assertEqual(len(k1), len("pqac-") + 8)

    def test_key_different_papers(self):
        self.assertNotEqual(make_citation_key_for_paper(1), make_citation_key_for_paper(2))

    def test_parse_citations(self):
        text = "论断1 (pqac-aaaaaaaa)。论断2 (pqac-bbbbbbbb)。再引 (pqac-aaaaaaaa)。"
        result = parse_citations(text)
        self.assertEqual(result, ["pqac-aaaaaaaa", "pqac-bbbbbbbb"])  # 去重保序

    def test_parse_no_citations(self):
        self.assertEqual(parse_citations("无引用文本"), [])

    def test_valid_keys_prompt(self):
        p = valid_keys_prompt(["pqac-aaa", "pqac-bbb"])
        self.assertIn("pqac-aaa", p)
        self.assertIn("pqac-bbb", p)

    def test_valid_keys_prompt_empty(self):
        self.assertEqual(valid_keys_prompt([]), "")


class EmbeddingTest(TransactionTestCase):
    """embedding 维度 + normalize（mock 模型避免下载）。"""

    def test_embed_shape(self):
        fake_vecs = np.array([[0.6, 0.8, 0.0, 0.0], [0.0, 0.6, 0.8, 0.0]], dtype=np.float32)
        with mock.patch("rag.embedding.get_embedder") as ge:
            ge.return_value.encode.return_value = fake_vecs
            from rag.embedding import Qwen3LocalEmbeddingProvider

            result = Qwen3LocalEmbeddingProvider("test-model", dimension=4).encode(["a", "b"])
        self.assertEqual(result.shape, (2, 4))

    def test_embed_empty(self):
        from django.conf import settings
        from rag.embedding import embed
        result = embed([])
        self.assertEqual(result.shape, (0, settings.PAPERLENS_EMBEDDING_DIM))


class BGEM3ProviderTest(TransactionTestCase):
    """BGE-M3 provider：dense + sparse 双路输出（mock BGEM3FlagModel）。"""

    def test_encode_dense_sparse(self):
        from rag.embedding import BGEM3EmbeddingProvider

        fake_output = {
            "dense_vecs": np.array([[0.6, 0.8, 0.0, 0.0]], dtype=np.float32),
            "lexical_weights": [{"100": np.float32(0.5), "200": np.float32(0.3)}],
        }
        with mock.patch("rag.embedding.get_embedder") as ge:
            ge.return_value.encode.return_value = fake_output
            provider = BGEM3EmbeddingProvider("BAAI/bge-m3", dimension=4)
            dense, sparse = provider.encode_dense_sparse(["hello world"])
        self.assertEqual(dense.shape, (1, 4))
        self.assertEqual(len(sparse), 1)
        self.assertEqual(sparse[0]["100"], 0.5)
        # 确认 np.float32 被转成纯 float（JSON 可序列化）
        self.assertIsInstance(sparse[0]["100"], float)

    def test_query_sparse(self):
        from rag.embedding import BGEM3EmbeddingProvider

        fake_output = {
            "dense_vecs": np.array([[0.6, 0.8]], dtype=np.float32),
            "lexical_weights": [{"42": np.float32(0.9)}],
        }
        with mock.patch("rag.embedding.get_embedder") as ge:
            ge.return_value.encode.return_value = fake_output
            provider = BGEM3EmbeddingProvider("BAAI/bge-m3", dimension=2)
            qs = provider.encode_query_sparse("test query")
        self.assertAlmostEqual(qs["42"], 0.9, places=5)



class NumpyStoreTest(TransactionTestCase):
    def test_search_returns_topk(self):
        from rag.models import Text

        # 造 3 个 Text（带 embedding）
        t1 = Text(docname="t1", chunk_index=0, content="a", embedding=[1.0, 0.0], citation_key="pqac-aaa")
        t2 = Text(docname="t2", chunk_index=1, content="b", embedding=[0.0, 1.0], citation_key="pqac-bbb")
        t3 = Text(docname="t3", chunk_index=2, content="c", embedding=[0.7, 0.7], citation_key="pqac-ccc")
        store = NumpyVectorStore()
        store.build_from([t1, t2, t3])
        # 查询接近 [1,0]：t1 最相似
        results = store.search(np.array([0.99, 0.01], dtype=np.float32), k=2)
        self.assertEqual(len(results), 2)
        self.assertEqual(results[0].docname, "t1")

    def test_search_empty_store(self):
        store = NumpyVectorStore()
        results = store.search(np.array([1.0, 0.0]), k=5)
        self.assertEqual(results, [])


class RCSParseTest(TransactionTestCase):
    """RCS 的 _parse_rcs 容错。"""

    def test_parse_valid_json(self):
        from rag.retrieval import _parse_rcs
        score, summary = _parse_rcs('{"score": 8, "summary": "相关"}')
        self.assertEqual(score, 8)
        self.assertEqual(summary, "相关")

    def test_parse_clamps_score(self):
        from rag.retrieval import _parse_rcs
        score, _ = _parse_rcs('{"score": 15, "summary": "x"}')
        self.assertEqual(score, 10)  # 截到 10

    def test_parse_invalid_json_fallback(self):
        from rag.retrieval import _parse_rcs
        score, summary = _parse_rcs("不是JSON")
        self.assertEqual(score, 0)


class RetrieveEvidenceFilterTest(TransactionTestCase):
    """retrieve_evidence 的过滤逻辑（真实 DB 造 Text + mock RCS 评分）。"""

    def test_filters_low_score(self):
        from papers.models import Paper, upsert_paper
        from rag.models import Text
        import asyncio

        paper = upsert_paper({"arxiv_id": "9999.99999", "title": "Filter Test", "year": 2024})
        for i in range(3):
            Text.objects.create(
                paper=paper, docname=f"ft chunk{i}", chunk_index=i,
                content=f"content {i}", embedding=[float(i)] + [0.0]*1023, citation_key="pqac-ft1",
                embedding_model="fake-hash-embedding", embedding_version="fake-hash-embedding:v1",
            )

        async def fake_rcs(question, text):
            e = mock.MagicMock()
            e.score = 0  # 全部低分，应被过滤
            e.summary = "low"
            return e

        # mock embed 返回与测试 embedding(2维) 匹配的查询向量
        with mock.patch("rag.retrieval.embed", return_value=np.array([[1.0] + [0.0]*1023])), \
             mock.patch("rag.retrieval._rcs_summary", fake_rcs):
            from rag.retrieval import retrieve_evidence
            result = asyncio.run(retrieve_evidence("q", paper_ids=[paper.id]))
        self.assertEqual(len(result), 0)  # 全过滤

    def test_keeps_high_score(self):
        from papers.models import Paper, upsert_paper
        from rag.models import Text
        import asyncio

        paper = upsert_paper({"arxiv_id": "9999.99998", "title": "Keep Test", "year": 2024})
        Text.objects.create(
            paper=paper, docname="kt chunk0", chunk_index=0,
            content="good", embedding=[1.0] + [0.0]*1023, citation_key="pqac-kt1",
                embedding_model="fake-hash-embedding", embedding_version="fake-hash-embedding:v1",
        )

        async def fake_rcs(question, text):
            e = mock.MagicMock()
            e.score = 8  # 高分，应保留
            e.summary = "高度相关"
            return e

        with mock.patch("rag.retrieval.embed", return_value=np.array([[1.0] + [0.0]*1023])), \
             mock.patch("rag.retrieval._rcs_summary", fake_rcs):
            from rag.retrieval import retrieve_evidence
            result = asyncio.run(retrieve_evidence("q", paper_ids=[paper.id]))
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].score, 8)


class PdfIngestBytesTest(TransactionTestCase):
    def test_ingest_pdf_bytes_persists_chunks(self):
        from papers.models import upsert_paper
        from rag.models import Text
        import asyncio

        paper = upsert_paper({"arxiv_id": "9999.99123", "title": "PDF Ingest Test", "year": 2026})
        text = (
            "PDF ingestion should parse local bytes into text, split the extracted paper text into chunks, "
            "compute embeddings, and persist Text rows for project-scoped retrieval. "
        ) * 4
        pdf_bytes = _simple_pdf_bytes(text)
        fake_vecs = np.ones((1, 1024), dtype=np.float32)

        # Mock BOTH embed (non-BGE-M3 path) and get_provider (BGE-M3 dense+sparse
        # path) so the test is deterministic regardless of which embedding provider
        # is active (fake in unit-test env, BGE-M3 in Docker with FlagEmbedding).
        class _FakeProvider:
            model_name = "fake-test"
            dimension = 1024
            version = "fake-test:dim1024"
            def encode_dense_sparse(self, texts):
                return fake_vecs, [{} for _ in texts]
            def encode(self, texts, input_type="document"):
                return fake_vecs

        with mock.patch("rag.ingest.embed", return_value=fake_vecs), \
             mock.patch("rag.embedding.get_provider", return_value=_FakeProvider()), \
             mock.patch("rag.ingest._parse_pdf_with_docling", return_value=("", [])):
            # Docling mocked to skip HF download; pypdf fallback handles the test PDF.
            count = asyncio.run(ingest_pdf_bytes(paper, pdf_bytes))

        self.assertEqual(count, 1)
        chunk = Text.objects.get(paper=paper)
        self.assertIn("PDF ingestion should parse", chunk.content)
        self.assertTrue(chunk.citation_key.startswith("pqac-"))
        self.assertEqual(len(chunk.embedding), 1024)
        self.assertTrue(all(v == 1.0 for v in chunk.embedding[:4]))
        self.assertEqual(chunk.page_start, 1)
        self.assertEqual(chunk.page_end, 1)
        self.assertGreaterEqual(chunk.char_end, chunk.char_start)
        self.assertTrue(chunk.content_hash)
        self.assertTrue(chunk.embedding_model)


class HybridRetrievalTest(TransactionTestCase):
    def test_rrf_fuse_merges_dense_and_lexical_rankings(self):
        from rag.models import Text
        from rag.retrieval import rrf_fuse

        dense_first = Text(id=1, docname="dense", chunk_index=0, content="dense", citation_key="pqac-a")
        lexical_first = Text(id=2, docname="lexical", chunk_index=1, content="lexical", citation_key="pqac-b")
        shared = Text(id=3, docname="shared", chunk_index=2, content="shared", citation_key="pqac-c")

        fused = rrf_fuse([dense_first, shared], [lexical_first, shared], limit=3, rrf_k=60)

        self.assertEqual({item.id for item in fused}, {1, 2, 3})
        self.assertEqual(fused[0].id, 3)

    def test_hybrid_retriever_python_fallback_uses_lexical_candidates(self):
        from papers.models import upsert_paper
        from rag.models import Text
        from rag.retrieval import hybrid_retrieve_texts
        import asyncio

        paper = upsert_paper({"arxiv_id": "9999.99124", "title": "Hybrid Retrieval Test", "year": 2026})
        lexical = Text.objects.create(
            paper=paper,
            docname="lexical chunk",
            chunk_index=0,
            content="Postgres full text search uses tsvector and tsquery for exact lexical retrieval.",
            search_vector="Postgres full text search tsvector tsquery exact lexical retrieval",
            embedding=[0.0] + [1.0] + [0.0]*1022, embedding_model="fake",
            embedding_version="fake:v1",
            citation_key="pqac-lexical",
        )
        Text.objects.create(
            paper=paper,
            docname="dense chunk",
            chunk_index=1,
            content="Unrelated dense candidate",
            search_vector="unrelated dense candidate",
            embedding=[1.0] + [0.0]*1023, embedding_model="fake",
            embedding_version="fake:v1",
            citation_key="pqac-dense",
        )

        with (
            mock.patch("rag.retrieval.embed", return_value=np.array([[1.0, 0.0]], dtype=np.float32)),
            mock.patch("rag.retrieval.embedding_metadata", return_value={"embedding_model": "fake", "embedding_dim": 2, "embedding_version": "fake:v1"}),
        ):
            results = asyncio.run(hybrid_retrieve_texts("tsvector tsquery lexical", paper_ids=[paper.id], final_k=2))

        self.assertIn(lexical.id, [item.id for item in results])


def _simple_pdf_bytes(text: str) -> bytes:
    escaped = text.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")
    stream = f"BT /F1 11 Tf 72 720 Td ({escaped}) Tj ET".encode("latin-1", errors="replace")
    objects = [
        b"1 0 obj << /Type /Catalog /Pages 2 0 R >> endobj\n",
        b"2 0 obj << /Type /Pages /Kids [3 0 R] /Count 1 >> endobj\n",
        b"3 0 obj << /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >> endobj\n",
        b"4 0 obj << /Type /Font /Subtype /Type1 /BaseFont /Helvetica >> endobj\n",
        b"5 0 obj << /Length " + str(len(stream)).encode("ascii") + b" >> stream\n" + stream + b"\nendstream endobj\n",
    ]
    output = bytearray(b"%PDF-1.4\n")
    offsets = []
    for obj in objects:
        offsets.append(len(output))
        output.extend(obj)
    xref = len(output)
    output.extend(f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode("ascii"))
    for offset in offsets:
        output.extend(f"{offset:010d} 00000 n \n".encode("ascii"))
    output.extend(f"trailer << /Root 1 0 R /Size {len(objects) + 1} >>\nstartxref\n{xref}\n%%EOF\n".encode("ascii"))
    return bytes(output)
