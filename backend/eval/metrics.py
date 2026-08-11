"""评测指标：Recall@k / faithfulness(LLM-judge) / coverage。

吸取 AppPilot 教训：诚实量化，LLM-judge 标注是参考指标（有方差）。
"""
from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger(__name__)


def _norm(s: str) -> str:
    return (s or "").strip().lower()


def recall_at_k(retrieved_titles: list[str], gold_titles: list[str], k: int = 10) -> float:
    """Recall@k：top-k 检索结果里是否命中任一 gold title 关键词。

    按 gold 主题命中算：若 top-k 里任一标题包含某 gold 关键词，则 recall=1.0。
    """
    if not gold_titles:
        return 0.0
    topk = retrieved_titles[:k]
    gold_lower = [_norm(g) for g in gold_titles]
    for t in topk:
        t_low = _norm(t)
        for g in gold_lower:
            if g and g in t_low:
                return 1.0
    return 0.0


def _llm_judge(question: str, report: str, rubric: str) -> dict:
    """通用 LLM-as-judge，返回 {score: 0-1, reason}。thinking=False 降本。"""
    from llm.deepseek import DeepSeekClient

    client = DeepSeekClient()
    prompt = f"""你是严格的科研综述评审。按评分标准评估综述。

研究问题：{question}

综述：
{report[:3000]}

评分标准：{rubric}

输出严格 JSON：{{"score": 0.0到1.0的一位小数, "reason": "简短理由"}}
只输出 JSON。"""
    r = client.complete(
        [{"role": "user", "content": prompt}],
        thinking=False,
        max_tokens=300,
        response_format={"type": "json_object"},
    )
    try:
        data = json.loads(r["content"])
        return {"score": float(data.get("score", 0)), "reason": data.get("reason", "")}
    except (json.JSONDecodeError, ValueError, TypeError):
        return {"score": 0.0, "reason": "解析失败"}


def faithfulness(question: str, report: str, retrieval_context: list[str] | None = None) -> float:
    """综述忠实度：每条论断是否有来源支撑（0-1）。

    优先用 DeepEval FaithfulnessMetric（RAGAS 两步法：拆 claim → 逐条对 context 验证），
    judge 走 DeepSeek。若 DeepEval 不可用或 key 缺失，退回粗糙 LLM-judge rubric。
    """
    if retrieval_context is not None:
        try:
            from .judge import make_judge, judge_available

            if judge_available():
                from deepeval.metrics import FaithfulnessMetric
                from deepeval.test_case import LLMTestCase

                metric = FaithfulnessMetric(
                    threshold=0.5, model=make_judge(), include_reason=True, async_mode=False
                )
                test_case = LLMTestCase(
                    input=question,
                    actual_output=report,
                    retrieval_context=retrieval_context,
                )
                metric.measure(test_case)
                logger.info(
                    "faithfulness(deepeval) score=%.2f reason=%s",
                    metric.score,
                    (metric.reason or "")[:80],
                )
                return float(metric.score)
        except Exception as exc:
            logger.warning("deepeval faithfulness 失败，退回 rubric: %s", exc.__class__.__name__)

    # fallback：粗糙 LLM-judge rubric
    rubric = (
        "评估综述中事实性论断被引用/来源支撑的比例。"
        "0.0=全无引用支撑或大量编造，1.0=几乎所有论断有明确来源（论文标题/年份/作者）。"
        "若综述末尾有来源列表且正文论断引用了它们，给高分。"
    )
    return _llm_judge(question, report, rubric)["score"]


def coverage(question: str, report: str, gold_topics: list[str]) -> float:
    """综述覆盖度：是否覆盖了该主题的关键子主题（0-1）。"""
    if not gold_topics:
        # LLM 自评覆盖度
        rubric = "评估综述对该研究问题主题的覆盖完整性。0.5=只覆盖部分，1.0=全面覆盖主要方面。"
        return _llm_judge(question, report, rubric)["score"]
    rubric = (
        f"评估综述是否覆盖以下关键子主题：{', '.join(gold_topics)}。"
        "0.0=都没覆盖，1.0=全部覆盖。按覆盖比例给分。"
    )
    return _llm_judge(question, report, rubric)["score"]
