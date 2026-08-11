"""Deterministic 30+ case evaluation for PaperLens hybrid RAG."""
from __future__ import annotations

import asyncio
import hashlib
import json
import math
import re
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Iterator
from unittest import mock

import numpy as np
from asgiref.sync import sync_to_async
from django.conf import settings

from api.models import ProjectPaper, ResearchProject
from papers.models import Paper
from rag.citations import make_citation_key_for_paper
from rag.models import Text
from rag.retrieval import hybrid_retrieve_texts


@dataclass(frozen=True)
class RagEvalPaper:
    title: str
    abstract: str
    year: int
    text: str


@dataclass(frozen=True)
class RagEvalCase:
    id: str
    question: str
    gold_titles: tuple[str, ...]
    expected_terms: tuple[str, ...]
    category: str


RAG_EVAL_PAPERS: tuple[RagEvalPaper, ...] = (
    RagEvalPaper(
        "Attention Is All You Need",
        "Transformer self-attention improves parallel sequence modeling but has quadratic long-context cost.",
        2017,
        "Transformer architecture uses multi-head self-attention and feed-forward layers. Full attention has quadratic memory and compute cost for long sequences, although it models global dependencies and trains in parallel.",
    ),
    RagEvalPaper(
        "Mamba: Linear-Time Sequence Modeling with Selective State Spaces",
        "Mamba uses selective state space models and hardware-aware scan for long sequence modeling.",
        2023,
        "Mamba introduces input-dependent selective state updates and a hardware-aware parallel scan. It provides linear-time sequence processing, but compressed recurrent state can make exact rare-token lookup harder than explicit attention.",
    ),
    RagEvalPaper(
        "Evaluating Retrieval-Augmented Generation for Faithful Answers",
        "RAG evaluation should measure retrieval quality, grounding, citation coverage, and faithfulness.",
        2025,
        "A RAG evaluation should report Recall@5, MRR, context precision, citation coverage, faithfulness, unsupported claim rate, and retrieval latency. Evidence should be tied to cited passages.",
    ),
    RagEvalPaper(
        "Citation Graphs for Literature Discovery",
        "Bibliographic coupling and citation maps reveal related work, root papers, and frontier papers.",
        2024,
        "Citation graph analysis uses referenced works and bibliographic coupling to connect papers. Root papers are older highly cited anchors, while frontier papers are recent nodes that point to emerging directions.",
    ),
    RagEvalPaper(
        "DBLP Metadata for Computer Science Literature Agents",
        "DBLP provides authoritative computer science venue, author, and key metadata.",
        2026,
        "DBLP is useful as a default source for computer science literature because it gives reliable venue, author, year, and bibliographic key metadata. OpenAlex can complement it with citations and abstracts.",
    ),
    RagEvalPaper(
        "Hybrid RAG with Dense Vectors and Lexical Search",
        "Hybrid retrieval combines dense embedding search, lexical search, and reciprocal rank fusion.",
        2026,
        "Hybrid RAG retrieves candidates from dense vector search and lexical full-text search. Reciprocal Rank Fusion combines rankings so exact terms and semantic matches can both surface.",
    ),
    RagEvalPaper(
        "PostgreSQL pgvector Indexing for Semantic Search",
        "pgvector stores embeddings inside Postgres and supports HNSW vector indexes.",
        2025,
        "PostgreSQL with pgvector can store embeddings in a vector column. HNSW indexes support approximate nearest-neighbor search, and metadata filters keep retrieval project-scoped.",
    ),
    RagEvalPaper(
        "Postgres Full-Text Search for RAG Retrieval",
        "PostgreSQL FTS provides lexical matching over passages with ranking.",
        2024,
        "Postgres full-text search uses tsvector and tsquery to retrieve exact lexical matches. It is a pragmatic BM25-like first lexical layer for local RAG systems.",
    ),
    RagEvalPaper(
        "PDF Ingestion Pipelines for Evidence-Centric RAG",
        "PDF ingestion should preserve page, section, offset, hash, and parse status.",
        2026,
        "PDF ingestion quality depends on parsing pages, chunking text, preserving section names, page ranges, character offsets, content hashes, and embedding status for retryable background jobs.",
    ),
    RagEvalPaper(
        "LangGraph Workflows for Durable Research Agents",
        "LangGraph is appropriate for explicit multi-step research workflows.",
        2026,
        "LangGraph fits long-running workflows such as expand search, add candidates, enqueue ingestion, query hybrid RAG, run a critic, draft a report, and persist a versioned artifact.",
    ),
    # ---- 15 篇新增论文,覆盖微调/对齐/多模态/检索/推理/效率/安全/代码/Agent/分布式 ----
    RagEvalPaper(
        "LoRA: Low-Rank Adaptation of Large Language Models",
        "LoRA freezes pretrained weights and injects trainable low-rank update matrices, cutting adapter fine-tuning memory with no added inference latency.",
        2021,
        "LoRA fine-tunes large language models by freezing pretrained weights and learning low-rank update matrices injected into attention projections. "
        "This parameter-efficient adapter approach cuts optimizer memory and GPU cost while matching full fine-tuning quality, with no added inference latency because the low-rank updates merge back into the weights.",
    ),
    RagEvalPaper(
        "Direct Preference Optimization: Your Language Model Is Secretly a Reward Model",
        "DPO aligns language models to human preferences without a separate reward model or reinforcement learning, by optimizing the policy directly on preference pairs.",
        2023,
        "Direct Preference Optimization (DPO) aligns a language model to human preferences using paired comparisons. "
        "Unlike RLHF, DPO needs no separate reward model, critic, or reinforcement learning loop: it reuses the policy itself as a reward model and optimizes a closed-form classification objective on preference pairs, simplifying alignment.",
    ),
    RagEvalPaper(
        "Learning Transferable Visual Models From Natural Language Supervision",
        "CLIP learns joint image-text embeddings from contrastive caption pairs, enabling zero-shot image classification from natural language prompts.",
        2021,
        "CLIP is a vision-language model that trains image and text encoders jointly on contrastive image-caption pairs. "
        "It learns a shared multimodal embedding space enabling zero-shot classification from natural-language class prompts, strong transfer to downstream tasks, and robust visual reasoning without task-specific labels.",
    ),
    RagEvalPaper(
        "Billion-Scale Similarity Search with GPUs",
        "FAISS implements approximate nearest-neighbor search with GPU-optimized IVF and product-quantization indexes for billion-scale dense vector retrieval.",
        2019,
        "FAISS is a library for dense vector similarity search at billion scale. "
        "It builds approximate nearest-neighbor indexes using IVF clustering and product quantization (PQ) compression, with GPU kernels that accelerate k-NN search. "
        "FAISS trades a small recall loss for large memory and latency savings over exact search, powering large-scale retrieval.",
    ),
    RagEvalPaper(
        "Tree of Thoughts: Deliberate Problem Solving with Large Language Models",
        "Tree of Thoughts lets language models explore multiple reasoning paths and backtrack, extending chain-of-thought prompting for search-like planning.",
        2023,
        "Tree of Thoughts (ToT) frames reasoning as search over a tree of intermediate thought states. "
        "At each step the model generates multiple candidate thoughts, evaluates them with self-assessment, and uses backtracking to recover from dead ends. "
        "ToT extends chain-of-thought prompting on planning and combinatorial tasks where a single reasoning path fails.",
    ),
    RagEvalPaper(
        "Graph Attention Networks",
        "GAT learns node representations by masked self-attention over graph neighborhoods, assigning learned attention weights to neighbor edges.",
        2018,
        "Graph Attention Networks (GAT) learn node representations on graphs using masked self-attention. "
        "Each node attends to its neighbors with learned attention coefficients, letting the model weight important edges without costly spectral eigendecomposition. "
        "GAT generalizes across graph sizes and produces interpretable attention weights for node and graph classification.",
    ),
    RagEvalPaper(
        "Evaluating Large Language Models Trained on Code",
        "Codex evaluates code-generation LLMs on HumanEval, a benchmark of Python programming problems with unit tests measuring functional correctness.",
        2021,
        "This work introduces Codex, a language model trained on public code, and HumanEval, a benchmark of hand-written Python programming problems. "
        "Functional correctness is measured by pass@k: the fraction of problems solved by at least one of k samples that pass unit tests. "
        "Sampling many completions and filtering with execution improves code generation over greedy decoding.",
    ),
    RagEvalPaper(
        "ReAct: Synergizing Reasoning and Acting in Language Models",
        "ReAct interleaves chain-of-thought reasoning with external tool calls and observations, reducing hallucination in knowledge-intensive tasks.",
        2023,
        "ReAct prompts language models to interleave reasoning traces (chain-of-thought steps) with actions such as search, lookup, or tool calls, then read the observations back into context. "
        "Coupling reasoning with grounded external actions improves task success and reduces hallucination versus pure reasoning on knowledge-intensive and decision-making tasks.",
    ),
    RagEvalPaper(
        "YaRN: Efficient Context Window Extension of Large Language Models",
        "YaRN extends rotary position embeddings (RoPE) to longer contexts via NTK-aware interpolation, enabling 128k-token extrapolation without retraining.",
        2023,
        "YaRN efficiently extends the context window of transformers trained with rotary position embeddings (RoPE). "
        "By interpolating RoPE frequencies with an NTK-aware scheme, YaRN lets models extrapolate to 128k tokens with light fine-tuning and no architecture change, lowering the long-context attention cost relative to dense retraining.",
    ),
    RagEvalPaper(
        "DistilBERT, a distilled version of BERT: smaller, faster, cheaper and lighter",
        "DistilBERT compresses BERT via knowledge distillation, halving parameters and latency while retaining about 97% of the teacher capability.",
        2019,
        "DistilBERT compresses a large BERT teacher into a smaller student via knowledge distillation: the student matches the teacher softened output logits while dropping the token-type embeddings and distillation heads. "
        "The result halves parameters and inference latency and reduces the carbon footprint while retaining about 97% of the teacher capability, trading a small accuracy loss for major efficiency gains.",
    ),
    RagEvalPaper(
        "When to Retrieve and When to Fine-Tune: Comparing RAG and Fine-Tuning",
        "This study compares retrieval-augmented generation against fine-tuning for knowledge injection, finding RAG better for fresh facts and fine-tuning for behavior change.",
        2024,
        "We compare retrieval-augmented generation (RAG) and parameter-efficient fine-tuning as ways to inject new knowledge into a language model. "
        "RAG shines when facts change frequently or need citation and provenance, since retrieval swaps documents without retraining. "
        "Fine-tuning better shifts model behavior, style, or reasoning patterns. Hybrid pipelines combine both, tuning behavior while retrieving fresh evidence.",
    ),
    RagEvalPaper(
        "On Calibration of Modern Neural Networks",
        "Deep networks are overconfident; temperature scaling measured by expected calibration error (ECE) improves predicted-probability reliability.",
        2017,
        "Modern deep networks are poorly calibrated: their predicted confidence is far higher than their accuracy. "
        "We measure this gap with Expected Calibration Error (ECE), a binned reliability statistic, and show that temperature scaling a single post-hoc parameter sharply reduces ECE. "
        "Calibration matters where predicted probabilities, not just top-1 labels, drive safety-critical decisions and error analysis with the confusion matrix.",
    ),
    RagEvalPaper(
        "Megatron-LM: Training Multi-Billion Parameter Language Models Using Model Parallelism",
        "Megatron-LM scales transformer training with tensor parallelism, splitting attention and feed-forward matrices across GPUs.",
        2019,
        "Megatron-LM scales transformer training to multi-billion parameters with intra-layer model parallelism: each attention and feed-forward weight matrix is partitioned across GPUs with tensor parallelism, exchanging partial results with a few all-reduce collectives. "
        "Combined with data parallelism and pipeline parallelism, this reduces per-GPU memory and lets very large models train without changing the optimizer.",
    ),
    RagEvalPaper(
        "Self-Instruct: Aligning Language Models with Self-Generated Instructions",
        "Self-Instruct bootstraps instruction data by letting a model generate, filter, and re-use its own task instructions and responses.",
        2022,
        "Self-Instruct bootstraps synthetic instruction-tuning data from a seed of human-written tasks. "
        "The language model generates new instructions and responses, then filters out low-quality or duplicated items with lexical and embedding similarity before adding survivors back as training data. "
        "This data-augmentation loop produces diverse supervised examples that improve instruction-following without much manual labeling.",
    ),
    RagEvalPaper(
        "Communication-Efficient Learning of Deep Networks from Decentralized Data",
        "Federated averaging (FedAvg) trains models across decentralized clients by averaging local updates, keeping raw data on-device for privacy.",
        2017,
        "Federated learning trains a shared model across decentralized devices holding private data. "
        "Federated Averaging (FedAvg) runs local stochastic gradient descent on each client over several epochs, then averages the model updates on a central server. "
        "Keeping raw samples on-device protects privacy and lowers communication, at the cost of statistical heterogeneity across non-iid client distributions.",
    ),
)


