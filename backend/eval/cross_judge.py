"""第二评判模型交叉评估,消除 self-judge 偏差。

业界共识:LLM 评同家族模型输出有 ~15-20% 抬高偏差(self-enhancement bias)。
本模块支持配置一个独立的第二评判模型(如 GPT-4-mini)对 DeepSeek 的输出交叉打分。

配置(环境变量):
    EVAL_CROSS_JUDGE_MODEL=gpt-4o-mini
    EVAL_CROSS_JUDGE_BASE_URL=https://api.openai.com/v1
    EVAL_CROSS_JUDGE_API_KEY=sk-...
不配置时 cross_judge 不可用,报告只输出 self-judge 分数(并标注 self-judge 风险)。
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any

logger = logging.getLogger(__name__)


def cross_judge_available() -> bool:
    """是否配置了第二评判模型。"""
    return bool(os.environ.get("EVAL_CROSS_JUDGE_API_KEY", ""))


def cross_judge_label() -> str:
    return os.environ.get("EVAL_CROSS_JUDGE_MODEL", "unknown")


def cross_judge_answer(question: str, answer: str, evidence: list, verdict: str, intent: str = "") -> dict[str, Any]:
    """用第二评判模型对单轮回答打分,与 self-judge 交叉对比。

    返回 {grounding, usefulness, citation_integrity, score, reason, model}。
    """
    from llm.deepseek import DeepSeekClient

    model = os.environ.get("EVAL_CROSS_JUDGE_MODEL", "gpt-4o-mini")
    base_url = os.environ.get("EVAL_CROSS_JUDGE_BASE_URL", "https://api.openai.com/v1")
    api_key = os.environ.get("EVAL_CROSS_JUDGE_API_KEY", "")

    client = DeepSeekClient(model=model, base_url=base_url, api_key=api_key)
    is_structural = intent in {"graph", "library"}
    grounding_hint = (
        "grounding: answer is based on real project papers/nodes (listing concrete paper titles counts as support)"
        if is_structural
        else "grounding: answer claims are supported by project evidence (no fabrication)"
    )
    evidence_text = "（structural query: graph nodes/paper list are the evidence）" if is_structural else (
        "\n".join(f"- {e.get('title') or e.get('docname')}: {e.get('summary', '')[:100]}" for e in (evidence or [])[:5])
        or "（no evidence）"
    )
    prompt = f"""You are a strict evaluator for a research paper Agent. Score the answer.

Question: {question[:200]}
Answer (truncated):
{answer[:1500]}
Evidence: {evidence_text[:800]}
Agent self-verdict: {verdict}

Score three dimensions (0.0-1.0):
- {grounding_hint}
- usefulness: directly useful to a CS researcher (not generic)
- citation_integrity: sources marked (paper titles/[cite] tags), no fabricated citations

Output strict JSON: {{"grounding": 0.0-1.0, "usefulness": 0.0-1.0, "citation_integrity": 0.0-1.0, "score": average, "reason": "one sentence"}}
Only output JSON."""
    r = client.complete(
        [{"role": "user", "content": prompt}],
        thinking=False, max_tokens=300,
        response_format={"type": "json_object"},
    )
    try:
        data = json.loads(r["content"])
        data["model"] = model
        return data
    except (json.JSONDecodeError, ValueError):
        return {"grounding": 0, "usefulness": 0, "citation_integrity": 0, "score": 0, "reason": "parse failed", "model": model}
