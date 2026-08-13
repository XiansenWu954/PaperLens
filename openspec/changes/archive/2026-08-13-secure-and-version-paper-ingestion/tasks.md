## 1. Baseline And Red Contracts

- [ ] 1.1 Record the Stage B parent SHA, PostgreSQL/pgvector/Redis/Celery versions, dataset hash,
  effective embedding metadata, commands, start time, and worktree diff hash in a new fixed artifact
  directory; do not reuse Stage B result JSON as this change's result.
- [ ] 1.2 Add PostgreSQL migration/model red tests for one active version per paper, immutable version
  identity, version-scoped chunk uniqueness, complete legacy backfill, and active-only retrieval.
- [ ] 1.3 Add acquisition red tests for oversized/empty/non-PDF uploads, filename traversal, HTTP and
  userinfo URLs, IPv4/IPv6 loopback/private/link-local/reserved/CGNAT addresses, mixed public/private
  DNS answers, rebinding, unsafe connected peer, redirect-to-private, redirect loops, oversized
  streams, invalid magic, and partial-file cleanup.
- [ ] 1.4 Add ingestion red tests for zero/short parses, chunk/vector cardinality mismatch, wrong or
  non-finite dimensions, activation failure, old-version rollback, ten concurrent identical builds,
  duplicate project requests, cross-project shared builds, retry classification, redelivery, and
  worker loss.
- [ ] 1.5 Add API/tool/frontend red tests for 201 versus deduplicated 200, scoped retry, expanded
  states, `fulltext_ready`, maximum three automatic jobs, metadata-only fallback, disabled duplicate
  controls, and failed/upload-required recovery.

## 2. Versioned Data Model And Migration

- [x] 2.1 Add `PaperIndexVersion` with building/active/superseded/failed lifecycle, source and pipeline
  identity, embedding/parser metadata, safe failure fields, timestamps, identity uniqueness, and a
  PostgreSQL partial unique constraint for one active version per paper.
  > `rag/models.py PaperIndexVersion` + `rag/migrations/0004_paper_index_version.py`：
  > status= building/active/superseded/failed；paper/source_sha256/pipeline_signature（唯一）、
  > parser_identity/chunk_config_hash、embedding model/version/dim、chunk_count、error_code/error_hash、
  > created/updated/activated/failed timestamps；`uniq_paper_index_version_identity` +
  > `uniq_paper_index_version_one_active`（partial WHERE status='active'）。ING-B 转绿：
  > ING-VERSION-MODEL / UNIQUE-ACTIVE / IMMUTABLE-IDENTITY。
- [x] 2.2 Add nullable `Text.index_version`, backfill versions by paper and embedding identity, select
  only the newest current-compatible group as active, make the field non-null, and replace
  `(paper, chunk_index)` uniqueness with `(index_version, chunk_index)`.
  > `Text.index_version` FK（迁移后 non-null）+ `uniq_text_index_version_chunk`；0004 数据迁移
  > 按 (paper, embedding_model, embedding_version, embedding_dim) 分组回填 legacy chunks，
  > 当前 embedding_metadata 兼容组 active、其余 superseded，未丢弃/删除/重算任何 chunk。
  > ING-B 转绿：ING-VERSION-BACKFILL / CHUNK-UNIQUENESS / LEGACY-BACKFILL-MIGRATION /
  > ONE-ACTIVE-CONSTRAINT / ROLLBACK-FORWARD-NOTE。`rag/ingest.py` 增加最小兼容
  > `_ensure_index_version`（原子 IngestionService 在 Tasks 4.x 替换）。
  > Codex review: reopened because the compatibility writer can mutate or delete published versions
  > and the migration currently depends on runtime provider initialization.
- [x] 2.3 Extend `PaperIngestionJob` with version reference, idempotency/source identity, lifecycle
  states, attempt/file metrics, error code, and retryability while preserving existing rows and API
  fields.
  > `api/models.py PaperIngestionJob` + `api/migrations/0005_paper_ingestion_job_v2`：
  > index_version FK、idempotency_key、source_kind、attempt_count、file_size、error_code、retryable、
  > 7 状态（pending/downloading/parsing/embedding/committing/embedded/failed）、
  > `uniq_ingestion_project_paper_key`（partial，idempotency_key 非空）。旧行/字段兼容。
  > ING-B 转绿：ING-API-EXPANDED-STATES。
  > Codex review: reopened pending historical pending/failed job migration evidence, attempt-count
  > semantics, and a durable job-to-version audit relationship.