RAG_EVAL_CASES: tuple[RagEvalCase, ...] = (
    RagEvalCase("transformer_quadratic_cost", "Why is full Transformer attention expensive for long sequences?", ("Attention Is All You Need",), ("quadratic", "memory", "compute"), "long_sequence"),
    RagEvalCase("transformer_parallel_training", "Which paper says self-attention enables parallel training across sequence positions?", ("Attention Is All You Need",), ("parallel", "attention"), "method"),
    RagEvalCase("mamba_linear_time", "What makes Mamba efficient for long sequence processing?", ("Mamba: Linear-Time Sequence Modeling with Selective State Spaces",), ("linear", "selective", "scan"), "long_sequence"),
    RagEvalCase("mamba_exact_lookup_limit", "What limitation can Mamba have for exact rare-token lookup?", ("Mamba: Linear-Time Sequence Modeling with Selective State Spaces",), ("exact", "rare-token", "state"), "method_compare"),
    RagEvalCase("rag_eval_metrics", "Which metrics should evaluate RAG faithfulness and retrieval quality?", ("Evaluating Retrieval-Augmented Generation for Faithful Answers",), ("recall", "mrr", "faithfulness"), "rag_evaluation"),
    RagEvalCase("rag_citation_coverage", "Which RAG metric checks whether answer claims are tied to citations?", ("Evaluating Retrieval-Augmented Generation for Faithful Answers",), ("citation", "coverage"), "rag_evaluation"),
    RagEvalCase("citation_root_frontier", "How does a citation map distinguish root papers from frontier papers?", ("Citation Graphs for Literature Discovery",), ("root", "frontier"), "citation_graph"),
    RagEvalCase("bibliographic_coupling", "What signal connects related papers in a citation graph?", ("Citation Graphs for Literature Discovery",), ("referenced", "coupling"), "citation_graph"),
    RagEvalCase("dblp_default_source", "Why should DBLP be a default source for a CS literature agent?", ("DBLP Metadata for Computer Science Literature Agents",), ("venue", "author", "metadata"), "metadata"),
    RagEvalCase("openalex_complement", "What should OpenAlex complement DBLP with?", ("DBLP Metadata for Computer Science Literature Agents",), ("citations", "abstracts"), "metadata"),
    RagEvalCase("hybrid_rrf", "How does Reciprocal Rank Fusion help hybrid RAG?", ("Hybrid RAG with Dense Vectors and Lexical Search",), ("dense", "lexical", "fusion"), "hybrid"),
    RagEvalCase("dense_lexical_balance", "Why combine dense vector search with lexical full-text search?", ("Hybrid RAG with Dense Vectors and Lexical Search",), ("semantic", "exact", "terms"), "hybrid"),
    RagEvalCase("pgvector_column", "Where are embeddings stored in a pgvector architecture?", ("PostgreSQL pgvector Indexing for Semantic Search",), ("vector", "column"), "infra"),
    RagEvalCase("hnsw_index", "Which pgvector index supports approximate nearest-neighbor search?", ("PostgreSQL pgvector Indexing for Semantic Search",), ("hnsw", "nearest"), "infra"),
    RagEvalCase("postgres_fts", "What does Postgres full-text search use for lexical retrieval?", ("Postgres Full-Text Search for RAG Retrieval",), ("tsvector", "tsquery"), "lexical"),
    RagEvalCase("bm25_like_layer", "Which component acts as the first lexical layer before Elasticsearch is introduced?", ("Postgres Full-Text Search for RAG Retrieval",), ("full-text", "lexical"), "lexical"),
    RagEvalCase("pdf_page_offsets", "Which PDF ingestion metadata helps audit evidence location?", ("PDF Ingestion Pipelines for Evidence-Centric RAG",), ("page", "offset", "section"), "pdf"),
    RagEvalCase("pdf_retry_status", "What status should make PDF ingestion retryable?", ("PDF Ingestion Pipelines for Evidence-Centric RAG",), ("status", "retryable", "background"), "pdf"),
    RagEvalCase("langgraph_use_case", "When is LangGraph justified in this project?", ("LangGraph Workflows for Durable Research Agents",), ("long-running", "workflow"), "agent_workflow"),
    RagEvalCase("workflow_nodes", "Which workflow steps belong in the long research expansion chain?", ("LangGraph Workflows for Durable Research Agents",), ("expand", "critic", "report"), "agent_workflow"),
    RagEvalCase("zh_mamba_advantage", "Mamba 在长序列处理中的优势是什么？", ("Mamba: Linear-Time Sequence Modeling with Selective State Spaces",), ("linear", "selective"), "zh"),
    RagEvalCase("zh_transformer_limit", "Transformer 在长上下文中的主要成本问题是什么？", ("Attention Is All You Need",), ("quadratic", "memory"), "zh"),
    RagEvalCase("zh_rag_eval", "RAG 评测应该关注哪些指标？", ("Evaluating Retrieval-Augmented Generation for Faithful Answers",), ("recall", "faithfulness"), "zh"),
    RagEvalCase("zh_pdf_metadata", "PDF 入库为什么要保留页码和 offset？", ("PDF Ingestion Pipelines for Evidence-Centric RAG",), ("page", "offset"), "zh"),
    RagEvalCase("venue_author_exact", "Which source is authoritative for CS venue and author metadata?", ("DBLP Metadata for Computer Science Literature Agents",), ("dblp", "venue", "author"), "exact"),
    RagEvalCase("project_scope_filter", "Which vector database setup keeps retrieval project-scoped with metadata filters?", ("PostgreSQL pgvector Indexing for Semantic Search",), ("metadata", "project-scoped"), "infra"),
    RagEvalCase("citation_discovery", "What graph method supports literature discovery through related work edges?", ("Citation Graphs for Literature Discovery",), ("citation", "related"), "citation_graph"),
    RagEvalCase("unsupported_quantum", "What evidence does this project have about quantum chemistry solvers?", tuple(), ("quantum", "chemistry"), "no_evidence"),
    RagEvalCase("unsupported_medical", "What clinical trial evidence is in this project?", tuple(), ("clinical", "trial"), "no_evidence"),
    RagEvalCase("unsupported_finance", "Which stock trading strategy did the papers validate?", tuple(), ("stock", "trading"), "no_evidence"),
    RagEvalCase("hybrid_exact_abbrev", "What does FTS add when a query contains exact abbreviations like RAG or DBLP?", ("Hybrid RAG with Dense Vectors and Lexical Search", "DBLP Metadata for Computer Science Literature Agents"), ("exact", "lexical"), "abbrev"),
    RagEvalCase("report_version_workflow", "Which Agent workflow step persists a versioned report artifact?", ("LangGraph Workflows for Durable Research Agents",), ("persist", "artifact"), "agent_workflow"),
    # ---- 68 个新增 case,覆盖 15 类 ----
    RagEvalCase("lora_method", "How does LoRA adapt large language models without updating all pretrained weights?",
                ("LoRA: Low-Rank Adaptation of Large Language Models",), ("low-rank", "adapter", "parameter-efficient"), "method"),
    RagEvalCase("dpo_method", "What objective does Direct Preference Optimization use instead of a separate reward model?",
                ("Direct Preference Optimization: Your Language Model Is Secretly a Reward Model",), ("preference", "classification", "policy"), "method"),
    RagEvalCase("clip_method", "How does CLIP achieve zero-shot image classification?",
                ("Learning Transferable Visual Models From Natural Language Supervision",), ("contrastive", "zero-shot", "prompt"), "method"),
    RagEvalCase("gat_method", "How do Graph Attention Networks weight neighbor contributions?",
                ("Graph Attention Networks",), ("attention", "node", "neighborhood"), "method"),
    RagEvalCase("react_method", "How does ReAct combine reasoning with external tools?",
                ("ReAct: Synergizing Reasoning and Acting in Language Models",), ("reasoning", "tool", "observation"), "method"),
    RagEvalCase("yarn_method", "What technique does YaRN use to extend the rotary position embedding context window?",
                ("YaRN: Efficient Context Window Extension of Large Language Models",), ("rotary", "interpolation", "context window"), "method"),
    RagEvalCase("dpo_vs_rlhf", "How does DPO differ from RLHF for alignment?",
                ("Direct Preference Optimization: Your Language Model Is Secretly a Reward Model",), ("reward model", "preference", "reinforcement"), "method_compare"),
    RagEvalCase("tot_vs_cot", "How does Tree of Thoughts extend ordinary chain-of-thought prompting?",
                ("Tree of Thoughts: Deliberate Problem Solving with Large Language Models",), ("backtracking", "thought", "search"), "method_compare"),
    RagEvalCase("rag_vs_finetuning_when", "When should I use RAG instead of fine-tuning to update model knowledge?",
                ("When to Retrieve and When to Fine-Tune: Comparing RAG and Fine-Tuning",), ("retrieval", "fine-tuning", "knowledge"), "method_compare"),
    RagEvalCase("transformer_vs_mamba_cost", "How does Mamba sequence cost compare to the Transformer quadratic attention?",
                ("Mamba: Linear-Time Sequence Modeling with Selective State Spaces", "Attention Is All You Need"), ("linear", "quadratic", "selective"), "method_compare"),
    RagEvalCase("transformer_long_attention", "What is the main bottleneck that makes full self-attention expensive on long documents?",
                ("Attention Is All You Need",), ("quadratic", "memory", "compute"), "long_sequence"),
    RagEvalCase("mamba_linear_long", "Which architecture processes long sequences in linear time using selective state spaces?",
                ("Mamba: Linear-Time Sequence Modeling with Selective State Spaces",), ("linear", "selective", "scan"), "long_sequence"),
    RagEvalCase("yarn_long_context", "Which method extends rotary position embeddings to 128k-token contexts with light tuning?",
                ("YaRN: Efficient Context Window Extension of Large Language Models",), ("rotary", "interpolation", "context window"), "long_sequence"),
    RagEvalCase("long_seq_arch_compare", "Which two architectures are most often contrasted when discussing linear-time versus quadratic long-sequence modeling?",
                ("Mamba: Linear-Time Sequence Modeling with Selective State Spaces", "Attention Is All You Need"), ("linear", "quadratic"), "long_sequence"),
    RagEvalCase("retrieval_recall_at_5", "Which metric measures whether a gold passage lands in the top 5 retrieved results?",
                ("Evaluating Retrieval-Augmented Generation for Faithful Answers",), ("recall", "precision"), "rag_evaluation"),
    RagEvalCase("faithfulness_unsupported", "Which RAG metric penalizes unsupported claims that lack a citation?",
                ("Evaluating Retrieval-Augmented Generation for Faithful Answers",), ("faithfulness", "unsupported", "citation"), "rag_evaluation"),
    RagEvalCase("rag_latency_metric", "Beyond relevance, which operational metric should a RAG evaluation report?",
                ("Evaluating Retrieval-Augmented Generation for Faithful Answers",), ("latency", "faithfulness"), "rag_evaluation"),
    RagEvalCase("rag_eval_vs_finetune", "How does evaluation change when comparing a retrieval-augmented system to a fine-tuned model?",
                ("Evaluating Retrieval-Augmented Generation for Faithful Answers", "When to Retrieve and When to Fine-Tune: Comparing RAG and Fine-Tuning"),
                ("recall", "fine-tuning"), "rag_evaluation"),
    RagEvalCase("gat_for_citation_graph", "Could graph attention help rank related papers in a citation network?",
                ("Citation Graphs for Literature Discovery", "Graph Attention Networks"), ("attention", "node", "citation"), "citation_graph"),
    RagEvalCase("citation_root_anchor", "What role do highly cited older papers play in a citation graph?",
                ("Citation Graphs for Literature Discovery",), ("root", "anchor", "cited"), "citation_graph"),
    RagEvalCase("bibliographic_coupling_references", "Which graph signal groups papers that cite the same set of references?",
                ("Citation Graphs for Literature Discovery",), ("bibliographic", "coupling", "referenced"), "citation_graph"),
    RagEvalCase("lora_low_rank_updates", "Why does LoRA add only low-rank update matrices during fine-tuning?",
                ("LoRA: Low-Rank Adaptation of Large Language Models",), ("low-rank", "adapter", "parameter-efficient"), "finetuning"),
    RagEvalCase("lora_no_inference_latency", "Why does LoRA not add inference latency?",
                ("LoRA: Low-Rank Adaptation of Large Language Models",), ("merge", "latency"), "finetuning"),
    RagEvalCase("lora_memory_savings", "How does LoRA reduce optimizer memory during fine-tuning?",
                ("LoRA: Low-Rank Adaptation of Large Language Models",), ("memory", "parameter-efficient", "gpu"), "finetuning"),
    RagEvalCase("finetuning_when_preferable", "When is fine-tuning preferable to retrieval augmentation?",
                ("When to Retrieve and When to Fine-Tune: Comparing RAG and Fine-Tuning",), ("fine-tuning", "behavior", "retrieval"), "finetuning"),
    RagEvalCase("self_instruct_tuning_data", "How can a model generate its own instruction fine-tuning data?",
                ("Self-Instruct: Aligning Language Models with Self-Generated Instructions",), ("bootstrap", "synthetic", "instruction"), "finetuning"),
    RagEvalCase("dpo_preference_pairs", "What training signal does DPO need instead of a separate reward model?",
                ("Direct Preference Optimization: Your Language Model Is Secretly a Reward Model",), ("preference", "pair", "reward model"), "alignment"),
    RagEvalCase("dpo_no_reinforcement", "Which alignment method avoids reinforcement learning by reusing the policy as a reward model?",
                ("Direct Preference Optimization: Your Language Model Is Secretly a Reward Model",), ("reward model", "preference", "policy"), "alignment"),
    RagEvalCase("dpo_removes_from_rlhf", "Compared with RLHF, what does DPO remove from the alignment pipeline?",
                ("Direct Preference Optimization: Your Language Model Is Secretly a Reward Model",), ("reward model", "reinforcement", "critic"), "alignment"),
    RagEvalCase("dpo_closed_form", "What kind of objective does DPO optimize on preference pairs?",
                ("Direct Preference Optimization: Your Language Model Is Secretly a Reward Model",), ("classification", "preference", "policy"), "alignment"),
    RagEvalCase("self_instruct_align", "How does Self-Instruct improve a model instruction following?",
                ("Self-Instruct: Aligning Language Models with Self-Generated Instructions",), ("instruction", "bootstrap", "filter"), "alignment"),
    RagEvalCase("clip_zero_shot", "How does CLIP perform image classification without task-specific labels?",
                ("Learning Transferable Visual Models From Natural Language Supervision",), ("zero-shot", "contrastive", "prompt"), "multimodal"),
    RagEvalCase("clip_joint_space", "What kind of space does CLIP learn between images and text?",
                ("Learning Transferable Visual Models From Natural Language Supervision",), ("embedding", "multimodal", "joint"), "multimodal"),
    RagEvalCase("clip_caption_pairs", "What training data lets CLIP learn transferable visual features?",
                ("Learning Transferable Visual Models From Natural Language Supervision",), ("caption", "contrastive", "image"), "multimodal"),
    RagEvalCase("clip_transfer_downstream", "Why are vision-language models like CLIP considered transferable?",
                ("Learning Transferable Visual Models From Natural Language Supervision",), ("transfer", "zero-shot", "embedding"), "multimodal"),
    RagEvalCase("clip_contrastive_vlm", "Which paper introduces a vision-language model trained with contrastive image and text objectives?",
                ("Learning Transferable Visual Models From Natural Language Supervision",), ("contrastive", "vision-language", "embedding"), "multimodal"),
    RagEvalCase("faiss_ann_billion", "Which library builds approximate nearest-neighbor indexes for billion-scale vector search?",
                ("Billion-Scale Similarity Search with GPUs",), ("nearest-neighbor", "billion", "vector"), "retrieval"),
    RagEvalCase("faiss_pq_memory", "How does FAISS compress dense vectors to save memory at billion scale?",
                ("Billion-Scale Similarity Search with GPUs",), ("product quantization", "compression", "memory"), "retrieval"),
    RagEvalCase("faiss_gpu_kernels", "Why are GPU kernels important for FAISS similarity search?",
                ("Billion-Scale Similarity Search with GPUs",), ("gpu", "kernel", "nearest-neighbor"), "retrieval"),
    RagEvalCase("hnsw_vs_faiss", "How do HNSW indexes in pgvector compare to FAISS for vector search?",
                ("PostgreSQL pgvector Indexing for Semantic Search", "Billion-Scale Similarity Search with GPUs"), ("hnsw", "product quantization"), "retrieval"),
    RagEvalCase("hybrid_dense_lexical_new", "How does hybrid retrieval balance dense semantic and lexical exact matching?",
                ("Hybrid RAG with Dense Vectors and Lexical Search",), ("dense", "lexical", "semantic"), "retrieval"),
    RagEvalCase("tot_search_tree", "How does Tree of Thoughts turn reasoning into a search problem?",
                ("Tree of Thoughts: Deliberate Problem Solving with Large Language Models",), ("tree", "search", "backtracking"), "reasoning"),
    RagEvalCase("tot_backtrack", "What lets Tree of Thoughts recover from a wrong reasoning step?",
                ("Tree of Thoughts: Deliberate Problem Solving with Large Language Models",), ("backtracking", "evaluate", "thought"), "reasoning"),
    RagEvalCase("react_reason_act", "Why does interleaving reasoning and acting help language models?",
                ("ReAct: Synergizing Reasoning and Acting in Language Models",), ("reasoning", "action", "observation"), "reasoning"),
    RagEvalCase("cot_single_path_limit", "On which tasks does a single chain-of-thought path fail, motivating Tree of Thoughts?",
                ("Tree of Thoughts: Deliberate Problem Solving with Large Language Models",), ("chain-of-thought", "planning", "path"), "reasoning"),
    RagEvalCase("distilbert_student", "How does DistilBERT reduce model size using knowledge distillation?",
                ("DistilBERT, a distilled version of BERT: smaller, faster, cheaper and lighter",), ("distillation", "student", "teacher"), "efficiency"),
    RagEvalCase("distilbert_latency_cut", "What efficiency gains does DistilBERT achieve over BERT?",
                ("DistilBERT, a distilled version of BERT: smaller, faster, cheaper and lighter",), ("latency", "parameters", "carbon"), "efficiency"),
    RagEvalCase("megatron_tensor_parallel", "How does Megatron-LM partition transformer layers for model-parallel training?",
                ("Megatron-LM: Training Multi-Billion Parameter Language Models Using Model Parallelism",), ("tensor", "partition", "parallelism"), "efficiency"),
    RagEvalCase("faiss_recall_speed_tradeoff", "How does approximate nearest-neighbor search in FAISS trade recall for speed?",
                ("Billion-Scale Similarity Search with GPUs",), ("recall", "latency", "product quantization"), "efficiency"),
    RagEvalCase("calibration_overconfident", "Why are overconfident neural networks a safety concern?",
                ("On Calibration of Modern Neural Networks",), ("calibration", "confidence", "safety"), "safety"),
    RagEvalCase("calibration_ece", "How is Expected Calibration Error used to diagnose model reliability?",
                ("On Calibration of Modern Neural Networks",), ("calibration", "binned", "confidence"), "safety"),
    RagEvalCase("react_grounding_safety", "How does grounding answers in external observations improve model safety and reduce hallucination?",
                ("ReAct: Synergizing Reasoning and Acting in Language Models",), ("observation", "hallucination", "grounded"), "safety"),
    RagEvalCase("humaneval_pass_at_k", "What metric does HumanEval use to measure code generation correctness?",
                ("Evaluating Large Language Models Trained on Code",), ("humaneval", "pass@k", "unit test"), "codegen"),
    RagEvalCase("codex_execution_filter", "How does sampling and execution filtering improve code generation?",
                ("Evaluating Large Language Models Trained on Code",), ("sampling", "execution", "unit test"), "codegen"),
    RagEvalCase("humaneval_python_benchmark", "Which benchmark evaluates LLMs on Python programming problems?",
                ("Evaluating Large Language Models Trained on Code",), ("humaneval", "python", "programming"), "codegen"),
    RagEvalCase("codegen_not_reasoning_distractor", "Which paper focuses on code generation rather than general reasoning?",
                ("Evaluating Large Language Models Trained on Code",), ("code", "humaneval", "programming"), "codegen"),
    RagEvalCase("react_tools_observations", "Which prompting framework lets an agent call tools and read observations back into context?",
                ("ReAct: Synergizing Reasoning and Acting in Language Models",), ("tool", "observation", "reasoning"), "agent"),
    RagEvalCase("react_vs_cot_agent", "How does ReAct reduce hallucination compared to pure chain-of-thought reasoning?",
                ("ReAct: Synergizing Reasoning and Acting in Language Models",), ("reasoning", "observation", "hallucination"), "agent"),
    RagEvalCase("agent_workflow_steps_new", "Which durable agent framework supports expand-search, critic, and report nodes?",
                ("LangGraph Workflows for Durable Research Agents",), ("workflow", "critic", "report"), "agent"),
    RagEvalCase("react_task_success", "On what kinds of tasks does ReAct improve over pure reasoning?",
                ("ReAct: Synergizing Reasoning and Acting in Language Models",), ("knowledge-intensive", "decision-making", "observation"), "agent"),
    RagEvalCase("zh_lora_advantage", "LoRA 微调相比全量微调的主要优势是什么？",
                ("LoRA: Low-Rank Adaptation of Large Language Models",), ("low-rank", "adapter", "parameter-efficient"), "chinese"),
    RagEvalCase("zh_dpo_vs_rlhf", "DPO 相比 RLHF 去掉了什么组件？",
                ("Direct Preference Optimization: Your Language Model Is Secretly a Reward Model",), ("reward model", "reinforcement"), "chinese"),
    RagEvalCase("zh_clip_zero_shot", "CLIP 是如何实现零样本图像分类的？",
                ("Learning Transferable Visual Models From Natural Language Supervision",), ("contrastive", "zero-shot", "image-text"), "chinese"),
    RagEvalCase("zh_faiss_search", "FAISS 如何实现十亿规模的向量检索？",
                ("Billion-Scale Similarity Search with GPUs",), ("nearest-neighbor", "product quantization", "gpu"), "chinese"),
    RagEvalCase("zh_tot_reasoning", "Tree of Thoughts 如何通过回溯改进推理？",
                ("Tree of Thoughts: Deliberate Problem Solving with Large Language Models",), ("backtracking", "thought", "search"), "chinese"),
    RagEvalCase("zh_distilbert_compress", "DistilBERT 通过什么方法压缩模型？",
                ("DistilBERT, a distilled version of BERT: smaller, faster, cheaper and lighter",), ("distillation", "student", "latency"), "chinese"),
    RagEvalCase("zh_megatron_parallel", "Megatron-LM 用哪种并行策略训练超大模型？",
                ("Megatron-LM: Training Multi-Billion Parameter Language Models Using Model Parallelism",), ("tensor", "parallelism", "partition"), "chinese"),
    RagEvalCase("zh_federated_privacy", "联邦学习 FedAvg 如何保护用户隐私？",
                ("Communication-Efficient Learning of Deep Networks from Decentralized Data",), ("federated", "client", "privacy"), "chinese"),
)


