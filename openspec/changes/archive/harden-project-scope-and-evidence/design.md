# Design: harden-project-scope-and-evidence

## 1. Design Principles

1. 模型负责提出工具意图，不负责决定授权 scope。
2. 项目 scope 只有一个 resolver；工具不得自行解释空列表或 excluded 状态。
3. 工具得到证据、答案引用证据、引用解析成功、claim 被支持是不同事实。
4. 安全门依据 capability contract，不依据关键词或 marker 是否出现。
5. 先增加能复现现有缺陷的负向测试，再迁移实现。
6. 保留公开 API 和现有框架，避免以安全修复为名重写 Agent。

## 2. Trusted Execution Context

定义冻结数据结构：

```python
ToolExecutionContext(
    project_id: int,
    run_id: int | None,
    session_id: int | None,
    request_id: str,
    actor: str,
)
```

- context 由 API/Harness/MCP adapter 在服务端创建。
- 应用内模型可见 schema 不包含 `project_id/run_id/session_id/actor`。
- executor 在 schema validation 后注入 context；模型原始同名字段不得进入工具函数。
- 越权字段尝试记录安全事件，只记录字段名和安全 ID，不记录完整 prompt。
- direct Python calls 也必须显式传 context 或使用受限 compatibility adapter，不能回退
  到隐式 global scope。

## 3. ProjectScopeResolver

Resolver 是所有项目数据访问的唯一入口：

```text
paper_ids(include_excluded=False)
papers(include_excluded=False)
library_memberships(include_excluded=True)
project_paper(paper_id, include_excluded=False)
chunks(paper_ids=None, active_only=True)
graph_papers()
```

语义：

- `paper_ids=None`：使用 resolver 的完整当前项目 scope。
- `paper_ids=[]`：明确空 scope，返回空结果。
- 提供的 paper IDs 与 evidence scope 取交集；不存在、其他项目和 excluded 不可见。
- Resolver 必须区分两种 scope：
  - evidence scope：只包含当前项目非 excluded membership，用于 RAG、全文读取、比较、
    报告和引用图谱。
  - library inventory scope：包含当前项目全部 membership（可含 excluded）及其状态，
    用于用户管理、显式库存列表和恢复操作；不得包含 foreign/unlinked paper，也不得把
    excluded paper 的 chunks 作为证据返回。
- global maintenance 必须使用不同的显式 `GlobalScopeResolver` 或管理 service，不能给
  Project resolver 增加隐藏 bypass flag。
- Project tools、RAG、Citation Graph、Report、MCP 不得直接按外部 paper ID 查询全局
  `Text` 或 `Paper`。

错误语义：

- 用户请求不属于项目的对象：返回统一 `project_resource_not_found`，避免泄露对象在
  其他项目中是否存在。
- 内部 scope invariant 被违反：记录 ERROR，并使工具失败；不得降级为 global query。

## 4. EvidenceEnvelope

统一全文证据结构：

```python
EvidenceEnvelope(
    evidence_id: str,
    project_id: int,
    paper_id: int,
    chunk_id: int,
    content_hash: str,
    excerpt: str,
    page_start: int | None,
    page_end: int | None,
    section: str,
    retrieval_sources: tuple[str, ...],
    retrieval_scores: dict[str, float],
    embedding_version: str,
)
```

- `chunk_id` 是数据库稳定主键；`chunk_index` 只能作为展示元数据，不能作为真实性
  依据。
- `evidence_id` 从 project/paper/chunk/content_hash 的规范化表示生成，或使用持久化
  UUID；同一 chunk version 必须稳定。
- excerpt 只用于模型上下文和 UI，真实性由 chunk ID + content hash 解析。
- `query_project_rag`、`read_paper_section`、`compare_papers` 输出同一 envelope。
- metadata candidate 使用不同的 `MetadataEvidence` 类型，不得伪装成全文 envelope。

兼容策略：工具可继续返回现有 title/page/section 字段，并新增 `evidence`；Harness
优先读 envelope，在迁移期对旧格式只标 `legacy_unresolved`，不得自动升级为 resolved。

## 5. Citation Resolution And Claim Support

定义四层状态：

### Retrieval Status

工具是否获得 metadata/fulltext 候选，与答案是否使用无关。

### Reference Resolution Status

`resolved` 仅在 resolver 重新查询数据库并同时确认以下条件后成立：

