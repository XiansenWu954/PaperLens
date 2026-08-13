# Tasks

## 1. Failing Security Tests First

- [x] 1.1 增加 `read_paper_section` 跨项目全文读取复现测试，修复前必须失败。
  > `agent/scope_failing_tests.py::CrossProjectReadLeakTest`: FAIL — 泄漏 CROSS_PROJECT_SECRET_CONTENT。
- [x] 1.2 为所有项目级 read/query/graph/report 工具建立参数化 scope 测试：当前项目、
  其他项目、excluded、无 membership、空 scope。
  > `ProjectToolScopeMatrixTest`: read_paper_section foreign=FAIL, excluded=FAIL; empty scope=FAIL。
  > compare_papers foreign=ok (已修); query_project_rag foreign=ok (已有 scope filter)。
  > 复审未完成:缺 current positive control、无 membership、graph/report；compare/RAG
  仅断言 foreign 不出现，空结果也会通过；empty scope 只检查 SQL 字符串而非查询行为。
  > 第二轮复审:inventory 显示当前项目 excluded membership 不是 P0；测试必须区分
  library inventory scope 与 evidence scope，并继续验证 foreign/unlinked 不可见。
  > 第三轮复审:32-case 重写仍有 RAG foreign 空通过、底层 `paper_ids=None` 契约错层、
  report positive 不证明 own fulltext、graph fixture 混入无边图语义，且数据库类型报告矛盾。
  > Batch D 验收:read/list/RAG/compare/graph/report 已具备 own positive 与
  foreign/excluded/unlinked/empty 的 non-vacuous 契约；PostgreSQL 产物固定。
- [x] 1.3 为全部 MCP project tools 增加相同的跨项目负向测试。
  > 未开始:MCP tools 的 scope 测试。
  > 第三轮复审:MCP 已有部分 dispatch case，但 foreign RAG 可空通过，`content` 非空不等于
  output schema 验证，graph fixture 无确定 own edge，仍不可勾选。
  > Batch D 验收:list/RAG/graph 与 output schema 红契约已覆盖；MCP request meta 中的
  trusted project context 对抗 selector case须在 Task 2.5 实现前先补红，当前保持未勾选。
  > Tasks 2.x 完成:MCP-TRUSTED-CONTEXT 红测已在 2.5 实现前补红（产物
  > `run-tasks2-red-mcpctx`），2.5 落地后转绿；outputSchema 与 structured_content 已随 2.5
  > 实现。§18.2 后：trusted-context 测试改经真实 handler entry 并以
  > `PAPERLENS_MCP_PROJECT_ID` 作为 server-bound 来源，client `_meta` 伪装对抗测试已补。
  > 勾选状态待 Codex 复审确认。
  > Codex 最终验收:in-process ClientSession 真实经过 SDK runner；server-bound、client `_meta`、
  > selector bootstrap 与统一 error shape均已覆盖，获准勾选。
- [x] 1.4 增加模型传入伪造 `project_id` 的对抗测试，证明服务端 context 覆盖且记录事件。
  > 复审部分覆盖:executor 收到真实 project_id，但测试吞掉任意 Exception，未断言模型 schema
  不暴露授权字段，也未断言 scope-violation audit event。
  > 第三轮复审:schema 未先断言工具集合非空；audit 未断言 run_id 与安全 payload/敏感信息缺失。
  > Batch D 验收:schema/execution/audit 三层红绿契约完整；audit 要求有效 run_id、安全摘要且
  排除完整参数、prompt、key、正文和 traceback。
