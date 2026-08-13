"""引用语境分类准确率评测（P1-6 升级盲区）。

PaperRelation 模型已上线（supporting/contradicting/mentioning），但零评测。
本模块构造 fixture（论文 A 引用 B，带摘要），gold 标注语境，
用真实 DeepSeek 分类，断言与 gold 一致率。
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

# fixture：论文 A 引用 B，gold 标注语境
RELATION_CASES = [
    {
        "name": "supporting - A 基于 B",
        "citing": {
            "title": "Improving BERT Fine-tuning with Layer-wise Decay",
            "abstract": "Building on the layer-wise learning rate decay method proposed by B, we extend it to multi-task settings.",
        },
        "cited": {
            "title": "Layer-wise Learning Rate Decay",
            "abstract": "We propose a layer-wise learning rate decay schedule for fine-tuning transformer models.",
        },
        "gold": "supporting",
    },
    {
        "name": "contradicting - A 反驳 B",
        "citing": {
            "title": "Revisiting Self-Attention: A Critical Study",
            "abstract": "We argue that the self-attention mechanism in B is computationally wasteful and propose a more efficient alternative.",
        },
        "cited": {
            "title": "Attention Is All You Need",
            "abstract": "We propose the Transformer, a network architecture based solely on attention mechanisms.",
        },
        "gold": "contradicting",
    },
    {
        "name": "mentioning - 仅提及",
        "citing": {
            "title": "A Survey of NLP Models",
            "abstract": "Various architectures have been proposed for NLP, including RNNs, CNNs, and the model in B among others.",
        },
        "cited": {
            "title": "GPT: Generative Pre-trained Transformer",
            "abstract": "We introduce GPT, a generative pre-trained transformer for language understanding.",
        },
        "gold": "mentioning",
    },
    {
        "name": "supporting - A 用 B 的数据集",
        "citing": {
            "title": "Our Model on GLUE Benchmark",
            "abstract": "We evaluate our model on the GLUE benchmark (B) and achieve state-of-the-art results across all tasks.",
        },
        "cited": {
            "title": "GLUE: A Multi-Task Benchmark",
            "abstract": "We present GLUE, a collection of natural language understanding tasks.",
        },
        "gold": "supporting",
    },
]


def _make_fake_paper(data: dict):
    class _P:
        pass
    p = _P()
    p.title = data["title"]
    p.abstract = data["abstract"]
    return p


def run_relation_quality() -> dict[str, Any]:
    """用真实 DeepSeek 分类引用语境，对比 gold 标注。

    返回 {case_count, accuracy, per_case[], passed}。passed: accuracy >= 0.6。
    LLM 分类有方差，60% 是合理阈值（3/4 正确即可）。
    """
    from api.views import _classify_citation_context
    from .judge import judge_available

    if not judge_available():
        return {
            "skipped": True,
            "reason": "DEEPSEEK_API_KEY 未配置，跳过引用语境真实评测",
            "passed": False,
        }

    correct = 0
    per_case = []
    for case in RELATION_CASES:
        citing = _make_fake_paper(case["citing"])
        cited = _make_fake_paper(case["cited"])
        label, context, confidence = _classify_citation_context(citing, cited)
        ok = label == case["gold"]
        if ok:
            correct += 1
        per_case.append({
            "name": case["name"],
            "gold": case["gold"],
            "predicted": label,
            "correct": ok,
            "confidence": round(confidence, 3),
            "context": context[:100],
        })
    n = len(RELATION_CASES)
    accuracy = correct / n if n else 0.0
    result = {
        "case_count": n,
        "accuracy": round(accuracy, 4),
        "per_case": per_case,
        "passed": accuracy >= 0.6,
    }
    logger.info("relation quality: accuracy=%.2f (%d/%d) passed=%s", accuracy, correct, n, result["passed"])
    return result