def run_rag_quality_eval(*, write_report: bool = False) -> dict[str, Any]:
    started = time.perf_counter()
    project = ResearchProject.objects.create(
        title=f"PaperLens RAG quality eval {time.strftime('%Y%m%d-%H%M%S')}",
        description="Archived deterministic fixture project for hybrid RAG evaluation.",
        status="archived",
    )
    with _embedding_patch():
        _seed_eval_project(project)
        case_results = [asyncio.run(_run_case(project.id, case)) for case in RAG_EVAL_CASES]
    metrics = _aggregate(case_results)
    result = {
        "passed": metrics["recall_at_5"] >= 0.80
        and metrics["citation_coverage"] >= 0.90
        and metrics["unsupported_claim_rate"] <= 0.10
        and len(case_results) >= 30,
        "project_id": project.id,
        "case_count": len(case_results),
        "duration_ms": round((time.perf_counter() - started) * 1000, 2),
        "metrics": metrics,
        "cases": case_results,
    }
    return result


async def _run_case(project_id: int, case: RagEvalCase) -> dict[str, Any]:
    started = time.perf_counter()
    paper_ids = await sync_to_async(_project_paper_ids)(project_id)
    texts = await hybrid_retrieve_texts(case.question, paper_ids=paper_ids, final_k=5)
    titles = [text.paper.title for text in texts]
    contexts = [text.content for text in texts]
    first_rank = _first_gold_rank(titles, case.gold_titles)
    is_no_evidence = len(case.gold_titles) == 0
    relevant_flags = [_is_relevant(text, case) for text in texts]
    context_precision = sum(relevant_flags) / len(relevant_flags) if relevant_flags else 0.0
    combined = " ".join(contexts).lower()
    term_hits = [term for term in case.expected_terms if term.lower() in combined]
    citation_coverage = sum(1 for text in texts if text.citation_key) / len(texts) if texts else 0.0
    faithfulness = 1.0 if (is_no_evidence or len(term_hits) >= min(2, len(case.expected_terms))) else 0.0
    unsupported = 1.0 if is_no_evidence and context_precision > 0 else 0.0
    passed = (first_rank > 0 and len(term_hits) > 0) if not is_no_evidence else unsupported == 0.0
    return {
        "id": case.id,
        "category": case.category,
        "question": case.question,
        "gold_titles": list(case.gold_titles),
        "retrieved_titles": titles,
        "passed": passed,
        "recall_at_5": 0.0 if is_no_evidence else (1.0 if first_rank > 0 else 0.0),
        "mrr": 0.0 if first_rank == 0 else round(1.0 / first_rank, 4),
        "context_precision": round(context_precision, 4),
        "citation_coverage": round(citation_coverage, 4),
        "faithfulness": faithfulness,
        "unsupported_claim": unsupported,
        "term_hits": term_hits,
        "latency_ms": round((time.perf_counter() - started) * 1000, 2),
    }


