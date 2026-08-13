"""BGE-M3 dense/sparse/hybrid 检索质量 ablation（P1-4 升级盲区评测）。

现有 evaluate_rag_quality 全程用词袋 mock，sparse 路径从不触发。
本模块用真实 BGE-M3 编码合成论文片段，对比 dense-only / sparse-only / hybrid 三路召回，
验证 BGE-M3 的 sparse 词级检索对缩写词/专有名词的增益。
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

# 合成论文片段：含缩写词/专有名词，sparse 应有优势（精确词级匹配）
EMBED_EVAL_CHUNKS = [
    {
        "id": 1,
        "title": "Mamba: Linear-Time Sequence Modeling with Selective State Spaces",
        "text": (
            "Mamba introduces a selective state space model (SSM) for efficient sequence modeling. "
            "Unlike Transformers, Mamba achieves linear time complexity through selective gating."
        ),
    },
    {
        "id": 2,
        "title": "Attention Is All You Need",
        "text": (
            "The Transformer architecture relies entirely on self-attention mechanisms, dispensing "
            "with recurrence. Scaled dot-product attention enables parallelizable training."
        ),
    },
    {
        "id": 3,
        "title": "Retrieval-Augmented Generation for Knowledge-Intensive NLP Tasks",
        "text": (
            "RAG combines parametric memory with non-parametric retrieval. The retriever fetches "
            "relevant documents from a dense index, grounding the generator in external knowledge."
        ),
    },
    {
        "id": 4,
        "title": "DBLP - A Computer Science Bibliography",
        "text": (
            "DBLP provides bibliographic metadata for computer science publications. It indexes "
            "venue names, authors, and titles for major CS conferences and journals."
        ),
    },
    {
        "id": 5,
        "title": "LoRA: Low-Rank Adaptation of Large Language Models",
        "text": (
            "LoRA freezes pre-trained weights and injects trainable low-rank decomposition matrices, "
            "reducing adapter parameters while maintaining fine-tuning quality."
        ),
    },
    {
        "id": 6,
        "title": "A Survey on Graph Neural Networks",
        "text": (
            "Graph Neural Networks (GNN) learn node representations via message passing. GCN, GAT, "
            "and GraphSAGE are foundational architectures for graph-structured data."
        ),
    },
]

# 查询 + gold（缩写词/专有名词查询，sparse 应能精确命中）
EMBED_EVAL_QUERIES = [
    {"query": "Mamba state space model", "gold_id": 1, "note": "专有名词"},
    {"query": "Transformer self-attention", "gold_id": 2, "note": "专有名词"},
    {"query": "RAG retrieval augmented generation", "gold_id": 3, "note": "缩写词"},
    {"query": "DBLP computer science bibliography", "gold_id": 4, "note": "缩写词"},
    {"query": "LoRA low-rank adaptation", "gold_id": 5, "note": "缩写词"},
    {"query": "GNN graph neural network", "gold_id": 6, "note": "缩写词"},
]


def _cosine_sim(a, b) -> float:
    import numpy as np
    return float(np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b) + 1e-9))


def _sparse_dot(query_sparse: dict, doc_sparse: dict) -> float:
    """sparse 词级权重点积（共享 token 的权重之和）。"""
    return sum(w * doc_sparse.get(tok, 0.0) for tok, w in query_sparse.items())


def run_embedding_ablation() -> dict[str, Any]:
    """用真实 BGE-M3 跑 dense/sparse/hybrid 三路 Recall@5 ablation。

    返回 {dense_recall, sparse_recall, hybrid_recall, per_query[], passed}。
    passed 判定：hybrid >= dense 且 hybrid_recall >= 0.6。
    """
    from rag.embedding import BGEM3EmbeddingProvider

    provider = BGEM3EmbeddingProvider("BAAI/bge-m3", dimension=1024)
    logger.info("embedding ablation: 编码 %d 文档片段（真实 BGE-M3）", len(EMBED_EVAL_CHUNKS))

    # 编码文档（dense + sparse）
    texts = [f"{c['title']}. {c['text']}" for c in EMBED_EVAL_CHUNKS]
    doc_dense, doc_sparse = provider.encode_dense_sparse(texts)

    per_query: list[dict[str, Any]] = []
    dense_hits = 0
    sparse_hits = 0
    hybrid_hits = 0
    for case in EMBED_EVAL_QUERIES:
        # 编码查询
        q_dense, q_sparse_list = provider.encode_dense_sparse([case["query"]])
        q_dense = q_dense[0]
        q_sparse = q_sparse_list[0]

        # dense 排序（余弦相似度）
        dense_scores = [(_cosine_sim(q_dense, doc_dense[i]), EMBED_EVAL_CHUNKS[i]["id"]) for i in range(len(EMBED_EVAL_CHUNKS))]
        dense_ranked = [tid for _s, tid in sorted(dense_scores, reverse=True)]

        # sparse 排序（词级权重点积）
        sparse_scores = [(_sparse_dot(q_sparse, doc_sparse[i]), EMBED_EVAL_CHUNKS[i]["id"]) for i in range(len(EMBED_EVAL_CHUNKS))]
        sparse_ranked = [tid for _s, tid in sorted(sparse_scores, reverse=True)]

        # hybrid（RRF 融合 dense+sparse，k=60）
        rrf_k = 60
        scores: dict[int, float] = {}
        for ranked in (dense_ranked, sparse_ranked):
            for rank, tid in enumerate(ranked, start=1):
                scores[tid] = scores.get(tid, 0.0) + 1.0 / (rrf_k + rank)
        hybrid_ranked = [tid for tid, _ in sorted(scores.items(), key=lambda x: -x[1])]

        gold = case["gold_id"]
        d_hit = gold in dense_ranked[:5]
        s_hit = gold in sparse_ranked[:5]
        h_hit = gold in hybrid_ranked[:5]
        dense_hits += d_hit
        sparse_hits += s_hit
        hybrid_hits += h_hit
        per_query.append({
            "query": case["query"],
            "note": case["note"],
            "gold_id": gold,
            "dense_top1": dense_ranked[0],
            "sparse_top1": sparse_ranked[0],
            "hybrid_top1": hybrid_ranked[0],
            "dense_hit": d_hit,
            "sparse_hit": s_hit,
            "hybrid_hit": h_hit,
        })

    n = len(EMBED_EVAL_QUERIES)
    result = {
        "dense_recall_at_5": round(dense_hits / n, 4),
        "sparse_recall_at_5": round(sparse_hits / n, 4),
        "hybrid_recall_at_5": round(hybrid_hits / n, 4),
        "per_query": per_query,
        "embedding_model": "BAAI/bge-m3 (real)",
        "case_count": n,
    }
    result["passed"] = result["hybrid_recall_at_5"] >= 0.6 and result["hybrid_recall_at_5"] >= result["dense_recall_at_5"]
    logger.info(
        "embedding ablation: dense=%.2f sparse=%.2f hybrid=%.2f passed=%s",
        result["dense_recall_at_5"], result["sparse_recall_at_5"],
        result["hybrid_recall_at_5"], result["passed"],
    )
    return result