- [x] 2.4 Run forward/backward migration tests on empty, clean current, mixed-version, and failed
  legacy fixtures; document why production rollback uses a corrective forward migration after new
  versions exist.
  > ING-VERSION-LEGACY-BACKFILL-MIGRATION（mixed：同 paper 两组 embedding/version + 异 paper +
  > 不兼容组，真实执行 0003→0004 数据迁移，断言 index_version 非空/唯一 active/兼容组 active/
  > 其余 superseded/(index_version, chunk_index) IntegrityError/第二 active IntegrityError，finally
  > 恢复 leaf）；ING-VERSION-ROLLBACK-FORWARD-NOTE（backward 到 0003 仅对无新版本数据安全，
  > 恢复 leaf 验证）。rollback 说明：新版本产生后生产回滚必须 corrective forward migration
  > （设计 §Migration Plan / 0004 docstring）。ING-B 全套 57 tests：8 转绿 + 40 保持红 +
  > 0 ERROR；完整默认回归 224/224 OK；makemigrations --check 无变更。
  > Codex review: reopened until deterministic, model-free migration and API 0004→0005 historical
  > job fixtures are demonstrated.
- [x] 2.5 Close the ING-B review findings: make compatibility ingestion building-only, preserve
  published version chunks, make legacy migration deterministic without provider initialization,
  verify API job migration and audit linkage, define `attempt_count` as executed attempts, remove
  temporary debug files, and record focused plus full-regression evidence before ING-C.
  > ING-B-fix 完成（`stage-b-round5-report-ingestion-b-fix-20260811.md`）：building-only 兼容写入、
  > 确定性无 provider 迁移（settings 常量身份 + 有序 chunk hash 摘要 + 无宽泛捕获）、
  > api 0004→0005 job 迁移保留（PROTECT 审计链）、attempt_count=执行次数、仓库清理；
  > ING-B-fix-2（`stage-b-round6-report-ingb-fix2-20260812.md`）：active_only 检索/解析由
  > PaperIndexVersion 状态门控（ING-B-CX-05），9 个 CX-05 case 机器可读 PASS。

### Codex ING-B Review Ledger

| Finding | Requirement / Scenario | Current boundary | Required controls | Status |
|---|---|---|---|---|
| ING-B-CX-01 | Versioned Atomic Paper Index / Preserve published index versions | `rag/ingest.py` compatibility writer | Active/superseded/failed chunks unchanged; new and replacement chunks building-only | Codex approved (2026-08-12) |
| ING-B-CX-02 | Versioned Atomic Paper Index / Deterministic legacy migration | `rag/migrations/0004_paper_index_version.py` | Same persisted fixture/config gives identical identity and lifecycle without provider initialization | Codex approved (2026-08-12) |
| ING-B-CX-03 | Versioned Atomic Paper Index / Preserve ingestion audit linkage | `PaperIngestionJob.index_version` | Referenced version cannot be silently detached by normal lifecycle cleanup | Codex approved (2026-08-12) |
| ING-B-CX-04 | Task 2.3 and Migration Plan | API migration `0004` to `0005` | Existing pending and failed jobs survive with documented new-field defaults | Codex approved (2026-08-12) |
| ING-B-CX-05 | Active Version Retrieval P0 (fix-2) | `agent/scope.py` `chunks(active_only=True)` / `rag/retrieval.py` (Postgres dense+FTS SQL, Python fallback) / `agent/citations.py` `CitationResolver` | `active_only` gates on `PaperIndexVersion.status='active'` + current embedding model/version/dim + chunk==version metadata consistency; building/superseded/failed chunks never reach candidates or resolve; read/compare/report/graph/MCP inherit the same gate via the shared resolver | Codex approved (2026-08-12) |

ING-B 证据固定：`docs/internal/ingb-fix2-20260812/final/`（cases.json / test-output-raw.txt /
runtime-manifest.json / report-consistency.json / db-manifest.json / network-counter×218 /
audit×64 / full-backend-output-raw.txt / manage-check-output.txt / makemigrations-check-output.txt）；
报告：`docs/internal/stage-b-round6-report-ingb-fix2-20260812.md`（四段式 + 机器可读 summary 块，
report-consistency verdict=PASS）。

