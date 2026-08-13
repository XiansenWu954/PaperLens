"""交互式端到端评估脚本：以"用户"身份实时驱动 PaperLens 智能体并采集质量评估。

流程：seed demo project → 对 5 类 intent 各发一条真实问题 → 采集 answer/events/quality_check
→ 用 DeepSeek-as-judge 按 grounding/usefulness/citation_integrity 打分 → 产出 JSON 报告。

用法：
    python manage.py shell -c "from eval.interactive_eval import run_interactive_eval; run_interactive_eval()"

或作为 management command（见 evaluate_interactive）。需要 DEEPSEEK_API_KEY + 运行中的后端。
"""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

API_BASE = "http://127.0.0.1:8000"

# 5 类 intent 的真实问题（覆盖 answer/search_add/library/graph/report）
INTERACTIVE_PROMPTS = [
    {"intent": "answer", "message": "Mamba 和 Transformer 在序列建模上的核心区别是什么？"},
    {"intent": "search_add", "message": "继续扩大关于 state space model 的论文范围"},
    {"intent": "library", "message": "列出当前项目论文库有哪些论文"},
    {"intent": "graph", "message": "刷新项目的引用图谱"},
    {"intent": "report", "message": "生成一段关于序列建模方法的 related work 草稿"},
]

JUDGE_PROMPT = """你是严格的研究助手评估员。评估以下 Agent 回答的质量。

用户问题：{question}
Agent 回答（节选）：
{answer}

项目证据（Agent 检索到的）：
{evidence}

Agent 的自评 verdict：{verdict}（grounded=有证据支撑，partial=部分，needs_more_evidence=缺证据）

按三个维度打分（0.0-1.0）：
- grounding：回答论断是否被项目证据支撑（不编造）
- usefulness：回答对 CS 研究者是否直接有用（非空话套话）
- citation_integrity：是否标注了来源（论文标题/[cite]标记），无伪造引用

输出严格 JSON：{{"grounding": 0.0到1.0, "usefulness": 0.0到1.0, "citation_integrity": 0.0到1.0, "score": 三项平均, "reason": "一句话"}}
只输出 JSON。"""


def _call_api(method: str, path: str, **kwargs) -> dict:
    """调用本地后端 API（需后端运行）。"""
    import httpx
    with httpx.Client(base_url=API_BASE, timeout=120) as client:
        r = getattr(client, method)(path, **kwargs)
        if r.status_code >= 400:
            raise RuntimeError(f"API {method} {path} failed: {r.status_code} {r.text[:200]}")
        return r.json()


def _judge_answer(question: str, answer: str, evidence: list, verdict: str, intent: str = "") -> dict:
    """用 DeepSeek-as-judge 给单轮回答打分。

    对 graph/library 类查询，证据标准放宽（图谱节点/论文列表本身就是证据），
    不要求 RAG chunk 支撑——避免用 RAG 标准不公平地评判非 RAG 回答。
    """
    from llm.deepseek import DeepSeekClient
    client = DeepSeekClient()
    # graph/library 类查询：以图谱节点/论文列表为证据，不要求 RAG chunks
    is_structural = intent in {"graph", "library"}
    if is_structural:
        evidence_text = "（该查询的证据是图谱节点/论文列表本身，而非 RAG 文档片段。评判 grounding 时，答案列出的具体论文标题/节点即为证据支撑。）"
        grounding_hint = "grounding：答案是否基于项目内真实论文/图谱节点（列出了具体论文标题即算有支撑），不要求 RAG 文档片段。"
    else:
        evidence_text = "\n".join(
            f"- {e.get('title') or e.get('docname') or 'paper'}: {e.get('summary', '')[:100]}"
            for e in (evidence or [])[:5]
        ) or "（无证据）"
        grounding_hint = "grounding：回答论断是否被项目证据支撑（不编造）"
    prompt = JUDGE_PROMPT.format(
        question=question[:200],
        answer=answer[:1500],
        evidence=evidence_text[:800],
        verdict=verdict,
    ).replace("grounding：回答论断是否被项目证据支撑（不编造）", grounding_hint)
    r = client.complete(
        [{"role": "user", "content": prompt}],
        thinking=False, max_tokens=300,
        response_format={"type": "json_object"},
    )
    try:
        return json.loads(r["content"])
    except (json.JSONDecodeError, ValueError):
        return {"grounding": 0, "usefulness": 0, "citation_integrity": 0, "score": 0, "reason": "解析失败"}