- [x] 1.5 增加伪 citation 测试：不存在 chunk、其他项目 chunk、错误 hash、inactive version、
  marker-only 均不得 resolved。
  > 复审无效:测试没有把 fake evidence 注入 context，citations 为空，for-loop 零次执行而通过。
  应直接用 crafted context 调用现有 `_quality_check`，先复现当前字段自认证。
  > 第三轮复审:nonexistent/foreign 已非空注入；wrong-hash 复合断言仍会假通过，且缺 CIT-VALID。
  > Batch D 验收:nonexistent/foreign/wrong-hash/marker-only/valid 已覆盖；inactive version依赖
  Task 3.3 的版本模型，在实现 CitationResolver 时先补红，当前保持未勾选。
  > Tasks 3.x 完成:CIT-NONEXISTENT/FOREIGN/WRONG-HASH 经数据库驱动 resolver 转绿；
  CIT-INACTIVE-VERSION 在 3.3 实现前补红（`run-tasks3-red`）、实现后转绿；CIT-VALID 保持绿；
  CIT-LEGACY-UNRESOLVED 覆盖 legacy 不自动升级。
- [x] 1.6 增加 metadata bypass 测试：metadata 不得满足 factual/compare/report fulltext policy。
  > 复审无效:只断言没有 fulltext citation，没有断言 unsupported factual answer 被替换、
  answer_mode=abstained 或 evidence_status 不足；当前测试还能触发重复工具循环。
  > 第三轮复审:factual 有进展，但 compare/report case仍不存在，文件头与报告声明不实。
  > Batch D 验收:factual/compare/report 三类红测和 list/search/export/graph 四类 action
  正向控制已固定，能够复现 typed collection 与 capability policy 缺陷。

> 第三轮通用门禁:`NetworkCallCounter` 仅定义未安装；“counter=0”无效。临时红测文件不在
> 默认 discovery 命名中，修复后必须迁入完整回归。详见内部指令 §13。

## 2. Trusted Scope Boundary

- [x] 2.1 实现冻结 `ToolExecutionContext` 和统一 context 创建入口。
  > `agent/context.py`：frozen dataclass（project_id/run_id/session_id/request_id/actor）+ 唯一
  > `create_context()`；harness 在 run 创建后注入 run/session id；ChatAgentLoop 可接收外部 context；
  > `api/views.py` 图端点经 `create_context(project_id)` 调用 executor。
  > §18.1 修复：`ChatAgentLoop(project_id=A, context=B)` 立即抛 ValueError（冻结 context 是唯一
  > 可信 identity，日志/scope-violation/executor 全部派生自 context）；模型夹带
  > `project_id/run_id/session_id/actor` 统一记录 `rejected_fields`（仅字段名，不记值/prompt/
  > payload/key）；新增 OVERRIDE-CONTEXT-MISMATCH 与 OVERRIDE-REJECTED-FIELDS 对抗测试。
- [x] 2.2 从模型可见 Function Calling schema 删除授权字段并禁止额外字段。
  > `PROJECT_AGENT_TOOLS` 全部移除 `project_id` 并设 `additionalProperties=false`；
  > OVERRIDE-SCHEMA 红测转绿（9 工具逐一验证）。
- [x] 2.3 实现 `ProjectScopeResolver`，明确 None、empty、excluded 和 not-found 语义。
  > `agent/scope.py`：`None`=完整 evidence scope；`[]`=fail closed；请求 ids 与 evidence scope
  > 交集；`project_paper()` 对 foreign/excluded/unlinked/nonexistent 统一返回 None（无存在性信号）；
  > `library_memberships()` 保留 excluded 及状态（inventory 语义不变）。
- [x] 2.4 将 `read_paper_section`、list、RAG、compare、graph、report 查询迁移到 resolver。
  > 全部迁移；read 采用统一 scoped not-found 形状（READ-FOREIGN/EXCLUDED/UNLINKED 红测转绿）；
  > compare/graph/report/list/export 行为与既有契约一致（矩阵测试保持绿色）。