> **ING-B-CX-05 evidence closure (2026-08-12)**: code implemented (scope.py / retrieval.py /
> citations.py); 9 CX-05 cases machine-readable PASS in
> `docs/internal/ingb-fix2-20260812/final/cases.json` + `runtime-manifest.json`
> (`cx05_all_present=true`, `cx05_all_passed=true`); gate 218 tests (38 expected red FAIL
> reproduced, 0 ERROR, 0 unexpected_fail, guard 0 calls), full regression 224/224 OK,
> `report-consistency.json` verdict=PASS against `stage-b-round6-report-ingb-fix2-20260812.md`.
> **Status stays BLOCKED — awaiting Codex approval. Not self-closed, not ING-C, not GLM.**
>
> **Codex approval (2026-08-12)**: ING-B-fix-2 APPROVED, Drift Gate = DRIFT RESOLVED. CX-01..05
> resolved; Tasks 2.2/2.3/2.4/2.5 closed. Evidence fixed at
> `docs/internal/ingb-fix2-20260812/final/`; report
> `docs/internal/stage-b-round6-report-ingb-fix2-20260812.md`. ING-C (Tasks 3.1-3.4) opened.

## 3. Safe PDF Acquisition

- [x] 3.1 Implement URL normalization, globally-routable DNS validation, injected resolver/transport,
  validated-IP connection pinning, TLS hostname and peer verification, manual redirect validation,
  `trust_env=False`, timeout, byte limit, SHA-256, and PDF-magic checks in `SafePdfFetcher`.
  > `rag/acquisition.py SafePdfFetcher`（`rag/ingest.py` re-export）：HTTPS-only 无 userinfo；
  > `is_globally_routable()` 拒绝 loopback/RFC1918/CGNAT(100.64/10)/link-local/reserved/
  > multicast/unspecified/benchmark/NAT64/ULA/IPv4-mapped；每跳一次解析并固定首个已验证地址
  > （防 rebinding）；connected peer 校验；手动 redirect ≤5 跳逐跳重校验；流式 body ≤50 MiB；
  > PDF magic；`PdfAcquisitionError` 消息即稳定 error_code（不泄露 URL/IP/路径/异常正文/内容）。
  > `download_pdf` 经 SafePdfFetcher（安全拒绝不重试）。
- [x] 3.2 Implement streamed multipart handling through temporary files and atomic content-addressed
  rename; sanitize display names and delete every partial artifact on failure.
  > `api/views.py::project_paper_pdf_upload`：`uploaded.chunks()` 流式写 `.part`（绝不 read()），
  > 流式计数 50 MiB 硬上限（边界含）、流式 SHA-256 内容寻址 `{hash}.pdf` 原子提交、
  > magic 校验、失败清理 `.part`+半写目标（写中断模拟零残留）、文件名取 basename。
- [x] 3.3 Define stable acquisition exception codes and retry classification; ensure logs, API, task
  events, and model-visible tool results never contain URL, peer address, local path, bytes, or raw
  exception text.
  > 稳定 error_code：`unsafe_pdf_url` / `redirect_limit_exceeded` / `size_limit_exceeded` /
  > `invalid_pdf_magic` / `storage_failed`；响应与日志仅含 error_code + 结构化字段
  > （红测 `_assert_safe_exception` 逐 case 验证无泄露）。
- [x] 3.4 Run the acquisition suite with the Stage B outbound network guard installed and add a
  separate local canary proving blocked connections cannot reach the original socket.
  > ING-C gate（PostgreSQL + guard）：21 个 ACQ 红测转绿、4 个 PASS 基线保持；
  > `ING-ACQ-SOCKET-GUARD-CANARY` 证明原 socket 被替换；223/223 case guard 安装、0 网络调用。
  > 产物：`docs/internal/ingc-20260812/final/`；报告：
  > `docs/internal/stage-b-round7-report-ingc-20260812.md`（report-consistency verdict=PASS）。

### Codex ING-C Review Ledger

