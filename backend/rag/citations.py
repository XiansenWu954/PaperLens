"""pqac 引用格式（缝合 PaperQA2 types.py Context + utils.py encode_id）。

基于 paper_id 生成稳定 key（同一论文切片共享 key，综述引用归并到论文更直观）。
注入 Valid Keys 防 LLM 幻觉引用；正则回引解析。
"""
from __future__ import annotations

import hashlib
import re

CITATION_RE = re.compile(r"\bpqac-[a-zA-Z0-9]{8}\b")
KEY_TEMPLATE = "pqac-{id}"


def make_citation_key_for_paper(paper_id: int) -> str:
    """基于 paper_id 生成稳定的 pqac key。"""
    h = hashlib.md5(str(paper_id).encode()).hexdigest()[:8]
    return KEY_TEMPLATE.format(id=h)


def parse_citations(text: str) -> list[str]:
    """从文本提取所有 pqac 引用 key（去重保序）。"""
    seen: dict[str, None] = {}
    for m in CITATION_RE.findall(text):
        seen.setdefault(m, None)
    return list(seen.keys())


def valid_keys_prompt(keys: list[str]) -> str:
    """构造注入 LLM 的 Valid Keys 约束（防幻觉引用）。"""
    if not keys:
        return ""
    return (
        f"只能使用以下引用 key 标注来源：{', '.join(keys)}。"
        "格式为 (pqac-xxxxxxxx)，不要编造未列出的 key。"
    )
