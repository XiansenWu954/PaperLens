"""Real arXiv PDF dataset for retrieval evaluation (deepseek-live-evaluation §4).

12 real papers across three disjoint topics, each with gold-chunk term
annotations and a dev/calibration/held-out split (§4.3). Gold matching uses a
*distinguishing term set* per paper rather than exact content strings, because
Docling markdown reflow + chunk overlap windows make exact-string matches
unstable. The term set is chosen so that each paper's gold terms are unlikely
to all co-occur in another paper's chunks.

Splits are grouped by paper (not random), so a paper never spans splits.
Held-out is frozen: Wave 1+ official conclusions use held-out only; dev is for
prompt/fixture tuning; calibration is for threshold setting.

PDFs live in backend/media/fixtures/pdf/ (gitignored). Each paper records its
arxiv_id so the ingest step can locate the file.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class RealPaper:
    arxiv_id: str
    title: str
    short_name: str
    year: int
    authors: list[str]
    topic: str  # "sequence" | "rag" | "graph"
    # Distinguishing terms that should co-occur in this paper's gold chunk(s).
    # Chosen to be specific enough that no other paper's chunks contain all of them.
    gold_terms: tuple[str, ...]
    # Claims the paper's full text supports (used for Evidence Sufficiency).
    supporting_claims: tuple[str, ...]
    # Claims the paper does NOT support (used to detect fabricated citations).
    unsupportable_claims: tuple[str, ...]
    split: str  # "dev" | "calibration" | "held_out"


@dataclass(frozen=True)
class RealRagCase:
    id: str
    question: str
    gold_arxiv_ids: tuple[str, ...]
    # Terms that must appear in retrieved evidence for the answer to be grounded.
    expected_terms: tuple[str, ...]
    # factual | compare | cross_lang | author_year | abbreviation | scope_leakage
    category: str
    allow_search: bool = False
    # For scope_leakage cases: terms that must NOT appear (proves no cross-project leak,
    # NOT a true abstention — true abstention needs the final LLM answer, Wave 1A).
    forbidden_terms: tuple[str, ...] = ()
    # For compare cases: optional sub-queries to drive a multi-query retrieval variant
    # (one sub-query per gold paper), used to test if splitting the comparison helps
    # recall both papers (d06-class weakness).
    sub_queries: tuple[str, ...] = ()


# --- The 12 papers (split: 5 dev / 2 calibration / 5 held-out) -------------
# Split by paper to prevent leakage; topics distributed across splits.

REAL_PAPERS: tuple[RealPaper, ...] = (
    # ---- dev (5): tune fixtures / inspect failures ----
    RealPaper("2312.00752", "Mamba: Linear-Time Sequence Modeling with Selective State Spaces", "Mamba", 2023,
              ["Albert Gu", "Tri Dao"], "sequence",
              ("selective state space", "S6", "hardware-aware", "linear time", "scan"),
              ("Mamba uses selective state space models for linear-time sequence modeling",
               "Mamba achieves faster inference than Transformers on long sequences"),
              ("Mamba uses self-attention", "Mamba is a graph neural network"), "dev"),
    RealPaper("1706.03762", "Attention Is All You Need", "Attention", 2017,
              ["Ashish Vaswani", "Noam Shazeer"], "sequence",
              ("self-attention", "transformer", "scaled dot-product", "multi-head", "encoder-decoder"),
              ("The Transformer replaces recurrence with self-attention",
               "Multi-head attention attends to different representation subspaces"),
              ("Transformer uses state space models", "Transformer is a retrieval method"), "dev"),
    RealPaper("2111.00396", "Efficiently Modeling Long Sequences with Structured State Spaces", "S4", 2021,
              ["Albert Gu", "Karolina Hejna"], "sequence",
              ("structured state space", "HiPPO", "continuous-time", "recurrent", "convolutional"),
              ("S4 models long sequences with structured state spaces via HiPPO"),
              ("S4 is a Transformer architecture"), "dev"),
    RealPaper("2208.03299", "Atlas: Few-shot Learning with Retrieval-augmented Language Models", "Atlas", 2022,
              ["Gautier Izacard"], "rag",
              ("retrieval-augmented", "few-shot", "memory", "dense retriever", "language model"),
              ("Atlas combines retrieval augmentation with few-shot learning"),
              ("Atlas is a graph neural network"), "dev"),
    RealPaper("1609.02907", "Semi-Supervised Classification with Graph Convolutional Networks", "GCN", 2017,
              ["Thomas Kipf", "Max Welling"], "graph",
              ("graph convolutional", "semi-supervised", "spectral", "node classification", "Chebyshev"),
              ("GCN performs semi-supervised node classification via spectral graph convolutions"),
              ("GCN is a sequence model"), "dev"),
    # ---- calibration (2): set thresholds ----
    RealPaper("2307.08621", "Retentive Network: A Successor to Transformer for Large Language Models", "RetNet", 2023,
              ["Yunchang Shen"], "sequence",
              ("retention", "recurrent", "parallel", "retentive network", "decay"),
              ("RetNet proposes retention as an alternative to Transformer attention"),
              ("RetNet is a retrieval-augmented method"), "calibration"),
    RealPaper("2112.09118", "Unsupervised Dense Information Retrieval with Contrastive Learning", "Contriever", 2021,
              ["Gautier Izacard"], "rag",
              ("contrastive", "unsupervised", "dense retrieval", "in-batch", "encoder"),
              ("Contriever learns dense retrieval without supervision via contrastive learning"),
              ("Contriever is a state space model"), "calibration"),
    # ---- held_out (5): frozen, Wave 1+ official conclusions only ----
    RealPaper("2305.13048", "RWKV: Reinventing RNNs for the Transformer Era", "RWKV", 2023,
              ["B. Peng"], "sequence",
              ("RWKV", "RNN", "linear attention", "time mixing", "channel mixing"),
              ("RWKV reinvents RNNs to combine Transformer-grade quality with RNN efficiency"),
              ("RWKV is a graph convolution method"), "held_out"),
    RealPaper("2006.16236", "Transformers are RNNs: Fast Autoregressive Transformers with Linear Attention", "LinearAttention", 2020,
              ["Angelos Katharopoulos"], "sequence",
              ("linear attention", "autoregressive", "kernel", "causal", "recurrent formulation"),
              ("Linear Attention reformulates attention with a kernel to be autoregressive and linear"),
              ("Linear Attention is a retrieval method"), "held_out"),
    RealPaper("2401.18059", "RAPTOR: Recursive Abstractive Processing for Tree-Organized Retrieval", "RAPTOR", 2024,
              ["Parth Sarthi"], "rag",
              ("RAPTOR", "recursive", "abstractive", "tree-organized", "hierarchical"),
              ("RAPTOR organizes retrieval into a recursive abstractive tree for document structure"),
              ("RAPTOR is a sequence model"), "held_out"),
    RealPaper("1706.02216", "Inductive Representation Learning on Large Graphs", "GraphSAGE", 2017,
              ["William Hamilton", "Jure Leskovec"], "graph",
              ("GraphSAGE", "inductive", "neighbor sampling", "aggregation", "large graphs"),
              ("GraphSAGE learns inductive node embeddings by sampling and aggregating neighbors"),
              ("GraphSAGE is an attention mechanism"), "held_out"),
    RealPaper("1710.10903", "Graph Attention Networks", "GAT", 2018,
              ["Petar Velickovic"], "graph",
              ("graph attention", "GAT", "masked", "self-attention", "neighborhood"),
              ("GAT applies masked self-attention over node neighborhoods in a graph"),
              ("GAT is a state space model"), "held_out"),
)


# --- Retrieval questions (split-aligned) -----------------------------------
# dev + calibration cases tune fixtures; held_out cases are the official measure.

REAL_RAG_CASES: tuple[RealRagCase, ...] = (
    # ===== dev (14): tune fixtures / inspect failures =====
    # factual (6)
    RealRagCase("d01", "Mamba 用什么机制实现线性时间的序列建模？",
                ("2312.00752",), ("selective", "state space", "scan"), "factual"),
    RealRagCase("d02", "Transformer 的注意力机制是怎么计算的？",
                ("1706.03762",), ("self-attention", "scaled", "dot-product"), "factual"),
    RealRagCase("d03", "S4 用什么方法建模长序列？",
                ("2111.00396",), ("structured", "state space", "HiPPO"), "factual"),
    RealRagCase("d04", "Atlas 如何结合检索与少样本学习？",
                ("2208.03299",), ("retrieval", "few-shot", "dense retriever"), "factual"),
    RealRagCase("d05", "GCN 如何做半监督的图节点分类？",
                ("1609.02907",), ("graph convolutional", "semi-supervised", "spectral"), "factual"),
    RealRagCase("d06", "Contriever 的对比学习目标具体是什么？",
                ("2112.09118",), ("contrastive", "in-batch", "unsupervised"), "factual"),
    # compare (3) — d07 is the known bi-encoder weakness; sub_queries enable multi-query variant
    RealRagCase("d07", "Mamba 和 Transformer 在序列建模方法上的核心区别是什么？",
                ("2312.00752", "1706.03762"), ("selective", "self-attention", "state space"), "compare",
                sub_queries=("Mamba selective state space model", "Transformer self-attention mechanism")),
    RealRagCase("d08", "GCN 和 GraphSAGE 在图卷积方法上的主要差异是什么？",
                ("1609.02907", "1706.02216"), ("graph convolutional", "spectral", "neighbor sampling"), "compare",
                sub_queries=("GCN spectral graph convolution", "GraphSAGE neighbor sampling aggregation")),
    RealRagCase("d09", "Atlas 和 Contriever 在检索增强方法上的区别？",
                ("2208.03299", "2112.09118"), ("few-shot", "contrastive", "dense retriever"), "compare",
                sub_queries=("Atlas few-shot retrieval-augmented language model", "Contriever contrastive dense retrieval")),
    # cross_lang (2): Chinese question, English papers
    RealRagCase("d10", "选择性状态空间模型如何实现？(中文问 Mamba)",
                ("2312.00752",), ("selective", "state space"), "cross_lang"),
    RealRagCase("d11", "自注意力机制的计算过程是什么？(中文问 Transformer)",
                ("1706.03762",), ("self-attention", "scaled"), "cross_lang"),
    # author_year (1)
    RealRagCase("d12", "Ashish Vaswani 2017 年关于注意力的论文方法是什么？",
                ("1706.03762",), ("self-attention", "transformer"), "author_year"),
    # abbreviation (1)
    RealRagCase("d13", "SSM 在序列建模中指什么方法？",
                ("2312.00752", "2111.00396"), ("state space", "selective", "structured"), "abbreviation"),
    # scope_leakage (1): graph query against sequence project — proves no cross-project word leak
    RealRagCase("d14", "GraphSAGE 的邻居采样聚合具体怎么做？(查 sequence 项目,不应泄漏 graph 论文)",
                (), ("GraphSAGE",), "scope_leakage", forbidden_terms=("GraphSAGE", "neighbor sampling")),

    # ===== calibration (6): set thresholds =====
    RealRagCase("c01", "RetNet 用什么机制替代 Transformer 的注意力？",
                ("2307.08621",), ("retention", "recurrent", "decay"), "factual"),
    RealRagCase("c02", "RWKV 的 time mixing 和 channel mixing 分别是什么？",
                ("2305.13048",), ("RWKV", "time mixing", "channel mixing"), "factual"),
    RealRagCase("c03", "RetNet 和 RWKV 都试图替代 Transformer,它们的方法差异是什么？",
                ("2307.08621", "2305.13048"), ("retention", "RWKV", "recurrent"), "compare",
                sub_queries=("RetNet retention mechanism", "RWKV time mixing channel mixing")),
    RealRagCase("c04", "RAPTOR 的递归抽象树结构如何组织文档？(中文也理解 RAPTOR)",
                ("2401.18059",), ("RAPTOR", "recursive", "abstractive", "tree"), "cross_lang"),
    RealRagCase("c05", "Albert Gu 和 Tri Dao 2023 年的线性时间序列模型是什么？",
                ("2312.00752",), ("selective", "state space", "Mamba"), "author_year"),
    RealRagCase("c06", "GAT 中的图注意力是怎么计算的？",
                ("1710.10903",), ("graph attention", "masked", "neighborhood"), "abbreviation"),

    # ===== held_out (10): frozen, Wave 1+ official conclusions only =====
    # factual (5)
    RealRagCase("h01", "RWKV 如何在 Transformer 时代重塑 RNN？",
                ("2305.13048",), ("RWKV", "linear attention", "time mixing"), "factual"),
    RealRagCase("h02", "Linear Attention 怎样把注意力变成线性且自回归的？",
                ("2006.16236",), ("linear attention", "kernel", "autoregressive"), "factual"),
    RealRagCase("h03", "RAPTOR 如何用递归抽象树组织检索？",
                ("2401.18059",), ("RAPTOR", "recursive", "abstractive", "tree"), "factual"),
    RealRagCase("h04", "GraphSAGE 如何通过邻居采样学习归纳式表示？",
                ("1706.02216",), ("GraphSAGE", "inductive", "neighbor sampling"), "factual"),
    RealRagCase("h05", "GAT 的 masked self-attention 应用在图上的具体方式？",
                ("1710.10903",), ("graph attention", "masked", "self-attention"), "factual"),
    # compare (2)
    RealRagCase("h06", "GraphSAGE 和 GAT 在图表示学习上的方法差异是什么？",
                ("1706.02216", "1710.10903"), ("GraphSAGE", "neighbor sampling", "graph attention"), "compare",
                sub_queries=("GraphSAGE neighbor sampling inductive", "GAT graph attention masked self-attention")),
    RealRagCase("h07", "Linear Attention 和 RWKV 都把 Transformer 线性化,方法路径有何不同？",
                ("2006.16236", "2305.13048"), ("linear attention", "kernel", "RWKV", "time mixing"), "compare",
                sub_queries=("Linear Attention kernel autoregressive", "RWKV time mixing channel mixing")),
    # cross_lang (1)
    RealRagCase("h08", "图注意力网络如何用 masked self-attention 处理邻居？(中文问 GAT)",
                ("1710.10903",), ("graph attention", "masked", "self-attention"), "cross_lang"),
    # author_year (1)
    RealRagCase("h09", "Thomas Kipf 和 Max Welling 关于图卷积网络的半监督方法是什么？",
                ("1609.02907",), ("graph convolutional", "semi-supervised", "spectral"), "author_year"),
    # scope_leakage (1): rag query against graph project
    RealRagCase("h10", "Atlas 的 few-shot 检索增强方法怎么做？(查 graph 项目,不应泄漏 rag 论文)",
                (), ("Atlas", "few-shot"), "scope_leakage", forbidden_terms=("Atlas", "few-shot")),
)


def papers_for_split(split: str) -> list[RealPaper]:
    return [p for p in REAL_PAPERS if p.split == split]


def cases_for_split(split: str) -> list[RealRagCase]:
    if split == "dev":
        return [c for c in REAL_RAG_CASES if c.id.startswith("d")]
    if split == "calibration":
        return [c for c in REAL_RAG_CASES if c.id.startswith("c")]
    if split == "held_out":
        return [c for c in REAL_RAG_CASES if c.id.startswith("h")]
    raise ValueError(f"unknown split: {split!r}")


def paper_by_arxiv(arxiv_id: str) -> RealPaper | None:
    return next((p for p in REAL_PAPERS if p.arxiv_id == arxiv_id), None)


def pdf_path(arxiv_id: str, fixtures_dir: str = "media/fixtures/pdf") -> str:
    """Locate the downloaded PDF for an arxiv_id."""
    import os

    paper = paper_by_arxiv(arxiv_id)
    short = paper.short_name if paper else ""
    candidate = os.path.join(fixtures_dir, f"{arxiv_id}_{short}.pdf")
    if os.path.exists(candidate):
        return candidate
    # Fallback: match by arxiv_id prefix
    for fn in os.listdir(fixtures_dir):
        if fn.startswith(arxiv_id):
            return os.path.join(fixtures_dir, fn)
    raise FileNotFoundError(f"no PDF for {arxiv_id} in {fixtures_dir}")
