# Design: align-project-constitution-v3

## 1. Design Goal

建立一个短小、可审计、不会与运行配置漂移的项目宪法。宪法只记录稳定架构
决策；模型版本、测试数量和临时性能数据留在环境配置或评测产物中。

## 2. Canonical Architecture Facts

### 2.1 Runtime

- Backend: Django + DRF + Daphne/ASGI。
- Primary integration/demo database: PostgreSQL + pgvector。
- SQLite: 仅限显式支持的本地 fallback 或隔离单元测试，不代表生产语义。
- Queue: Redis + Celery，承担 PDF 解析、embedding、外部检索和长任务工作单元。
- Frontend: Vue 3 + TypeScript + Pinia + Vite。

### 2.2 Models

- LLM provider 仍为 DeepSeek，模型名称来自环境配置，不在代码或 spec 中重复硬编码
  临时可用型号。
- 当前 embedding provider 为 BGE-M3，向量维度和 embedding version 必须随索引
  记录；测试默认使用 deterministic fake provider。
- 未来切换 embedding 必须新建 embedding version 并重建索引，不允许混用向量。

### 2.3 Configuration Sources

- Docker Compose 从根 `.env` 注入配置。
- 独立运行 Django 时可从 `backend/.env` 加载本地配置。
- `.env.example` 是公开配置契约；`settings.py` 的有效默认值和 health endpoint 必须
  与该契约一致。
- 密钥只能通过环境注入，不进入日志、报告、fixture、spec 或 README。

## 3. Agent Framework Boundaries

### Deterministic Router

保留为低方差意图基线和评测 oracle。明确、低风险的 list/export/graph 等意图不
需要 LLM 路由。

### ReAct Harness

负责普通项目对话的 bounded Function Calling、工具策略、预算、证据门禁、运行
事件和 fallback。Harness 是执行策略边界，不拥有数据库授权规则。

### LangGraph

只用于需要 checkpoint、等待、恢复、人工审批或多阶段持久状态的长研究任务。
普通 Chat 不为展示概念而迁移到图运行时。

### Celery

执行幂等工作单元，不与 LangGraph 同时拥有同一工作流。长流程由 LangGraph
拥有状态，Celery 负责节点提交的实际任务。

### MCP

只导出跨客户端有意义的稳定能力，并复用内部工具契约和授权边界。不得把所有
内部 helper MCP 化。

## 4. SOTA Adoption Rule

任何新框架或基础设施必须在独立 change 中回答：

1. 当前哪个可复现缺陷无法由现有框架局部解决？
2. 新方案删除了哪些重复实现或建立了什么新能力？
3. 同数据集、同环境的质量、延迟、可靠性和维护复杂度基线是什么？
4. 是否有 feature flag、兼容 adapter 和回滚路径？
5. 公开 API、数据格式和历史评测如何迁移？

无法回答以上问题时，默认不引入。

## 5. Documentation Truth Hierarchy

从高到低：

1. 已归档并合并到 `openspec/specs/` 的当前 capability spec。
2. `openspec/AGENTS.md` 中的协作和架构宪法。
3. 当前代码、迁移和环境配置形成的可执行契约。
4. 当前 Git SHA 对应的机器可读评测产物。
5. README、handoff 和人工总结。

若高低层冲突，必须建立 change 解决，禁止执行者自行选择有利版本。

## 6. Rejected Alternatives

- 直接修改归档 change：会篡改历史决策依据。
- 把所有运行参数写入 AGENTS：会让宪法频繁过期。
- 顺便重构业务代码：会破坏“规范校准可独立审阅”的边界。
- 用 README 作为架构真相源：README 面向公开展示，不适合承载强制约束。

## 7. Risks And Mitigations

- 风险：规范与当前实现仍有未发现差异。缓解：通过配置/依赖/spec 交叉检查任务列出
  所有差异，未确认项标为 unresolved，而不是猜测。
- 风险：把历史基线误写成当前事实。缓解：归档 change 不修改，只更新 current specs。
- 风险：模型名称再次变化。缓解：宪法只固定 provider 和配置来源，不固定短期型号。