- [x] 2.5 将 MCP project tools 迁移到相同 resolver 和 context。
  > selector 视为 transport-level context bootstrap：校验项目存在后创建冻结 context；
  > server-bound request meta 优先，参数不得覆盖（MCP-TRUSTED-CONTEXT 红测在实现前补红、
  > 实现后转绿）；声明 outputSchema 并返回 `structured_content`（MCP-OUTPUT-SCHEMA 转绿）；
  > 单用户安全边界在 `_resolve_mcp_context` 注释与报告中显式记录。
  > §18.2 修复：handler 改为 SDK 真实签名 `(ServerRequestContext, CallToolRequestParams)`，
  > 直接消费 typed params；server-bound project 仅来自进程配置
  > `PAPERLENS_MCP_PROJECT_ID`（stdio 单用户模式），客户端 `params.meta` 永不可信
  > （`_meta` 实际是 open map，但 handler完全不读取它作为 identity）；missing/invalid/nonexistent
  > selector 统一返回 `project_not_found` 稳定 error shape；测试改走真实 registered
  > handler entry（in-process memory transport + ClientSession），覆盖 server-bound A +
  > selector B、client `_meta` 伪装、无 bound + 合法 selector bootstrap。
  > Codex 最终验收:真实 transport/handler 契约、identity mismatch与 rejected-fields审计通过。
- [x] 2.6 删除或封闭会把空 paper IDs 退化成全库的 project-scope helper。
  > `rag/retrieval.py`：`_paper_scope_sql([])` 生成空集合 ANY 子句、python 路径 `__in=[]`
  > 均为空结果；RAG-EMPTY-IDS 红测转绿；`None` 仍为底层全局（项目边界由 resolver/wrapper 保证）。

## 3. Evidence And Citation Contracts

- [x] 3.1 实现 `EvidenceEnvelope` 和 `MetadataEvidence` 类型及 schema tests。
  > `agent/evidence.py`：frozen `EvidenceEnvelope`（evidence_id/project_id/paper_id/chunk_id/
  > content_hash/excerpt/page/section/retrieval_sources/retrieval_scores/embedding_version，
  > chunk_index 仅展示）+ `MetadataEvidence`（metadata，绝不伪装 fulltext）；
  > ENVELOPE-RAG/READ/COMPARE/METADATA schema case 固定。
  > §20.1 修复：evidence_id 由 project/paper/chunk/content_hash/embedding_version 的规范化
  > 表示经固定摘要确定性生成（EVIDENCE-ID-STABLE/CONTENT-CHANGE/VERSION-CHANGE case），
  > query/read/compare 共用同一 factory，ENVELOPE-* 断言生产工具输出与 factory 一致。
  > §21 修复：canonical evidence_id 由 parser 与 CitationResolver 独立重算并精确比较
  > （EVIDENCE-ID-MISMATCH / CIT-EVIDENCE-ID-MISMATCH）；同一 declared identity 对应不同
  > canonical payload 时整组 fail closed（duplicate_identity_conflict），交换输入顺序结果一致
  > （CIT-DUPLICATE-EVIDENCE-ID-DIFFERENT-PAYLOAD）；make_evidence_id 保留完整 SHA-256。
  > §22 收口：evidence_id 为必填非空字符串，完全缺失也 fail closed（CIT-MISSING-EVIDENCE-ID，
  > 同批保留合法 sibling）；retrieval_status 分别统计有效 fulltext/metadata，仅 legacy（含
  > foreign/missing-project metadata）为 none，由 legacy_unresolved_count 表达
  > （COLLECT-FOREIGN-METADATA-ONLY，无 sibling 掩盖）；prebuilt store 从 scoped+active texts
  > 重建，Top-K 被 forbidden 占据时合法 own-active 仍被召回（PYTHON-STORE-ALLOWED-BELOW-K）。
  > §21 静态复审再次撤回：parser 尚未验证 `evidence_id` 是否由 envelope 字段规范重算得到，
  > 也未真正验证 ID 字段基本类型；伪造/碰撞 ID 仍可影响去重和 resolver identity。
  > §22 最终静态复审：canonical mismatch 已修；剩余 direct CitationResolver 在 evidence_id
  > 完全缺失时仍可继续解析，必须将缺失声明 ID 一并 fail closed。