def _seed_eval_project(project: ResearchProject) -> None:
    for index, item in enumerate(RAG_EVAL_PAPERS):
        paper = Paper.objects.create(
            title=item.title,
            abstract=item.abstract,
            year=item.year,
            citation_count=100 - index,
            pdf_url=f"https://paperlens.local/eval/{index}.pdf",
        )
        ProjectPaper.objects.create(
            project=project,
            paper=paper,
            status="included",
            added_by="demo",
            source_reason="RAG quality fixture",
        )
        chunks = [
            f"{item.title}. {item.abstract}",
            item.text,
            f"{item.abstract} {item.text}",
        ]
        for chunk_index, content in enumerate(chunks):
            vector = _lexical_embed([f"{item.title} {content}"])[0]
            Text.objects.create(
                paper=paper,
                docname=f"{item.title[:32]} chunk{chunk_index}",
                chunk_index=chunk_index,
                content=content,
                embedding=vector.tolist(),
                embedding_model="eval-lexical",
                embedding_dim=len(vector),
                embedding_version="eval-lexical:v1",
                content_hash=hashlib.sha256(content.encode("utf-8")).hexdigest(),
                page_start=chunk_index + 1,
                page_end=chunk_index + 1,
                section="Evaluation fixture",
                char_start=0,
                char_end=len(content),
                search_vector=f"{item.title}\n{item.abstract}\n{content}",
                citation_key=make_citation_key_for_paper(paper.id),
            )


