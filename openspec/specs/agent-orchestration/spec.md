# Spec: agent-orchestration

## Purpose

Define bounded multi-agent research orchestration that plans work, runs parallel
researchers, and synthesizes cited results with controlled state and recovery.
## Requirements
### Requirement: 多智能体编排图

PaperLens 的显式长研究工作流 MUST 使用有界、可恢复的编排状态；普通项目 Chat MUST 保持
deterministic router + bounded ReAct Harness，不得仅为路由工具而进入 LangGraph。

#### Scenario: 显式长研究工作流

- **WHEN** 用户明确启动需要 checkpoint、waiting、resume、审批或多阶段状态的研究扩展
- **THEN** LangGraph MAY 编排 planner、researcher 和 synthesizer 节点
- **AND** Celery MUST 只执行可幂等重投的工作单元
- **AND** ProjectRun MUST 保持唯一工作流身份和可审计状态。

#### Scenario: 完整图运行

- **WHEN** 已批准的显式长研究工作流进入图执行
- **THEN** planner、researcher 和 synthesizer MUST 按持久化状态与条件边执行
- **AND** 最终结果 MUST 包含结构化产物、来源和可恢复的运行状态
- **AND** 节点失败或等待 MUST NOT 通过重新提交普通 Chat 来恢复。

#### Scenario: 普通项目对话

- **WHEN** 用户提出普通项目问答、检索、列表、比较或单次工具动作
- **THEN** 请求 MUST 使用 deterministic router + bounded ReAct Harness
- **AND** LangGraph MUST NOT be required solely to route or execute that request.

### Requirement: 问题分解
PaperLens MUST satisfy this requirement according to the scenarios below.
planner 必须把研究问题分解成多个检索子查询（sub_queries），用 Pydantic 结构化输出。

#### Scenario: planner 产出子查询
- **WHEN** planner 收到 "Mamba 最新进展"
- **THEN** 返回 ≥1 个 sub_query 字符串列表
- **AND** 子查询数 ≤ max_sub_queries 配置

### Requirement: Function Calling 检索
PaperLens MUST satisfy this requirement according to the scenarios below.
researcher 必须通过 Function Calling 调用 search_papers 工具检索论文，结果归一化并入本地库。

#### Scenario: researcher 调工具
- **WHEN** researcher 处理一个 sub_query
- **THEN** DeepSeek 发出 search_papers tool_call
- **AND** 工具执行 datasources.search 返回论文列表
- **AND** 论文 upsert 入 papers 本地库

### Requirement: 并行 researcher
PaperLens MUST satisfy this requirement according to the scenarios below.
多个 sub_query 的 researcher 必须并发执行（asyncio.gather），受 max_concurrent 上限约束。

#### Scenario: 并行执行
- **GIVEN** planner 产出 3 个 sub_query，max_concurrent=3
- **WHEN** fan_out_researchers 执行
- **THEN** 3 个 researcher 并发运行
- **AND** 各自的 notes/sources 累加回 AgentState

### Requirement: ReAct 工具调用预算
PaperLens MUST satisfy this requirement according to the scenarios below.
researcher 的 ReAct 循环必须有 tool_call_iterations 上限，防止无限调用。

#### Scenario: 超预算停止
- **WHEN** researcher 工具调用次数达到 max_tool_calls_per_researcher
- **THEN** 强制进入 extract_notes，不再调用工具

### Requirement: 上下文不爆炸
PaperLens MUST satisfy this requirement according to the scenarios below.
researcher 的完整 ReAct messages 不得上浮到 AgentState，只上报 notes + sources（输入/输出 state 分离）。

#### Scenario: 输出状态裁剪
- **WHEN** researcher 子图完成
- **THEN** 仅 notes 和 sources 累加到 AgentState
- **AND** AgentState 不包含 researcher 的完整 messages

### Requirement: 综述汇总
PaperLens MUST satisfy this requirement according to the scenarios below.
synthesizer 必须把所有 notes + sources 汇总成带来源的结构化综述。

#### Scenario: 产出综述
- **WHEN** synthesizer 收到累积的 notes + sources
- **THEN** 产出 markdown 综述
- **AND** 综述含来源列表（论文标题/作者/年份）

### Requirement: 成本控制

PaperLens MUST 从运行时配置读取可用模型和推理模式，并以质量、延迟、可靠性和 token 使用
分别评估，不得把短期模型名称或成本偏好固化为永久架构要求。

#### Scenario: 配置推理模式

- **WHEN** planner、researcher、critic 或 synthesizer 调用 DeepSeek
- **THEN** 模型名称和推理模式 MUST 来自已验证配置
- **AND** 评测产物 MUST 记录实际模型、模式、token、延迟和停止原因
- **AND** 未经同数据对照，不得声称关闭或开启 reasoning 提升了质量或效率。

#### Scenario: planner 关闭 reasoning

- **WHEN** 已验证配置为 planner 关闭 reasoning
- **THEN** 客户端 MUST 使用当前 DeepSeek API 支持的配置表达
- **AND** 该选择 MUST 作为本次运行配置记录，而不是作为永久架构常量
- **AND** 配置不可用时 MUST 明确失败或采用已记录的兼容模式，不得静默伪造测量结果。

### Requirement: 端到端验证
PaperLens MUST satisfy this requirement according to the scenarios below.
必须有一条命令跑通完整图并产出真实综述。

#### Scenario: runner 跑通
- **WHEN** 执行 `python -m agent.runner "<研究问题>"`
- **THEN** 全程不崩
- **AND** sources 累加 ≥3 条真实论文
- **AND** 产出带来源的综述
