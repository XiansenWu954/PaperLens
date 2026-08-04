# Spec delta: agent-orchestration

## ADDED Requirements

### Requirement: 多智能体编排图
系统必须有一个 LangGraph StateGraph，把研究问题经 planner → researcher(并行) → synthesizer 产出综述，复刻 open_deep_research 的 supervisor 模式。

#### Scenario: 完整图运行
- **WHEN** 输入一个研究问题运行 graph
- **THEN** 依次执行 planner、researcher(对每个 sub_query)、synthesizer
- **AND** 最终产出结构化综述字符串

### Requirement: 问题分解
planner 必须把研究问题分解成多个检索子查询（sub_queries），用 Pydantic 结构化输出。

#### Scenario: planner 产出子查询
- **WHEN** planner 收到 "Mamba 最新进展"
- **THEN** 返回 ≥1 个 sub_query 字符串列表
- **AND** 子查询数 ≤ max_sub_queries 配置

### Requirement: Function Calling 检索
researcher 必须通过 Function Calling 调用 search_papers 工具检索论文，结果归一化并入本地库。

#### Scenario: researcher 调工具
- **WHEN** researcher 处理一个 sub_query
- **THEN** DeepSeek 发出 search_papers tool_call
- **AND** 工具执行 datasources.search 返回论文列表
- **AND** 论文 upsert 入 papers 本地库

### Requirement: 并行 researcher
多个 sub_query 的 researcher 必须并发执行（asyncio.gather），受 max_concurrent 上限约束。

#### Scenario: 并行执行
- **GIVEN** planner 产出 3 个 sub_query，max_concurrent=3
- **WHEN** fan_out_researchers 执行
- **THEN** 3 个 researcher 并发运行
- **AND** 各自的 notes/sources 累加回 AgentState

### Requirement: ReAct 工具调用预算
researcher 的 ReAct 循环必须有 tool_call_iterations 上限，防止无限调用。

#### Scenario: 超预算停止
- **WHEN** researcher 工具调用次数达到 max_tool_calls_per_researcher
- **THEN** 强制进入 extract_notes，不再调用工具

### Requirement: 上下文不爆炸
researcher 的完整 ReAct messages 不得上浮到 AgentState，只上报 notes + sources（输入/输出 state 分离）。

#### Scenario: 输出状态裁剪
- **WHEN** researcher 子图完成
- **THEN** 仅 notes 和 sources 累加到 AgentState
- **AND** AgentState 不包含 researcher 的完整 messages

### Requirement: 综述汇总
synthesizer 必须把所有 notes + sources 汇总成带来源的结构化综述。

#### Scenario: 产出综述
- **WHEN** synthesizer 收到累积的 notes + sources
- **THEN** 产出 markdown 综述
- **AND** 综述含来源列表（论文标题/作者/年份）

### Requirement: 成本控制
planner 和 synthesizer 必须关闭 reasoning 降本；researcher 的 Function Calling 保留 reasoning。

#### Scenario: planner 关闭 reasoning
- **WHEN** planner 调用 DeepSeek
- **THEN** 使用 thinking=False（thinking=disabled）

### Requirement: 端到端验证
必须有一条命令跑通完整图并产出真实综述。

#### Scenario: runner 跑通
- **WHEN** 执行 `python -m agent.runner "<研究问题>"`
- **THEN** 全程不崩
- **AND** sources 累加 ≥3 条真实论文
- **AND** 产出带来源的综述