def run_interactive_eval(project_id: int | None = None) -> dict[str, Any]:
    """端到端交互评估：seed（可选）→ 5 类 intent 真实问答 → judge 打分。"""
    started = time.perf_counter()

    # 1. 准备项目
    if project_id is None:
        seed = _call_api("post", "/api/projects/demo-seed", json={})
        project_id = seed["project"]["id"]
        logger.info("interactive eval: seeded demo project #%d", project_id)

    rounds: list[dict[str, Any]] = []
    for case in INTERACTIVE_PROMPTS:
        round_started = time.perf_counter()
        # 2. 发送问题（非流式 chat，同步返回 answer + events）
        try:
            result = _call_api("post", f"/api/projects/{project_id}/chat",
                               json={"message": case["message"]})
        except Exception as exc:
            rounds.append({"intent": case["intent"], "message": case["message"],
                           "error": exc.__class__.__name__, "passed": False})
            continue

        answer = result.get("answer", "")
        events = result.get("events", [])
        # 从 events 提取 quality_check + evidence
        quality = next((e["data"] for e in events if e["event"] == "quality_check"), {})
        evidence_event = next((e for e in events if e["event"] == "evidence"), None)
        evidence = evidence_event["data"].get("evidence", []) if evidence_event else []
        tools = [e["data"].get("name") for e in events if e["event"] == "tool_call"]

        # 3. judge 打分(self-judge: DeepSeek 评 DeepSeek)
        try:
            judge = _judge_answer(case["message"], answer, evidence, quality.get("verdict", "unknown"), case["intent"])
        except Exception as exc:
            judge = {"score": 0, "reason": f"judge 失败: {exc.__class__.__name__}"}

        # 3b. cross-judge(若配置了第二评判模型,消除 self-judge 偏差)
        cross = None
        try:
            from .cross_judge import cross_judge_available, cross_judge_answer
            if cross_judge_available():
                cross = cross_judge_answer(case["message"], answer, evidence, quality.get("verdict", "unknown"), case["intent"])
        except Exception as exc:
            cross = {"score": 0, "reason": f"cross-judge 失败: {exc.__class__.__name__}"}

        # 以 cross-judge 为准(若可用),否则用 self-judge
        decisive_score = cross.get("score", 0) if cross else judge.get("score", 0)
        # Strict per-case gate (deepseek-live-evaluation-plan §6.4/§10 item 4):
        # the old 0.60 was too lenient. A single core intent must reach 0.65.
        passed = decisive_score >= 0.65
        rounds.append({
            "intent": case["intent"],
            "message": case["message"],
            "answer_preview": answer[:300],
            "answer_chars": len(answer),
            "tools_called": tools,
            "self_verdict": quality.get("verdict"),
            "evidence_count": quality.get("evidence_count", 0),
            "self_judge": judge,
            "cross_judge": cross,
            "judge_score": decisive_score,
            "judge_model": "cross" if cross else "self",
            "passed": passed,
            "duration_ms": round((time.perf_counter() - round_started) * 1000, 2),
        })
        logger.info("interactive [%s] verdict=%s score=%.2f model=%s passed=%s",
                    case["intent"], quality.get("verdict"), decisive_score,
                    "cross" if cross else "self", passed)

    scores = [r["judge_score"] for r in rounds if isinstance(r.get("judge_score"), (int, float))]
    avg_score = sum(scores) / len(scores) if scores else 0
    # self-judge 单独的平均分(用于暴露偏差)
    self_scores = [r["self_judge"]["score"] for r in rounds
                   if isinstance(r.get("self_judge", {}).get("score"), (int, float))]
    avg_self_score = sum(self_scores) / len(self_scores) if self_scores else 0
    passed_count = sum(1 for r in rounds if r.get("passed"))

    result = {
        "version": "1.0",
        "generated_at": datetime.utcnow().strftime("%Y%m%d_%H%M%S"),
        "project_id": project_id,
        "model": "deepseek-v4-flash (real) + bge-m3 (real)",
        "rounds": rounds,
        "average_judge_score": round(avg_score, 4),
        "average_self_judge_score": round(avg_self_score, 4),
        "judge_bias_note": (
            f"self-judge avg {avg_self_score:.2f} vs decisive avg {avg_score:.2f}; "
            + ("cross-judge active, bias mitigated" if any(r.get("cross_judge") for r in rounds)
               else "self-judge only (DeepSeek 评 DeepSeek),可能有 ~15-20% 抬高偏差")
        ),
        "passed_rounds": passed_count,
        "total_rounds": len(rounds),
        # Strict overall gate (§6.4): average >= 0.80 AND every core intent >= 0.65.
        # The old 0.60 average let low-quality answers through.
        "passed": avg_score >= 0.80 and all(r.get("judge_score", 0) >= 0.65 for r in rounds),
        "thresholds": {"average": 0.80, "per_case_min": 0.65},
        "duration_ms": round((time.perf_counter() - started) * 1000, 2),
    }
    return result


def write_report(result: dict) -> Path:
    reports_dir = Path(__file__).resolve().parents[0] / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    target = reports_dir / f"interactive_eval_{result['generated_at']}.json"
    target.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return target
