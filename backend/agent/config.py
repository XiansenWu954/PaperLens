"""Agent 配置（缝合 open_deep_research configuration.py 的预算控制思想）。"""
from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass
class AgentConfig:
    # 预算
    max_sub_queries: int = 3              # planner 最多分解几个子查询
    max_concurrent_researchers: int = 3   # 并行 researcher 上限
    max_tool_calls_per_researcher: int = 3  # 单 researcher 的工具调用预算
    max_notes_chars: int = 12000          # 单 researcher 笔记上限（喂 synthesizer 前裁剪）

    # 模型（地基验证：deepseek-v4-flash 可用，旧名停用）
    planner_model: str = os.environ.get("DEEPSEEK_MODEL", "deepseek-v4-flash")
    researcher_model: str = os.environ.get("DEEPSEEK_MODEL", "deepseek-v4-flash")
    synthesizer_model: str = os.environ.get("DEEPSEEK_MODEL", "deepseek-v4-flash")

    # reasoning 开关：planner/synthesizer 关闭降本；researcher FC 保留（地基验证）
    planner_thinking: bool = False
    researcher_thinking: bool = True
    synthesizer_thinking: bool = False


DEFAULT_CONFIG = AgentConfig()
