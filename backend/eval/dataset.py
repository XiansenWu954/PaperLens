"""自建 CS 评测集（≥10 题，3 类：factual/recent/compare）。

吸取 AppPilot 教训：用同一评测集严格对比 baseline vs PaperLens。
gold 用 title 关键词（模糊匹配，适应检索标题变体）。
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class EvalItem:
    id: str
    type: str  # factual / recent / compare
    question: str
    gold_queries: list[str]  # 检索用的 gold 查询（验证检索召回）
    gold_titles: list[str] = field(default_factory=list)  # gold paper title 关键词（验证 Recall@k）
    gold_topics: list[str] = field(default_factory=list)  # 综述应覆盖的主题（验证 coverage）


EVAL_ITEMS: list[EvalItem] = [
    EvalItem(
        id="q01", type="factual",
        question="Transformer 的核心注意力机制是什么？",
        gold_queries=["attention is all you need"],
        gold_titles=["attention is all you need"],
        gold_topics=["self-attention", "query key value", "scaled dot-product"],
    ),
    EvalItem(
        id="q02", type="factual",
        question="BERT 的预训练方法是什么？",
        gold_queries=["BERT pre-training", "bidirectional encoder representations transformers"],
        gold_titles=["bert"],
        gold_topics=["masked language model", "next sentence prediction"],
    ),
    EvalItem(
        id="q03", type="recent",
        question="2024 年 Mamba 状态空间模型有哪些代表工作？",
        gold_queries=["Mamba state space model"],
        gold_titles=["mamba"],
        gold_topics=["selective state space", "linear time", "vision mamba"],
    ),
    EvalItem(
        id="q04", type="recent",
        question="近期大语言模型思维链推理的方法有哪些？",
        gold_queries=["chain of thought reasoning large language model"],
        gold_titles=["chain of thought", "chain-of-thought"],
        gold_topics=["chain of thought", "reasoning", "prompting"],
    ),
    EvalItem(
        id="q05", type="compare",
        question="检索增强生成 RAG 和模型微调相比有什么优劣？",
        gold_queries=["retrieval augmented generation", "fine-tuning"],
        gold_titles=["retrieval augmented", "rag"],
        gold_topics=["rag", "fine-tuning", "knowledge update"],
    ),
    EvalItem(
        id="q06", type="factual",
        question="ResNet 的残差连接解决了什么问题？",
        gold_queries=["residual learning deep networks", "resnet"],
        gold_titles=["deep residual learning", "resnet"],
        gold_topics=["residual", "vanishing gradient", "skip connection"],
    ),
    EvalItem(
        id="q07", type="recent",
        question="扩散模型 Diffusion 在图像生成上的最新进展？",
        gold_queries=["diffusion model image generation"],
        gold_titles=["diffusion"],
        gold_topics=["diffusion", "denoising", "score matching"],
    ),
    EvalItem(
        id="q08", type="compare",
        question="CNN 和 Vision Transformer 在视觉任务上各有什么优劣？",
        gold_queries=["vision transformer", "convolutional neural network comparison"],
        gold_titles=["vision transformer", "vit", "an image is worth"],
        gold_topics=["vision transformer", "cnn", "inductive bias", "attention"],
    ),
    EvalItem(
        id="q09", type="factual",
        question="强化学习中的 PPO 算法原理是什么？",
        gold_queries=["proximal policy optimization", "ppo"],
        gold_titles=["proximal policy optimization"],
        gold_topics=["ppo", "policy gradient", "clip"],
    ),
    EvalItem(
        id="q10", type="recent",
        question="图神经网络 GNN 的代表性方法有哪些？",
        gold_queries=["graph neural network"],
        gold_titles=["graph convolutional", "gat", "graphsage"],
        gold_topics=["graph", "message passing", "aggregation"],
    ),
    EvalItem(
        id="q11", type="compare",
        question="自监督学习 SSL 和监督学习在表示学习上的区别？",
        gold_queries=["self-supervised learning representation"],
        gold_titles=["self-supervised", "simclr", "byol", "moco"],
        gold_topics=["self-supervised", "contrastive", "labels"],
    ),
    EvalItem(
        id="q12", type="factual",
        question="GAN 生成对抗网络的对抗训练原理是什么？",
        gold_queries=["generative adversarial networks"],
        gold_titles=["generative adversarial"],
        gold_topics=["generator", "discriminator", "adversarial"],
    ),
]


def get_items() -> list[EvalItem]:
    return EVAL_ITEMS


def type_distribution() -> dict[str, int]:
    from collections import Counter
    return dict(Counter(it.type for it in EVAL_ITEMS))
