# Spec delta: mcp-and-evaluation

## ADDED Requirements

### Requirement: MCP server 导出工具
PaperLens 必须把自己的工具层（search_papers/gather_evidence）导出为标准 MCP server（stdio 协议），任何 MCP 客户端可发现并调用。

#### Scenario: 工具发现
- **WHEN** MCP 客户端连接 paperlens server 并 list_tools
- **THEN** 返回 search_papers 和 gather_evidence 两个工具
- **AND** 每个工具有 name/description/inputSchema

#### Scenario: 工具调用
- **WHEN** 客户端 call_tool("search_papers", {"query":"..."})
- **THEN** 返回 TextContent，内容是检索结果 JSON

### Requirement: MCP client 双向验证
必须有 client 验证双向闭环：能连接 server → 列工具 → 调用 → 拿结果。

#### Scenario: client demo
- **WHEN** 运行 mcp client demo
- **THEN** 连接 server 成功
- **AND** 列出 ≥2 工具
- **AND** 调用 search_papers 返回论文结果

### Requirement: CS 评测集
必须自建 CS 评测集（≥10题，覆盖事实/最新方法/对比三类），含 gold 标注。

#### Scenario: 评测集覆盖
- **WHEN** 加载 EVAL_ITEMS
- **THEN** ≥10 题，覆盖 factual/recent/compare 三类
- **AND** 每题有 question + gold_titles

### Requirement: 检索 Recall@k 指标
必须计算检索结果命中 gold 的 Recall@k（k=10）。

#### Scenario: Recall 计算
- **GIVEN** gold_titles 含 "Mamba"，检索返回 10 条其中 3 条标题含 "Mamba"
- **WHEN** 计算 Recall@10
- **THEN** 命中 gold（至少 1 条命中即 recall=1.0，按 gold 主题命中算）

### Requirement: 答案 faithfulness 指标
必须用 LLM-as-judge 评估综述每条论断是否有引用支撑（faithfulness）。

#### Scenario: faithfulness 评估
- **WHEN** 对一篇综述评 faithfulness
- **THEN** 返回 0-1 之间分值（有引用支撑论断占比）

### Requirement: baseline vs PaperLens 严格对比
必须用同一评测集、同一 LLM 跑 baseline（朴素搜索）vs PaperLens（图谱+RAG），产对比表。

#### Scenario: 对比报告
- **WHEN** 运行 eval.run_eval
- **THEN** 产出对比表（baseline vs paperlens 的 Recall@k/faithfulness/coverage 均值）
- **AND** 结果如实记录（不粉饰，吸取 AppPilot 教训）

### Requirement: 同一 LLM 控制变量
baseline 与 PaperLens 必须用同一 DeepSeek 模型，避免模型差异污染对比。

#### Scenario: 控制变量
- **WHEN** 跑对比
- **THEN** 两个 variant 用相同 model 参数