- [x] 3.2 让 query/read/compare 工具输出统一 envelope，同时保留必要兼容字段。
  > query_project_rag fulltext / read_paper_section chunks / compare_papers chunks 均携带
  > 完整 envelope 并保留 title/summary/citation/chunk_index 等前端与 API 兼容字段；
  > metadata fallback 输出 `MetadataEvidence` 形状。
  > §20.2 修复：active embedding model/version 在 resolver.chunks 与 retrieval 候选查询层
  > （postgres/python 双路径）统一过滤，stale chunk 不再进入模型上下文；每工具新增
  > stale-chunk 负向控制（RAG-STALE-EXCLUDED/READ-STALE-EXCLUDED/COMPARE-STALE-EXCLUDED）
  > 并保留 active own chunk 正向。
  > §21 修复：python 候选路径对预构建 NumpyVectorStore 的 dense 结果按 scoped+active queryset
  > 的稳定 Text IDs 再过滤（PYTHON-STORE-SCOPE-ACTIVE，own-stale/foreign 均不可旁路）；
  > compare 的 has_fulltext 改为按 resolver 当前 active chunks 判定。
  > §21.4 修复：citation binding 统一解析显式 [cite:<marker>] token（extract_citation_markers，
  > _answer_has_any_marker/_ensure_source_markers/_quality_check 共用），裸 marker 与自然语言
  > 子串不再绑定（CIT-BARE-MARKER-NOT-BOUND / CIT-SUBSTRING-NOT-BOUND /
  > CIT-EXPLICIT-TOKEN-BOUND）。
  > §21 静态复审再次撤回：Python fallback 在传入已有 `NumpyVectorStore` 时直接 search 该 store，
  > 可绕过 scoped/active queryset；compare 的 `has_fulltext` 仍按任意 Text 判断而非 active chunks。
  > §22 最终静态复审：泄漏旁路已关闭，但 prebuilt store 仍先按原 store Top-K 再过滤；若 Top-K
  > 全为 forbidden/stale，合法 own-active 候选会被饿死。需从 scoped texts 建 store 或足量检索后截断。
- [x] 3.3 实现批量、数据库驱动的 `CitationResolver`。
  > `agent/citations.py`：一次 chunk 查询 + 一次 membership 查询批量验证——context 项目一致、
  > 当前非 excluded membership、chunk 主键及 paper 归属、content_hash 匹配、embedding_version
  > 匹配当前 active index version（fixture 通过 `embedding_metadata()` 明确 active version，
  > 未降低生产约束）；CIT-INACTIVE-VERSION 红测先补红后转绿。
  > §20.3 修复：envelope project_id 必填且与 context 精确相等（CIT-MISSING-PROJECT），缺失/
  > 畸形/不同均 fail closed；envelope 声明的 embedding_version 必须与数据库 chunk 版本一致
  > （CIT-ENVELOPE-VERSION-MISMATCH）；解析身份以 evidence_id 为主、同 marker 多候选全部
  > 保留（CIT-DUPLICATE-MARKER，非 first-wins）；畸形 chunk/paper/project id 逐条 fail
  > closed 不中断整批（CIT-MALFORMED-IDENTITY，含有效正向对照）。
  > §21 静态复审再次撤回：resolver 信任传入 evidence_id 且按该值去重，未独立验证其与
  > project/paper/chunk/hash/version 的规范身份一致；同 evidence_id 不同 payload 仍会 first-wins。
- [x] 3.4 将 retrieval、reference resolution、citation binding、claim support 分字段持久化。
  > quality 结果分离 `retrieval_status` / `reference_resolution_status` /
  > `citation_binding_status` / `claim_support_status`（claim 初始 pending/not_required）+ per-
  > citation `reference_resolution_status`/`resolution_reason`；随既有 quality_check 事件
  > 持久化到 ProjectRunEvent JSON，未新增数据库表。
  > §21 静态复审撤回：`marker.lower() in answer.lower()` 会把正文中普通标题/短字符串出现
  > 误判为 citation binding；必须解析明确 `[cite:...]` token 并精确匹配规范化 marker。
