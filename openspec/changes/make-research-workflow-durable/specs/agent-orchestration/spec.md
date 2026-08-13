## MODIFIED Requirements

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
