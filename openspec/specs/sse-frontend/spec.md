# Spec: sse-frontend

## Purpose

Define the compatible REST and SSE research flow and the frontend states required
to submit, observe, and read research tasks.

## Requirements

### Requirement: 研究任务 REST 端点
PaperLens MUST satisfy this requirement according to the scenarios below.
必须能通过 POST 创建研究任务，GET 查询任务结果。

#### Scenario: 创建任务
- **WHEN** POST /api/research {question}
- **THEN** 创建 ResearchTask(status=pending)，返回 {task_id}

#### Scenario: 查询结果
- **GIVEN** 任务已完成
- **WHEN** GET /api/research/<id>
- **THEN** 返回 {status:"done", report, citation_graph, sources}

### Requirement: SSE 流式端点
PaperLens MUST satisfy this requirement according to the scenarios below.
必须通过 SSE 实时推送 agent 工作过程。

#### Scenario: 流式事件
- **WHEN** GET /api/research/<id>/stream（连接 SSE）
- **THEN** 先收到 `: connected`
- **AND** 依次收到 step/token/graph 事件
- **AND** 最终收到 done 事件

### Requirement: 正确的 SSE 头
PaperLens MUST satisfy this requirement according to the scenarios below.
响应必须是 text/event-stream，禁缓冲。

#### Scenario: 头正确
- **WHEN** 连接 SSE 端点
- **THEN** Content-Type: text/event-stream
- **AND** Cache-Control: no-cache
- **AND** X-Accel-Buffering: no

### Requirement: LangGraph 流映射
PaperLens MUST satisfy this requirement according to the scenarios below.
LangGraph astream 的 updates/messages 事件必须映射为 SSE step/token/graph 事件。

#### Scenario: updates 映射
- **WHEN** astream 产出一个 updates chunk（node=planner）
- **THEN** 发送 event: step {node:"planner"}

#### Scenario: citation_graph 映射
- **WHEN** updates chunk node=citation_graph 含 vis_data
- **THEN** 发送 event: graph {nodes, edges}

#### Scenario: messages 映射
- **WHEN** astream 产出 messages chunk（token 增量）
- **THEN** 发送 event: token {text}

### Requirement: 任务持久化
PaperLens MUST satisfy this requirement according to the scenarios below.
agent 完成后必须把综述/图谱/来源持久化到 ResearchTask。

#### Scenario: 持久化
- **WHEN** agent 运行结束
- **THEN** ResearchTask.status=done，final_report/citation_graph/sources 已存

### Requirement: 前端 SSE 消费
PaperLens MUST satisfy this requirement according to the scenarios below.
Vue 前端必须通过 EventSource 消费 SSE，自动清理连接。

#### Scenario: 消费与清理
- **WHEN** 组件挂载开始 SSE
- **THEN** EventSource 注册 step/token/graph/done 监听
- **AND** 组件卸载时 es.close()（onScopeDispose）

### Requirement: 引用图谱可视化
PaperLens MUST satisfy this requirement according to the scenarios below.
前端必须用 d3-force 渲染引用图谱，节点 size∝citation/color∝year，可交互。

#### Scenario: 图谱渲染
- **WHEN** 收到 graph 事件
- **THEN** CitationGraph 用 d3-force 渲染节点和边
- **AND** 节点半径随 citation_count 缩放，颜色随 year 映射

### Requirement: 综述流式渲染
PaperLens MUST satisfy this requirement according to the scenarios below.
前端必须流式渲染综述（token 逐字追加）。

#### Scenario: 逐字渲染
- **WHEN** 收到 token 事件
- **THEN** 综述区域追加该 token 文本

### Requirement: 端到端验证
PaperLens MUST satisfy this requirement according to the scenarios below.
必须能从浏览器完成完整研究流程。

#### Scenario: 浏览器端到端
- **WHEN** 前端输入问题并发起
- **THEN** 看到 agent 步骤时间线 + 图谱渲染 + 综述流式生成