- [x] 3.5 移除 Harness 对 `reference_resolved` 的自认证逻辑。
  > `_quality_check` 改为 async 并调用 CitationResolver；移除 `has_chunk and is_fulltext` 自认证；
  > legacy/非 envelope 条目标记 `legacy_unresolved`（CIT-LEGACY-UNRESOLVED），绝不自动升级。
- [x] 3.6 用 typed evidence collection 替代按工具名收集证据的白名单。
  > `_collect_evidence` 扫描全部工具结果的 `evidence` 与 `chunks`，经统一 parser/factory
  > （`parse_evidence_item`）校验必填字段与基本类型；有效 fulltext envelope 与 metadata
  > 单独解析；迁移期旧结构显式降级为 legacy（`__legacy_unresolved`），不计入 fulltext
  > availability；空/畸形结构直接丢弃（COLLECT-FULLTEXT-MALFORMED、COLLECT-METADATA-VALID、
  > §21 修复：parser 真实校验字段类型（project/paper/chunk 必须正整数，bool 不算 int；
  > hash/version/evidence_id 必须非空字符串）与 metadata project identity（缺失/foreign 均不
  > 计入 metadata retrieval，COLLECT-FULLTEXT-WRONG-TYPES / COLLECT-METADATA-MISSING-PROJECT /
  > COLLECT-METADATA-FOREIGN-PROJECT）；字段齐全但类型错误或 canonical ID 错误属 malformed。
  > COLLECT-LEGACY-DOWNGRADE case）；META-COMPARE 红测保持红（留给 Task 4.x）。
  > §20.4 修复：collector 经统一 parser/factory 校验 envelope 必填字段与基本类型，仅
  > `evidence_type` 标签不再视为 typed evidence；残缺旧结构显式降级为 `legacy_unresolved`。
  > §21 静态复审再次撤回：当前 parser 只检查非空，没有验证 int-compatible identity、canonical
  > evidence_id；MetadataEvidence 甚至不要求其类型定义中的 project_id，报告中的“基本类型校验”
  > 与代码不一致。
  > §22 最终静态复审：类型与 trusted project 校验已修；但 foreign/missing-project metadata 被
  > 降为 legacy 后仍进入 evidence list，而 retrieval_status 的 fallback 将任意非空 evidence 标为
  > metadata，导致“foreign metadata 不计入 metadata retrieval”的声明与实现不一致。
  > §22 最终验收：缺失 evidence_id、legacy retrieval 分类与 prebuilt-store Top-K 饥饿均已修复；
  > `run-tasks3-final` 记录 88 targeted case（85 PASS / 3 expected META FAIL）和 224 默认回归 OK。
  > Codex 静态复审批准 Tasks 3.1-3.6，允许进入 Tasks 4.x。

## 4. Capability Evidence Policy

- [x] 4.1 定义 capability contract enum/matrix，不使用答案关键词推断安全要求。
  > `agent/capability.py`：`Capability`（action/factual/compare/report/clarify）+
  > `CapabilityContract`（requires_resolved_bound_fulltext / per_paper_fulltext）；
  > `capability_for_intent(intent)` 由 intent/router 结构化结果映射（compare>report>factual
  > 严格优先）。`harness._quality_check` 删除答案长度、中文关键词、called-tools 组合推断，
  > answer_mode 完全由 contract 决定；工具调用不降低要求（factual 请求调 list/export 仍为
  > factual，POLICY-ACTION-CANNOT-BYPASS）。
  > Codex 静态复审撤回：`library` 同时覆盖纯列表和“推荐/核心论文/为什么”等内容推理，后者虽调用
  > RAG 仍被映射为 ACTION，可无引用输出论文内容；必须在 intent 中结构化区分。