- evidence.project_id 等于执行 context.project_id。
- paper 是项目当前非 excluded membership。
- chunk 主键存在且属于该 paper。
- chunk 是当前 active index version。
- content hash 与引用 envelope 一致。

其他结果为 `unresolved`，并记录安全原因码；不得写 `verified=True`。

### Citation Binding Status

记录回答中的 claim 是否指向 resolved evidence：`fully_bound | partially_bound |
unbound | not_required`。规则层可以检测 marker/claim 映射，但不能判断语义支持。

### Claim Support Status

`supported | contradicted | insufficient | pending | not_required`。Harness 初始只能写
`pending/not_required`；确定性规则或独立 Judge 才能写最终状态。Judge 输入必须包含
claim、resolved excerpt 和稳定 evidence ID，不能看到其他未引用上下文后自行挑证据。

## 6. Capability-Aware Evidence Policy

定义枚举，不使用关键词安全门：

| Capability | Answer mode | Minimum evidence | Citation required |
|---|---|---|---|
| list/export/search/graph/add result | `action_result` | metadata/structured artifact | 否，除非陈述论文内容 |
| factual project answer | `answered` | resolved fulltext | 是 |
| compare papers | `answered` | 每个比较对象至少一个 resolved fulltext | 是 |
| report/related work | `answered` | resolved fulltext | 是 |
| no evidence | `abstained` | none | 不引用无关论文 |
| ambiguous request | `clarified` | none | 否 |

安全门输入为 `CapabilityContract + resolved evidence summary + answer mode`。当证据不满足
最低要求时，Harness 必须 fail closed 为 `abstained/needs_more_evidence`，保留 raw answer
供内部评测，但不能把 unsupported raw answer 返回用户。

## 7. Tool And MCP Integration

- `execute_project_tool` 接受 context + validated args，统一生成 audit event。
- Project tool schema 由 Pydantic/JSON Schema 生成，`additionalProperties=false`。
- MCP schema 继续从相同工具 contract 派生。外部 MCP 在没有项目会话上下文时 MAY
  接受 `project_id` 作为资源选择器，但 adapter 必须先按本地/认证策略授权该选择，
  再建立可信 context；工具实现仍只读取 context。
- MCP output 增加 schema validation；错误返回稳定 error code，不返回 traceback 或对象存在性。
- destructive tools 继续不进入 Agent/MCP surface。

## 8. Migration Order

1. 写失败测试，复现 `read_paper_section` 跨项目泄漏和 citation 自证。
2. 引入 context/resolver，不修改工具输出。
3. 将 read/list/RAG/compare/graph/report/MCP 查询逐一迁移。
4. 引入 EvidenceEnvelope 和 resolver-backed citation resolution。
5. 替换 `_collect_evidence` 工具白名单为 typed evidence collection。
6. 启用 capability policy 和兼容字段。
7. 跑 Docker PostgreSQL 全回归和真实负向安全套件。

每一步必须保持可运行，禁止一次性重写 Harness。

## 9. Observability

每次工具调用记录：request_id、project_id、run_id、tool_name、scope paper count、result
count、duration、status 和安全 reason code。不得记录完整 excerpt、prompt 或论文正文。

新增事件：

- `tool_scope_violation`
- `citation_resolution`
- `evidence_policy_applied`

事件名可作为内部事件新增，不改变已有 SSE 必需事件名。

## 10. Risks And Mitigations

- 性能：每条 citation 重新查询可能产生 N+1。使用批量 resolver 按 chunk IDs 一次查询，
  但不缓存跨 run 的授权结果。
- 兼容：前端可能读取旧 citation 字段。迁移期保留字段但将旧 `verified` 标记 deprecated，
  其值不得用于门禁。
- 空 scope：旧 SQL helper 将空列表解释成全库。增加显式 None/empty 测试并禁止裸 helper。
- 测试假绿：SQLite 不足以验证 pgvector/项目查询。安全和 scope gate 必须在 Docker
  PostgreSQL 下运行。
- Judge 方差：reference resolution 使用确定性数据库验证；claim support 才允许 Judge，
  且单独报告 same-family/cross-judge。

## 11. Rejected Alternatives

- 给 `read_paper_section` 单独加一个 filter：无法覆盖其他工具和后续新增路径。
- 继续用 marker/title 匹配验证引用：无法证明数据库对象和项目归属。
- 让 LLM 输出 project_id 后做 prompt 约束：授权不能依赖模型服从。
- 为此切换 Agent framework：问题属于 domain contract，不属于 Agent loop 实现。
