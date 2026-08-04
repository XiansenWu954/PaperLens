# Change: add-mcp-and-evaluation（收尾）

## Why（为什么做）
前 5 个 change 完成了 agent 内核 + 全栈产品。本 change 补齐最后两个技术栈缺口，完成求职量化：

1. **MCP 双向**：把 PaperLens 自建工具层（search_papers / gather_evidence）**导出为标准 MCP server**，并**消费外部 MCP server**——展示对 Model Context Protocol 的完整掌握（2025-2026 最热 Agent 协议）。
2. **自建 CS 评测集 + 严格 baseline 对比**：吸取 AppPilot 教训（改进不严格对比 = 无效），用**同一评测集**对比 baseline（朴素搜索+摘要）vs PaperLens（含图谱+RAG），产出可量化数字（Recall@k / faithfulness / coverage）。

## What（改什么）
### MCP 部分
- 新建 `mcp_server/` 包：用官方 `mcp` SDK 把 search_papers + gather_evidence 导出为 MCP server（stdio 协议），任何 MCP 客户端（Claude/Cursor）可调用。
- 新建 `mcp_client/`：一个消费侧 demo，连接本地 MCP server 调工具（验证双向闭环）。
- researcher 的工具列表**可选**从 MCP server 获取（展示消费侧），但默认走本地（性能）。

### 评测部分
- 新建 `eval/` 包：自建 CS 评测集（10-15 题，三类：事实查找/列最新方法/对比 X 与 Y），人工标 gold papers。
- 评测指标实现：检索 Recall@k、答案 faithfulness（LLM-as-judge）、coverage。
- baseline variant：朴素搜索（datasources.search 直出摘要，无图谱无RAG）。
- 对比脚本：同一评测集跑 baseline vs PaperLens，产出对比报告（诚实，吸取 AppPilot 教训）。

## Out of scope
- 大规模评测集（100+题）—— 10-15 题够证明方法论，标注成本高。
- 人工标注众包 —— 自标。
- 外部 arxiv-mcp server 运行（网络/依赖重）—— 用自建 server 验证双向闭环。

## 风险
- MCP stdio 协议在测试环境的进程管理——测试用 in-process client/server 避免子进程复杂度。
- 评测的 LLM-as-judge 有方差——多跑取均值 + 明确标注是参考指标。
- baseline 与 PaperLens 用同一 DeepSeek，避免模型差异污染对比。