- [x] 4.2 action/list/export/search/graph 与 factual/compare/report 使用不同最低证据等级。
  > ACTION 契约允许 metadata/结构化结果（list/search/export/graph/add 全不替换）；
  > FACTUAL/REPORT 要求至少一条答案显式绑定的 resolved fulltext（bound 仅认
  > [cite:<marker>] token）；COMPARE 按 compare_papers 结构化目标逐侧验证覆盖，
  > 缺失侧进入 `compare_missing_paper_ids` 并披露（POLICY-COMPARE-FULL / ONE-SIDE、
  > POLICY-FACTUAL-BOUND/UNBOUND/LEGACY/UNRESOLVED、POLICY-REPORT-BOUND/UNBOUND、
  > POLICY-ACTION-ADD）；META-FACTUAL/COMPARE/REPORT 三个红测转绿。
  > Codex 静态复审撤回：compare target 从工具返回 `papers` 推导，结果遗漏目标或为空时可缩小
  > obligation；ACTION 也未要求相关工具成功，错误结果后模型仍可把操作描述为成功。
- [x] 4.3 证据不足时 fail closed，保留 raw answer 供内部评测但不返回 unsupported 内容。
  > stream() 安全门按 contract 判定：不满足 → 标准 abstention（compare 附缺口披露），
  > `safety_replaced=True`，`raw_model_answer` 保留，拒答后不再附加 [cite:] 或“证据依据”
  > （POLICY-ABSTENTION-NO-CITE）；模型自称 answered/grounded 不能绕过（POLICY-SELF-CLAIM）。
  > Codex 静态复审撤回：COMPARE 在 target set 为空时 `compare_missing=[]`，可在零 resolved evidence
  > 下得到 answered；action tool error 同样缺少 deterministic fail-closed 结果。
- [x] 4.4 abstention 不得绑定无关项目论文来提高 grounded 指标。
  > 拒答文本不含任何 [cite:] token；eval 指标将 `safety_replaced` 的合规拒答排除出
  > source_required（agent_harness/agent_quality 适配），不会因绑定无关引用提升 grounded。`intent.py`
  > 新增结构化 compare/export 意图（INTENT_EVAL_CASES 同步更新）。
  > §24 修复：① 纯 library inventory（ACTION）与 library reasoning（core/推荐/为什么 →
  > FACTUAL，要求 resolved+bound fulltext）分离，intent_eval 同步；② compare obligation
  > 来自本轮经服务端校验的 compare_papers.paper_ids 调用参数（loop/plan 在 context 保存
  > __args_<tool> 安全参数），工具错误/少于 2 目标/空结果/遗漏目标/单侧绑定均 abstain 并
  > 结构化披露；③ ACTION 区分成功 artifact 与工具错误：error 时输出确定性失败说明、保留
  > raw answer、记录 action_failure_mode，不再伪装 action_result 成功（合法空结果仍为成功）。
  > 既有拒答无引用逻辑本身通过；待 4.1-4.3 修正后一起最终验收，避免错误 capability 使本应拒答
  > 的请求根本不进入 abstention。
  > §25 修复：① `agent/validation.py` 统一 production schema 验证（required/type/additionalProperties
  > =false/未知工具），ChatAgentLoop 与 deterministic plan 均在 executor 前调用，无效参数不执行、
  > 不写 `__args_<tool>`、不形成 compare obligation（4 反向 + 1 正向 case）；② 空消息/含糊请求 →
  > clarified、破坏性请求 → blocked（capability 新增 BLOCKED/CLARIFY，不再折叠为 action_result）；
  > ③ ACTION 按 required/terminal tool 终态判定：终态成功（含被成功重试恢复的早期错误 → warning）
  > 为 action_result，未执行 required tool 或终态失败为 action_failed；search-add 以 add 终态为
  > 完成条件（RECOVERED-ERROR / REQUIRED-STEP-FAILED / SEARCH-ADD-TERMINAL-ADD case）。

