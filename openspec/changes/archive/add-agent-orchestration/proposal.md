# Change: add-agent-orchestration

## Why（为什么做）
地基 change 已交付：数据源工具层（datasources.search）+ DeepSeek 客户端（complete/complete_with_tools）+ papers 本地库。但这些能力目前是孤立的——没有一个编排层把它们串成"输入研究问题 → 自动搜索 → 汇总"的闭环。

本 change 引入 LangGraph 多智能体编排，让 Agent 能：把一个研究问题分解成检索子任务，用 Function Calling 调 datasources 工具，把结果汇总成结构化输出。这是后续 RAG 证据层、引用图谱、综述生成的骨架。

## What（改什么）
- 新建 `agent` 包。
- 实现 LangGraph StateGraph：基于 open_deep_research 当前 main 的 **supervisor 模式**（非旧 planner/research_team/writer）。
  - 节点：`planner`（把问题分解成检索计划）→ `researcher`（ReAct 子图，调 datasources 工具）→ `synthesizer`（汇总）。
  - supervisor 并行 researcher 子图（asyncio.gather，max_concurrent 控制）。
- 把 datasources.search 包装成 Function Calling 工具，供 researcher ReAct 调用。
- State 4 类 + reducer（缝合 open_deep_research：override vs operator.add，防上下文爆炸）。
- 引用图谱节点、RAG 证据层、SSE 流式端点**不在本 change**（各自独立 change）。
- 端到端验证：一个问题跑完整图，产出结构化综述（带来源），不依赖图谱/RAG。

## Out of scope
- 引用图谱构建（独立 change add-citation-graph）
- 全文 RAG/PDF 解析/RCS reranker（独立 change add-rag-pipeline）
- pqac 引用格式（RAG change 一起做，本 change 综述只附来源列表）
- SSE 流式端点（独立 change add-sse-streaming）
- 前端

## 风险
- LangGraph 依赖较重，需确认与 Django 异步环境的兼容（agent 跑在 async，ORM 调用需 sync_to_async）。
- supervisor 并行 researcher 子图的 token 预算控制——需 max_concurrent + max_iterations 兜底。
- DeepSeek Function Calling 在多轮 ReAct 中的稳定性——靠 max_react_tool_calls 兜底。