def _aggregate(cases: list[dict[str, Any]]) -> dict[str, Any]:
    evidence_cases = [case for case in cases if case["gold_titles"]]
    return {
        "recall_at_5": _avg([case["recall_at_5"] for case in evidence_cases]),
        "mrr": _avg([case["mrr"] for case in evidence_cases]),
        "context_precision": _avg([case["context_precision"] for case in evidence_cases]),
        "citation_coverage": _avg([case["citation_coverage"] for case in cases]),
        "faithfulness": _avg([case["faithfulness"] for case in cases]),
        "unsupported_claim_rate": _avg([case["unsupported_claim"] for case in cases]),
        "average_retrieval_latency_ms": round(_avg([case["latency_ms"] for case in cases]), 2),
        "passed_cases": sum(1 for case in cases if case["passed"]),
        "total_cases": len(cases),
    }


@contextmanager
def _embedding_patch() -> Iterator[None]:
    dimension = int(getattr(settings, "PAPERLENS_EMBEDDING_DIM", 1024))
    metadata = {
        "embedding_model": "eval-lexical",
        "embedding_dim": dimension,
        "embedding_version": f"eval-lexical:dim{dimension}:v1",
    }
    with (
        mock.patch("rag.retrieval.embed", _lexical_embed),
        mock.patch("rag.retrieval.embedding_metadata", lambda: metadata),
    ):
        yield