## 5. Observability And Compatibility

> Codex 最终静态复审：Tasks 4.1-4.4 继续保持未勾选。批准前须完成内部指令 §25：
> 真实服务端工具参数校验、可达且分离的 clarify/blocked 状态、ACTION 终态失败与已恢复错误
> 的区分。事件 payload 持久化泄漏列为 Tasks 5.1/5.2 的首个 P1 门禁，不得再报告为已脱敏。
> `run-tasks4-final` 复审：clarify/blocked 已通过；其余仅剩 §26 两项收口。当前 validator
> 只是顶层 schema 近似检查，且 recovered-action 样例没有真正恢复 search。完成完整 schema
> validation 与 required/terminal action outcome 后再批准 Tasks 4.1-4.4。
> `run-tasks4-26c` 最终验收：完整 Draft 2020-12 schema validation、非 object 根参数保护、
> compare/range 约束、required/terminal action 与真实 recovery controls 均已闭合；129/129
> targeted 与 224/224 full regression 报告一致。Codex 批准 Tasks 4.1-4.4。未知工具名及完整
> arguments/context 经事件持久化的问题不属于 capability，转为 Tasks 5.1/5.2 首个 P1 门禁。
> §25.4 P1 门禁（5.1/5.2 首个 P1）：`final_answer_raw` 携带完整 context、`tool_call` 事件携带
> 完整 arguments，`__args_*` 并未跳过持久化。事件仅保存 allowlisted 摘要与关联 ID（request/
> project/run/tool），不得保存 prompt、question、papers payload、全文 evidence、密钥或内部
> `__args_*`；内存 policy context 与持久化 event payload 必须分离。

- [x] 5.1 增加 scope violation、citation resolution 和 evidence policy 结构化事件。
- [x] 5.2 日志贯穿 request/project/run/tool IDs，并确认无 prompt、正文或密钥泄漏。
- [x] 5.3 保留现有 API 路径、SSE 必需事件名和前端读取字段。
- [x] 5.4 标记旧 `verified` 字段 deprecated，确保任何门禁不再读取它。
  > `run-tasks5-fix` 首轮实现已有 136 targeted PASS，但 Codex 静态复审发现 token bypass、
  > message_preview/exception message、以及 Celery/workflow/API direct ProjectRunEvent producers
  > 尚未纳入统一门禁。Tasks 5.1-5.4 保持未勾选，先执行 GLM 独立审计指令，再由 DS 集中修复。
  > GLM 独立审计：20 case 为 8 PASS / 12 FAIL，复现 prompt/error/workflow 泄漏、producer bypass、
  > nested value passthrough、token IDs 缺失与前端 contract 断裂。按内部指令 §30 实施统一
  > EventPublisher、递归 event schema、安全异常边界和前端 safe view model；GLM 二次 PASS 前保持未勾选。
  > §30 修复后为 18 PASS / 2 旧规范冲突 FAIL；主体方向通过，但静态复审发现 regex-only exception
  > message 仍可泄漏 opaque 文本、MCP/RAG 尚有 logger.exception、llm_result 指标固定为 0。
  > 按内部指令 §31 收口后再交 GLM 二次独立复测；当前仍保持未勾选。
  > §31 主体通过；复测前仍发现 legacy realtime error DB、ChatLoop tool exception context、
  > MCP unknown-tool 与若干 eval artifact 的原始 exception 旁路。按内部指令 §32 一次性清理后
  > 立即交 GLM，后续不再由 DS 扩展自测范围。
  > §32 静态验收通过，DS 停止；GLM 按独立审计文档 §5 重跑修订后的 20 case 与扩展集。
  > 二次审计 PASS 前 Tasks 5.1-5.4 仍保持未勾选。
  > GLM 二次复测报告 26/26 PASS，泄漏、关联 ID 与前端 contract 指标通过；但 Codex 静态复审发现
  > `AUDIT-ALL-PRODUCERS-COVERAGE` 将 `agent/project_workflow.py (_event)` 无条件标为 runtime observed，
  > 且五个 producer 复用同一事件并集作为 evidence。按内部指令 §34 仅修测试/聚合器/报告并生成
  > 独立逐 producer 运行证据；真实 5/5 runtime coverage 复验前 5.1-5.4 继续保持未勾选。
  > `glm-tasks5-reaudit-v2` 最终验收：26/26 审计 case PASS；五个 producer 使用独立 run_id/row_ids，
  > LangGraph `_event` 由真实 workflow node 产生 `workflow_node`；declared-set、ID schema、零泄漏、
  > 前端 contract 和 PostgreSQL manifest 均与机器产物一致。Codex 批准 Tasks 5.1-5.4，允许进入
  > Tasks 6.x 验证阶段；v1 报告保留为勘误历史，v2 为权威验收产物。