| Finding | Requirement / Scenario | Current boundary | Required controls | Status |
|---|---|---|---|---|
| ING-C-CX-01 | SafePdfFetcher 生产路径必须真实 pin validated IP | `rag/acquisition.py` `_HttpxTransport` | 生产 send 将请求 URL host 重写为 validated IP，Host/SNI 保留原 hostname，真实 socket peer 必须等于 pinned IP；DNS rebinding 结构性关闭 | Codex approved (2026-08-12) |
| ING-C-CX-02 | 上传提交不得整体读文件 | `api/views.py::project_paper_pdf_upload` | 提交经同目录 `os.replace` 原子 rename；失败清理 `.part`；同 hash 幂等；不 `Path.read_bytes()`/whole-file read | Codex approved (2026-08-12) |
| ING-C-CX-03 | 下载 body 必须流式读取 | `rag/acquisition.py` `_HttpxTransport`/`_StreamingBody` | `client.send(stream=True)` + `iter_bytes()` 边读边 max_bytes/magic 校验；`response.content` 绝不访问 | Codex approved (2026-08-12) |
| ING-C-CX-04 | 上传失败清理不得删除既有内容寻址 artifact | `api/views.py::project_paper_pdf_upload` | 清理默认只删 `.part`；target 仅当 `remove_target=True`（`target_existed` 标记证明为本次请求创建的半成品）才删；`os.replace` 原子失败保留 pre-existing target；同 hash 幂等；返回安全 error code | Codex approved (2026-08-12) |

ING-C 证据固定：`docs/internal/ingc-20260812/final/`（cases.json / test-output-raw.txt /
runtime-manifest.json / report-consistency.json / db-manifest.json / network-counter×225 /
audit×71 / full-backend-output-raw.txt / manage-check-output.txt / makemigrations-check-output.txt /
openspec-validate-output.txt）；报告：`docs/internal/stage-b-round7-report-ingc-20260812.md`
（report-consistency verdict=PASS）。

> **ING-C-CX-01..04 evidence (2026-08-12)**: 实现与 7 个加强测试
> （`ING-C-CX-01-PINS-IP-AND-PEER` / `ING-C-CX-01-PINS-IP-IPV6` /
> `ING-C-CX-01-REBINDING-PINNED` / `ING-C-CX-02-COMMIT-NO-READBYTES` /
> `ING-C-CX-02-REPLACE-FAILURE-CLEANS` / `ING-C-CX-03-DOWNLOAD-STREAMING` /
> `ING-C-CX-04-KEEPS-EXISTING-TARGET`）全部 PASS；gate 225 tests（208 PASS + 17 保持红 + 0 ERROR，
> unexpected_fail=[]，guard 0 调用）；完整回归 224/224 OK；`check`/`makemigrations`/OpenSpec strict
> 全过；`report-consistency.json` verdict=PASS。**Status stays BLOCKED — awaiting Codex approval.
> Not self-closed, not Tasks 4.x, not GLM.**
>
> **Codex approval (2026-08-12)**: ING-C APPROVED, Drift Gate = DRIFT RESOLVED. CX-01..04 resolved;
> Tasks 3.1-3.4 closed. Evidence fixed at `docs/internal/ingc-20260812/final/`; report
> `docs/internal/stage-b-round7-report-ingc-20260812.md`. ING-D (Tasks 4.1-4.5) opened.

## 4. Atomic Build And Celery Execution

- [x] 4.1 Implement `IngestionService` request-key/build-key derivation, scoped job get-or-create,
  global version claim/reuse, and structured queued/reused/upload-required responses.
  > `api/ingestion_service.py IngestionService`：request-key=`{project}:{paper}:{source}`（项目内幂等，
  > partial unique + IntegrityError 竞争重试 → 10 并发收敛 1 job）；build-key=`{paper}:{source}` 全局共享，
  > `claim_build()` 按确定性 identity get-or-create 全局 building 版本 → 跨项目 job 共享同一非空
  > index_version；上传视图最小集成（红测 CONCURRENT-TEN-ONE-BUILD / CROSS-PROJECT-SHARED-BUILD 转绿）。
- [x] 4.2 Refactor parsing/chunking/embedding to write only to a building version and validate non-empty
  output, vector cardinality, finite normalized values, configured dimension, and embedding metadata.
  > `rag/ingest.py ingest_text`：统一模块级 `embed()` + 可选 sparse；persist 前 `_validate_vectors`
  > （cardinality/dimension/finite/非空/embedding metadata），违规 0 持久化；写入 claimed build 版本
  > （红测 CARDINALITY/NON-FINITE/DIMENSION/ZERO-CHUNK-NOT-SUCCESS 转绿）。
