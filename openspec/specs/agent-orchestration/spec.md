# Spec: agent-orchestration

## Purpose

Define bounded multi-agent research orchestration that plans work, runs parallel
researchers, and synthesizes cited results with controlled state and recovery.
## Requirements
### Requirement: 多智能体编排图

PaperLens 的显式长研究工作流 MUST 使用持久化、可恢复且有界的编排状态；普通项目 Chat
MUST 保持 deterministic router + bounded ReAct Harness，不得仅为工具路由进入 LangGraph。

#### Scenario: 显式长研究工作流

- **WHEN** 用户明确启动包含检索扩展、论文入库、项目 RAG、critic 和报告持久化的长研究任务
- **THEN** 系统 MUST 为一个 ProjectRun 创建唯一持久化工作流身份
- **AND** LangGraph MUST 使用该 ProjectRun ID 作为稳定 checkpoint thread identity
- **AND** Celery MUST 只执行可幂等重投的工作单元，不得成为第二工作流状态所有者。

#### Scenario: 完整图运行

- **WHEN** 已批准的显式长研究工作流进入图执行
- **THEN** planner、researcher 和 synthesizer MUST 按持久化状态、条件边和已提交依赖事实执行
- **AND** 最终结果 MUST 包含结构化产物、来源和可恢复的运行状态
- **AND** 节点失败或等待 MUST NOT 通过重新提交普通 Chat 来恢复。

#### Scenario: 入库等待与恢复

- **GIVEN** 工作流已经创建或复用了一个或多个论文入库依赖
- **WHEN** 任一依赖尚未进入成功、失败或不可用终态
- **THEN** 工作流 MUST 持久化 checkpoint 并进入 `waiting_ingestion`
- **AND** worker MUST 被释放，不得通过占用 worker 或在图内循环轮询等待
- **AND** 仅使用相同的 ProjectRun identity 才能恢复该工作流。

#### Scenario: 终态后恢复

- **WHEN** 所有入库依赖均已进入终态
- **THEN** 系统 MUST 至少一次请求恢复并以数据库状态幂等处理重复恢复
- **AND** 丢失的即时恢复消息 MUST 由周期性 reconciliation 在 30 秒内补偿
- **AND** RAG MUST NOT begin before the latest dependency terminal timestamp.

#### Scenario: Initial dependency enqueue is lost

- **GIVEN** a workflow dependency remains pending and its ingestion job has not begun an attempt
- **WHEN** the original broker publication is lost or fails before a worker claims it
- **THEN** periodic reconciliation MUST request the same idempotent ingestion job again
- **AND** the request MUST converge on the existing project job and global build.

#### Scenario: In-progress ingestion worker is lost

- **GIVEN** a workflow dependency is pending and its ingestion job has begun a non-terminal attempt
- **WHEN** the Celery worker disappears before the job reaches `embedded` or `failed`
- **THEN** a private database execution lease MUST expire without relying on broker visibility timeout
- **AND** periodic reconciliation MUST redispatch the same idempotent ingestion job within one
  execution-lease plus one Beat interval
- **AND** a live worker MUST renew the lease so a long-running valid parse is not treated as orphaned
- **AND** ownership at every durable boundary MUST require both the expected token and an unexpired
  execution lease in the same database transaction as the protected write
- **AND** an expired worker MUST NOT renew or clear its old lease, change job or run state, create or
  attach a build, write chunks, activate an index, publish events or terminalize the job even when
  its token has not yet been replaced
- **AND** a voluntary retry handoff MUST remain recoverable when its retry publication is lost
- **AND** duplicate delivery MUST still converge on one active build and one dependency terminal state.

#### Scenario: 单一执行 owner

- **WHEN** 相同 ProjectRun 被并发启动、恢复、重投或由 reconciliation 再次发现
- **THEN** 数据库租约 MUST 保证同一时刻最多一个有效 owner 推进有副作用的节点
- **AND** 有效 owner 存在时重复执行 MUST 安全退出
- **AND** owner 丢失或租约过期后另一个 worker MUST 能从 checkpoint 恢复。

#### Scenario: 幂等用户可见副作用

- **WHEN** worker 在节点执行前后崩溃或同一恢复任务被重复投递
- **THEN** 每个 ProjectRun MUST 最多产生一个归属报告、一个 committed RAG 结果事件和一个节点终态事件
- **AND** 允许重新执行未提交的只读计算
- **AND** 不得重复创建项目论文关系、入库构建、报告版本或其他用户可见产物。

#### Scenario: 受控的部分完成

- **GIVEN** 至少一个依赖失败或不可用
- **WHEN** 至少一篇目标论文具有当前项目可访问的 active 全文且报告引用全部解析并绑定
- **THEN** 工作流 MAY 以 `partial` 完成并 MUST 披露失败论文和证据缺口
- **AND** critic MAY 将结果降级为失败但 MUST NOT 绕过确定性证据门禁。

#### Scenario: 无可用全文

- **WHEN** 所有依赖终态后没有可用的 active 项目全文，或报告引用未解析绑定
- **THEN** 工作流 MUST 进入 `error`
- **AND** MUST NOT 创建报告版本
- **AND** 只允许稳定错误码和安全说明进入事件、日志和 API。

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

### Requirement: Durable workflow checkpoint safety

Workflow checkpoints MUST contain only the minimum safe state required to resume execution.

#### Scenario: Persist checkpoint state

- **WHEN** the workflow saves a checkpoint at a graph boundary
- **THEN** checkpoint state MUST be limited to project, run, paper, job and report identifiers,
  bounded counts, lifecycle states, stable codes and irreversible summaries
- **AND** the workflow MUST reload the question and project-scoped evidence from the authoritative
  database when a node needs them.

#### Scenario: Reject sensitive checkpoint content

- **WHEN** checkpoint tables and pending writes are audited
- **THEN** they MUST NOT contain API keys, full prompts, questions, PDF URLs, paths, paper bodies,
  evidence excerpts, report bodies or raw exceptions
- **AND** a sensitive sentinel on any prohibited surface MUST fail the release gate.

### Requirement: Durable workflow availability

The durable workflow MUST fail closed when its required persistence layer is unavailable or disabled.

#### Scenario: Feature disabled

- **WHEN** durable workflow execution is disabled by verified runtime configuration
- **THEN** a new research-expansion request MUST return stable `workflow_unavailable`
- **AND** it MUST NOT start the legacy uncheckpointed workflow.

#### Scenario: Checkpointer unavailable

- **WHEN** checkpoint storage is not initialized or cannot be reached
- **THEN** health output MUST report the workflow subsystem unavailable
- **AND** new workflow starts MUST return HTTP 503 with stable `workflow_unavailable`
- **AND** MUST fail without creating an executing run or user-visible artifact.