## 6. Verification

- [x] 6.1 运行 targeted scope/citation/policy tests，记录发现数和结果。
- [x] 6.2 在 Docker PostgreSQL 下运行完整后端回归，要求 100% 通过。
- [x] 6.3 执行跨项目真实数据库 canary，确认泄漏率为 0%。
- [x] 6.4 运行 MCP schema compatibility 和全部 project tool dispatch tests。
- [x] 6.5 运行前端 Vitest/build，确认兼容字段没有破坏 UI。
- [x] 6.6 扫描日志和评测产物，确认密钥、完整 prompt、全文泄漏数为 0。
- [x] 6.7 固定机器可读 artifact，自动核对报告字段与 JSON 一致。
  > Tasks 6.7 同时将 `per_producer_independent_evidence` 从聚合器常量改为根据 run_id 唯一性、row_ids
  > 非空且互不重叠自动计算；当前 v2 原始数据已满足该条件，此项为防止未来评测回归，不阻断 Tasks 5.x。
  > DS `tasks6-final` 原始测试结果看似全绿，但 Codex 静态复审发现 verifier 对 backend/frontend 使用
  > 无条件 `True` 和固定数字，报告一致性仅搜索通用数字子串，canary category 也为常量。按内部指令
  > §36 改为解析 raw output、结构化字段比较、动态 canary inventory，并增加 mutation fail-closed 测试；
  > 修正前 Tasks 6.1-6.7 均不批准，也不交 GLM。
  > §36 主体已完成；Codex 最后静态边界见 §37：Vitest 必须比较 passed/total，Vitest/build exit 文件
  > 必须存在且为 0，summary block 必须唯一。该小包通过后直接交 GLM，不再扩展 DS 验证范围。
  > §37 静态复审通过，DS 阶段结束。mutation tests 的固定 raw output 未随产物保存，由 GLM 按内部
  > 指令 §38 独立重跑并留证；GLM 最终 PASS 前 Tasks 6.1-6.7 继续保持未勾选。
  > GLM 最终独立验收 PASS：12/12 mutation、全部后端/前端 suite、动态 canary、泄漏扫描、producer
  > runtime 与结构化报告核对均通过。Codex 批准 Tasks 6.1-6.7，允许进入 Tasks 7.x 归档门禁。

## 7. Archive Gate

- [x] 7.1 使用“已实现/原始证据/完整测试/仍未完成”四段式提交结果。
- [x] 7.2 用户确认所有 P0 scope/evidence gate 通过。
- [x] 7.3 合并 capability spec 并归档 change；随后才允许开始 PDF 自动入库 change。
  > 用户已确认 P0 scope/evidence gate；Codex 将 `project-scope-and-evidence` 合入当前 specs，更新
  > `agent-harness-and-mcp` 的 scope/quality/MCP 与脱敏契约，并完成结构、Requirement 映射和密钥扫描。
