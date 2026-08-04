"""synthesizer 节点：notes + sources → markdown 综述（缝合 open_deep_research final_report_generation）。

用 thinking=False 降本（汇总不需要思维链）。综述含来源列表。
"""
from __future__ import annotations

import logging

from ..config import AgentConfig
from ..prompts import SYNTHESIZER_SYSTEM
from ..state import AgentState

logger = logging.getLogger(__name__)


async def synthesizer(state: AgentState, config: AgentConfig) -> dict:
    """汇总所有 notes + sources 成综述。"""
    from llm.deepseek import DeepSeekClient

    notes = state.get("notes", [])
    sources = state.get("sources", [])
    citation_graph = state.get("citation_graph") or {}

    notes_block = "\n\n---\n\n".join(f"### 研究笔记 #{i+1}\n{n}" for i, n in enumerate(notes))
    sources_block = _format_sources(sources)

    # ★护城河：注入引用图谱的三类分析，让综述按奠基/前沿/子主题组织
    graph_hint = citation_graph.get("synthesis_hint", "")
    graph_section = f"\n\n# 引用图谱分析（请据此组织综述结构）\n{graph_hint}\n" if graph_hint else ""

    user_content = (
        f"# 研究问题\n{state['question']}\n\n"
        f"# 研究者笔记\n{notes_block}\n\n"
        f"# 来源论文元数据\n{sources_block}"
        f"{graph_section}\n\n"
        f"请基于以上撰写研究综述。若提供了引用图谱分析，请按'奠基性论文/最新前沿/子主题簇'组织综述结构。"
    )

    client = DeepSeekClient(model=config.synthesizer_model)
    r = client.complete(
        [
            {"role": "system", "content": SYNTHESIZER_SYSTEM},
            {"role": "user", "content": user_content},
        ],
        thinking=config.synthesizer_thinking,
        max_tokens=2048,
    )
    logger.info("synthesizer -> report %d chars, %d sources", len(r["content"]), len(sources))
    return {"final_report": r["content"]}


def _format_sources(sources: list[dict]) -> str:
    """格式化来源列表供 synthesizer 参考。"""
    if not sources:
        return "（无来源论文）"
    lines = []
    seen = set()
    for s in sources:
        title = s.get("title", "")
        if title in seen:
            continue
        seen.add(title)
        authors = ", ".join((s.get("authors") or [])[:2])
        year = s.get("year", "")
        cite = s.get("citation_count", 0)
        lines.append(f"- {title} ({year}) [{authors}] 引用数={cite}")
    return "\n".join(lines)