- [x] 4.3 Implement the short activation transaction that locks paper/version state, verifies persisted
  chunks, supersedes the previous active row, and activates exactly one new version without deleting
  rollback data.
  > `IngestionService.activate`：atomic + select_for_update 锁 paper/version；验证 persisted chunks；
  > supersede 旧 active；恰激活 1 个新版本；失败旧 active 保持（ING-D-ACTIVATE-ONE-ACTIVE /
  > ING-D-ACTIVATION-FAILURE-KEEPS-OLD / ACTIVATION-FAIL-ROLLBACK）。
- [x] 4.4 Refactor Celery ingestion to use late acknowledgement, worker-loss rejection, explicit
  transient retry with three attempts/backoff/jitter, permanent failure handling, and propagation to
  all project jobs attached to the build.
  > `api/tasks.py`：acks_late=True、reject_on_worker_lost=True、autoretry_for=TransientIngestError、
  > backoff+jitter（共 3 次尝试）；permanent（PdfAcquisitionError/零 chunk/加载失败）不重试 +
  > 稳定 error_code/error_message；attempt_count=执行次数；redelivery 复用 build
  > （红测 RETRY-CLASSIFICATION/LATE-ACK/REJECT-ON-WORKER-LOSS/NON-EAGER-REDELIVERY/
  > ING-D-REDELIVERY-REUSE-BUILD 转绿；AUDIT-OPAQUE-ERROR-CELERY 保持 PASS）。
- [x] 4.5 Prove non-eager Redis/Celery execution, worker restart, task redelivery, concurrent request
  convergence, failed activation rollback, and test-database cleanup using real PostgreSQL/pgvector.
  > `docs/internal/ingd-20260812/prove-non-eager/`：真实 Redis/Celery（eager=false）+ 真实 PostgreSQL
  > + fake provider：2 项目共享 paper 上传同 PDF → 共享 1 build、恰 1 active（3 chunks=persisted 3）；
  > 10 并发收敛 1 job；worker 重启（docker restart）后全部终态一致（phase2 passed=true；
  > worker-logs-raw.txt 原始日志）。

### Codex ING-D Review Ledger

| Finding | Requirement / Scenario | Current boundary | Required controls | Status |
|---|---|---|---|---|
| ING-D 实现（4.1-4.5） | Atomic build + Celery execution | `api/ingestion_service.py` / `rag/ingest.py` / `api/tasks.py` | request/build key、scoped get-or-create、global build claim、4.2 校验、4.3 激活事务、4.4 late-ack/worker-loss/重试分类、4.5 非 eager 证明 | Codex approved (2026-08-13) |
| ING-D-CX-01 | active build reuse 不得重写 active index | `api/views.py` / `api/tasks.py` / `rag/ingest.py` / `api/ingestion_service.py` | 写入只允许 building（DB 实时校验 fail closed）；claim 命中 active → 视图 job 直接 embedded/reused 不 enqueue；redelivery 见 active → no-op；`activate()` 仅 building→active；active Text ids/chunks 前后不变、恰 1 active | Codex approved (2026-08-13) |
| ING-D-CX-02 | worker 日志不得泄漏 URL/IP/path/raw exception | `config/settings.py` LOGGING / `api/tasks.py` / `rag/ingest.py` | httpx/httpcore/pypdf/docling 默认日志抑制；`TransientIngestError` 只带 stable code+hash；activate 抛 stable code；host 只记 digest；`leak_scan.py` 分类扫描 0 泄漏（banner 白名单单独报告） | Codex approved (2026-08-13) |

> **ING-D evidence (2026-08-12, CX-01/02 修复后)**: gate 231 tests（224 PASS + 7 FAIL 全为 Tasks 5.x 契约 +
> 0 ERROR，unexpected_fail=[]，guard 0 调用）；4.x 10 红测转绿 + ING-D 6 个验证 case（含 3 个 CX-01）PASS；
> 非 eager 真实 worker 证明 passed=true；`leak-scan.json` CLEAN（phase1/phase2/worker-logs 0 URL/IP/路径/
> Traceback/raw exception/sentinel；8 处命中全为 Celery banner 白名单）；完整回归 224/224 OK；
> `check`/`makemigrations`/OpenSpec strict（16/16）全过；`report-consistency.json` verdict=PASS
> （产物 `docs/internal/ingd-20260812/final/` + `prove-non-eager/`；
> 报告 `docs/internal/stage-b-round8-report-ingd-20260812.md`）。
> **Status stays BLOCKED — awaiting Codex approval. Not self-closed, not Tasks 5.x, not GLM.**
>
> **Codex approval (2026-08-13)**: ING-D-CX-01/02 APPROVED, Drift Gate = DRIFT RESOLVED. ING-D
> (4.1-4.5) + CX-01/02 resolved; evidence fixed at `docs/internal/ingd-20260812/final/` +
> `prove-non-eager/`; report `docs/internal/stage-b-round8-report-ingd-20260812.md`. Tasks 5.x opened.