_VOCAB = (
    "transformer", "attention", "quadratic", "memory", "compute", "parallel", "global",
    "mamba", "selective", "state", "scan", "linear", "exact", "rare", "token",
    "retrieval", "augmented", "generation", "rag", "recall", "mrr", "precision",
    "citation", "coverage", "faithfulness", "unsupported", "latency", "evidence",
    "graph", "bibliographic", "coupling", "root", "frontier", "related",
    "dblp", "venue", "author", "metadata", "openalex", "abstracts",
    "hybrid", "dense", "vector", "lexical", "fusion", "semantic", "terms",
    "postgresql", "pgvector", "hnsw", "nearest", "project", "scoped",
    "postgres", "full", "text", "tsvector", "tsquery",
    "pdf", "page", "section", "offset", "hash", "status", "retryable", "background",
    "langgraph", "workflow", "expand", "ingestion", "critic", "report", "persist", "artifact",
    # 新增领域词(微调/对齐/多模态/检索/推理/效率/安全/代码/Agent/分布式/隐私)
    "lora", "low-rank", "adapter", "parameter-efficient", "merge", "fine-tuning", "behavior",
    "dpo", "preference", "reward", "classification", "policy", "critic", "reinforcement",
    "clip", "contrastive", "image", "caption", "zero-shot", "prompt", "embedding", "multimodal", "vision-language", "transfer",
    "faiss", "nearest-neighbor", "product", "quantization", "compression", "billion", "gpu", "kernel", "ivf",
    "tree", "thoughts", "search", "backtracking", "chain-of-thought", "planning", "path",
    "node", "neighborhood", "edges",
    "humaneval", "python", "programming", "pass@k", "unit", "execution", "sampling", "code",
    "yarn", "rotary", "interpolation", "context", "window",
    "distilbert", "distillation", "student", "teacher", "parameters", "carbon",
    "knowledge", "provenance",
    "calibration", "confidence", "temperature", "ece", "binned", "confusion", "safety",
    "megatron", "tensor", "parallelism", "partition", "pipeline",
    "self-instruct", "bootstrap", "synthetic", "instruction", "filter",
    "federated", "client", "privacy",
)


