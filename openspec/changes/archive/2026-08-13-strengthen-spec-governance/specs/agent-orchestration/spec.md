## MODIFIED Requirements

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