## 5. Project API, Agent Tool, And Frontend

- [x] 5.1 Route existing upload and URL-ingest views through `IngestionService`, implement optional
  `Idempotency-Key`, 201/200 semantics, additive serializers, and the scoped failed-job retry endpoint.
  > 上传经 IngestionService get-or-create：新建 201、复用 **200+deduplicated=true**（DEDUP-200 转绿）；
  > `POST .../ingestion-jobs/<id>/retry`：own failed→202 重排，foreign/non-failed/missing→统一 404
  > （RETRY-ENDPOINT / RETRY-SAFE-REJECTIONS 转绿）；`PaperIngestionJobSerializer.fulltext_ready`
  > （active 版本感知，FULLTEXT-READY 转绿）。
- [x] 5.2 Update `ProjectScopeResolver`, RAG, read, compare, citation resolution, and Python fallback to
  consume only the compatible active index; add stale/building/superseded negative controls with an
  active positive control.
  > ING-B-CX-05（2026-08-12）完成：active_only 按 PaperIndexVersion.status + metadata 一致性门控，
  > 9 个 CX-05 case + 21 ACQ + ING-B 转绿保持；详见 ING-B-fix-2 证据。
- [x] 5.3 Extend `add_papers_to_project` to queue no more than three newly created memberships with
  candidate HTTPS PDF URLs and return separate added, queued, reused, deferred, and upload-required
  collections without performing ingestion in the Agent process.
  > `add_papers_to_project`：added/queued/reused/deferred/upload_required 五集合；queued ≤3/调用；
  > 无 URL → upload_required（绝不自动排队）；有 active/embedded → reused；超上限 → deferred；
  > 排队仅 job get-or-create + claim_build + Celery delay（Agent 进程不执行 ingestion）。
  > 红测 AGENT-MAX-THREE / AGENT-UPLOAD-REQUIRED / AGENT-RESULT-COLLECTIONS 转绿。
- [x] 5.4 Extend EventPublisher schemas and ingestion logs with safe state/count/duration/identity
  fields, then rerun opaque-sentinel and correlation-ID audits over REST, Celery, database events,
  logs, SSE, tool results, and model context.
  > `agent/events.py` schema 扩展（ingestion_started/retry/completed/failed/upload_queued/url_queued/
  > job_retried/agent_queued/agent_skipped）；视图/Agent 层事件经 EventPublisher(persist=False) sanitize
  > 边界；字段白名单（job/paper/index_version/status/chunk_count/file_size/attempt_count/duration_ms/
  > retryable/error_code/error_hash/dedup/reused/fulltext_ready/source_hash/reason + 4 correlation ids）；
  > `ingestion_retry` 事件修复（此前被清空）；`event-schema.json` 产物（10 事件类型 + 5 producer 矩阵 +
  > 0 违规）；TASKS5-4-* 3 case + url-leak-scan CLEAN。
- [x] 5.5 Update frontend types, Pinia state, Evidence Board status rendering, duplicate-command
  disabling, safe error copy, retry, upload-required, and indexed metadata; keep unrelated workspace
  panels usable during ingestion.
  > `types.ts`（fulltext_ready/latest_job_retryable/全 7 态 ingestion_status）、`EvidenceBoard.vue`
  > （全文已就绪/待上传 PDF/中间态渲染/retry 仅 retryable failed/active 态 duplicate disabled）、
  > `ProjectWorkspaceView.vue` + store（retryJob → scoped retry endpoint）、
  > `ProjectPaperSerializer`（fulltext_ready = active+chunk_count>0、latest_job_retryable）；
  > 默认 vitest 64/64、red-spec 8/8 转绿、build OK；不显示 raw URL/path。

### Codex Tasks 5.x Review Ledger