def _lexical_embed(texts: list[str], *_, **__) -> np.ndarray:
    dimension = int(getattr(settings, "PAPERLENS_EMBEDDING_DIM", 1024))
    rows = []
    for text in texts:
        normalized = _normalize_eval_text(text)
        values = [float(normalized.count(term)) for term in _VOCAB]
        vector = np.zeros(dimension, dtype=np.float32)
        vector[: min(len(values), dimension)] = values[:dimension]
        norm = float(np.linalg.norm(vector))
        if norm:
            vector = vector / norm
        else:
            fallback_width = min(len(_VOCAB), dimension)
            vector[:fallback_width] = 1.0 / math.sqrt(fallback_width)
        rows.append(vector)
    return np.array(rows, dtype=np.float32)


def _normalize_eval_text(text: str) -> str:
    lowered = (text or "").lower()
    replacements = {
        "长序列": "long sequence",
        "长上下文": "long context",
        "优势": "advantage low-rank adapter parameter-efficient",
        "局限": "limitation",
        "成本": "cost compute memory",
        "评测": "evaluation recall mrr faithfulness",
        "指标": "metrics recall mrr coverage",
        "引用图谱": "citation graph",
        "页码": "page",
        "入库": "ingestion",
        "检索增强": "retrieval augmented generation",
        "向量": "vector embedding",
        "论文": "paper",
        # 新增中文 case 映射
        "微调": "fine-tuning low-rank adapter",
        "全量": "full fine-tuning",
        "组件": "component reward model reinforcement",
        "零样本": "zero-shot contrastive",
        "图像": "image caption",
        "向量检索": "nearest-neighbor vector product quantization gpu",
        "回溯": "backtracking search",
        "推理": "reasoning thought chain-of-thought",
        "压缩": "distillation student latency",
        "并行": "tensor parallelism partition pipeline",
        "超大模型": "multi-billion parameter model",
        "联邦学习": "federated learning client",
        "隐私": "privacy on-device",
    }
    for source, target in replacements.items():
        lowered = lowered.replace(source, target)
    lowered = lowered.replace("rare-token", "rare token")
    lowered = lowered.replace("project-scoped", "project scoped")
    lowered = lowered.replace("full-text", "full text")
    return lowered


def _project_paper_ids(project_id: int) -> list[int]:
    return list(ProjectPaper.objects.filter(project_id=project_id).values_list("paper_id", flat=True))


def _first_gold_rank(titles: list[str], gold_titles: tuple[str, ...]) -> int:
    for index, title in enumerate(titles, start=1):
        if any(gold.lower() in title.lower() for gold in gold_titles):
            return index
    return 0


def _is_relevant(text: Text, case: RagEvalCase) -> bool:
    if any(gold.lower() in text.paper.title.lower() for gold in case.gold_titles):
        return True
    haystack = f"{text.paper.title} {text.content}".lower()
    return sum(1 for term in case.expected_terms if term.lower() in haystack) >= 2


def _avg(values: list[float]) -> float:
    return round(sum(values) / len(values), 4) if values else 0.0


def dumps_rag_quality_eval(result: dict[str, Any]) -> str:
    return json.dumps(result, ensure_ascii=False, indent=2, default=str)