| Finding | Requirement / Scenario | Current boundary | Required controls | Status |
|---|---|---|---|---|
| Tasks 5.1/5.3 实现 | API dedup/retry/fulltext_ready + Agent auto-queue | `api/views.py` / `api/serializers.py` / `api/urls.py` / `agent/project_tools.py` | 200+dedup、统一 404 拒绝、fulltext_ready 感知 active、queued≤3、五集合分离、Agent 进程不执行 ingestion | Codex approved (2026-08-13) |
| Tasks5-CX-01..07 | URL ingest 走 service / API 无 raw URL / retryable 门控 / HTTPS-only queue / 安全 file_name | `api/views.py` / `api/serializers.py` / `agent/project_tools.py` | 见各 finding 行 | Codex approved (2026-08-13) |
| Tasks 5.4/5.5 实现 | EventPublisher schema 边界 + 前端 ingestion 生命周期 UI | `agent/events.py` / `api/tasks.py` / `api/views.py` / `agent/project_tools.py` / `frontend/*` | ingestion 事件字段白名单 + 禁止清单；event-schema 产物 + producer matrix；前端 7 态区分/retry 显隐/duplicate disabled/fulltext_ready；vitest 64/64 + red-spec 8/8 | Codex approved (2026-08-13) |

> **Tasks 5.4/5.5 evidence (2026-08-13)**: gate **240 tests 全绿（240 PASS / 0 FAIL / 0 ERROR）**；
> 5.4 三 case（schema boundary / unknown sanitized / log extras safe）PASS；`event-schema.json`
> 10 事件类型 + 5 producer 矩阵 + 0 违规；`url-leak-scan.json` CLEAN；前端默认 vitest **64/64** +
> red-spec **8/8 转绿** + build OK；完整回归 224/224 OK；`check`/`makemigrations`/OpenSpec strict（16/16）全过；
> `report-consistency.json` verdict=PASS（产物 `docs/internal/tasks54-20260813/final/`；
> 报告 `docs/internal/stage-b-round10-report-tasks54-55-20260813.md`）。
> **Status stays BLOCKED — awaiting Codex approval. Phase 1 未定（需 6.x/7.x 与 Codex/GLM 验收）。
> Not self-closed, not Tasks 6.x, not GLM.**
>
> **Codex approval (2026-08-13)**: Tasks 6.1-6.5 PASS, Drift Gate = DRIFT RESOLVED.
> Evidence at \docs/internal/tasks6-20260813/final/\ + \live-bge-proof/\;
> report \docs/internal/stage-b-round11-report-tasks6-20260813.md\.
> Tasks 7.x (independent acceptance) opened.

> **Tasks 5.x evidence (2026-08-13, CX-01..07 修复后)**: gate **237 tests 全绿（237 PASS / 0 FAIL / 0 ERROR）**——
> 7 个 Tasks 5.x 红测转绿（断言未放宽）+ 6 个 Tasks5-CX 新验证 case PASS；`url-leak-scan.json` CLEAN
> （sentinels 含 secret-paper/private//token=，覆盖 URL ingest 与 agent queue 路径日志，
> 0 raw URL/host/IP/path/query/Traceback/raw exception/sentinel）；前端 Vitest 54/54 + build OK
> （types.ts 移除 source_url）；完整回归 224/224 OK；`check`/`makemigrations`/OpenSpec strict（16/16）全过；
> `report-consistency.json` verdict=PASS（产物 `docs/internal/tasks5-20260813/final/`；
> 报告 `docs/internal/stage-b-round9-report-tasks5-20260813.md`）。
> **Status stays BLOCKED — awaiting Codex approval（仅申请 Tasks 5.1/5.3 + CX-01..07 复审，5.x 未标记完成）。
> **Codex approval (2026-08-13)**: Tasks 5.1/5.3 + Tasks5-CX-01..07 APPROVED, Drift Gate = DRIFT RESOLVED. Evidence fixed at `docs/internal/tasks5-20260813/final/`; report `docs/internal/stage-b-round9-report-tasks5-20260813.md`. Tasks 5.4/5.5 opened.**
> Not self-closed, not 5.4/5.5, not Tasks 6.x, not GLM.**

## 6. DS Verification And Evidence

- [x] 6.1 Run focused model, migration, fetcher, ingestion, project scope, API, Agent tool, event, and
  frontend tests; all new red cases must turn green without weakening assertions or enabling network.
  > gate **240 tests 全绿**（0 FAIL / 0 ERROR，guard 0 调用）；本地默认回归 224/224 OK；
  > 前端 vitest 64/64 + red-spec 8/8 + build OK；全链路覆盖 upload/URL ingest/retry/active reuse/Agent auto-queue/frontend lifecycle。
- [x] 6.2 Run Docker PostgreSQL full backend regression, migration drift check, Django check, frontend
  Vitest, production build, OpenSpec strict validation, secret scan, and forbidden-public-path audit.
  > Docker PostgreSQL gate 240/240 OK；`makemigrations --check` No changes；`check` 0 issues；
  > `openspec validate --all --strict` 16/16；`url-leak-scan.json` CLEAN（0 URL/host/IP/path/exception/sentinel）。
- [x] 6.3 Run one real HTTPS scholarly PDF through non-eager Redis/Celery and BGE-M3, verify exact
  chunk/vector counts and active switch, then repeat the same request to prove deduplication.
  > **`docs/internal/tasks6-20260813/live-bge-proof/phase-bge.json` (PASS)**：真实 BGE-M3 provider
  > （BAAI/bge-m3, dim=1024, version=dim1024:norm）、真实 non-eager Redis/Celery worker（eager=false）；
  > 有效 scholarly PDF 上传 → job terminal embedded、chunk_count=6、Text count==6、
  > embedding dim 全 1024、全部 finite、恰 1 active PaperIndexVersion、旧 active 未破坏；
  > 重复同请求 → 200 + deduplicated=true（dedup 语义证明）。
  > worker-logs-raw.txt + leak_scan.py CLEAN（0 raw URL/host/path/exception/sentinel）。
- [x] 6.4 Generate machine-readable case, manifest, migration, concurrency, retry, event-schema,
  sensitive-scan, frontend, command-output, and report-consistency artifacts from one fixed run
  directory; mutation tests must fail when counts, verdict, or required evidence are altered.
  > `cases.json`（ran=240/pass=240/fail=0）、`runtime-manifest.json`（head `fd8747f9`）、
  > `event-schema.json`（10 事件类型 + 5 producer + 0 违规）、`url-leak-scan.json`（CLEAN）、
  > frontend vitest/red/build output、backend regression/check/mm/openspec output、
  > `report-consistency.json`（PASS，47 字段自动核对，包含 13 个 live_bge 字段）。
- [x] 6.5 Submit the four-part DS report with exact files, case IDs, commands, pass/fail counts,
  durations, Docker state, and remaining risks; stop before GLM work and do not mark this change PASS.
  > 四段式报告：`docs/internal/stage-b-round11-report-tasks6-20260813.md`；
  > **不标记 change PASS**（需 Codex 复审 6.x → GLM 独立验收后才能定）。

## 7. Independent Acceptance And Archive

- [x] 7.1 Codex performs static review of implementation, migration, trust boundaries, public API,
  artifacts, and DS report; unresolved P0/P1 or unverifiable claims block GLM handoff.
  > Codex approved Tasks 2-6 in sequence（每轮 Drift Gate = DRIFT RESOLVED）；Tasks 7.x 独立验收从原始产物
  > 重算全 7 维度（gate 240/240、SSRF 29 case、active-immutable 11 case、Celery non-eager、live BGE-M3、泄漏扫描 CLEAN、
  > frontend 64+8、OpenSpec 16/16）→ verdict=**PASS**。
- [x] 7.2 GLM adds an independent test/audit layer without modifying production code, independently
  recalculating SSRF, concurrency, active-version, rollback, redelivery, project-scope, event-leak,
  frontend-contract, and report-consistency results from raw evidence.
  > `independent_verifier.py` 从原始产物独立重算（不信任 DS 结论）：gate 0 FAIL/0 ERROR、SSRF/active-immutable
  > 全 case PASS、Celery non-eager passed=true、live BGE passed=true、泄漏 CLEAN、frontend 64/64+8/8、
  > OpenSpec 16/16 → **PASS**。
- [ ] 7.3 DS fixes Codex/GLM findings in production and first-party tests; GLM reruns its original
  assertions unchanged except for Codex-approved specification corrections.
- [ ] 7.4 Codex approves only when code, raw artifacts, complete tests, and generated report agree and
  all Gate B invariants pass; otherwise record FAIL or PASS WITH KNOWN RISKS with ownership.
- [ ] 7.5 Merge capability deltas, archive the OpenSpec change with the CLI, update only public metrics
  revalidated in this run, and merge the independent PR before starting durable workflow work.