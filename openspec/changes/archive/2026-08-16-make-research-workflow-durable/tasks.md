## 1. Codex Specification Gate

- [x] 1.1 Inspect the merged Phase 1 baseline, current workflow, Celery ownership, ProjectRun schema,
  EventPublisher contracts, frontend run inspector and current capability specs.
- [x] 1.2 Freeze proposal, design and deltas for `agent-orchestration`, `project-workspace` and
  `production-hybrid-rag`, including compatibility, rollback and measurable release gates.
- [x] 1.3 Replace the stale hardcoded public OpenSpec count with a count-independent strict-validation
  statement and run `openspec validate --all --strict`.
- [x] 1.4 Commit the approved OpenSpec-only baseline and issue the bounded DS Batch A handoff; no
  production implementation is authorized before this task is complete.

## 2. Batch A — Red Tests And Reproducible Baseline

- [x] 2.1 Add explicit non-default red tests proving the current graph performs RAG before ingestion
  terminal state, has no persistent checkpoint/wait boundary and directly creates ingestion jobs.
- [x] 2.2 Add red tests for duplicate start/resume/report/event side effects, owner lease conflict and
  expiry, lost wakeup, disabled/unready checkpointer and stable fail-closed API errors.
- [x] 2.3 Add red tests for dependency scope and lifecycle: ready active evidence, pending ingestion,
  partial failure, all failure, unavailable URL, excluded/unlinked/foreign paper and terminal times.
- [x] 2.4 Add red tests scanning LangGraph checkpoint/pending-write tables, ProjectRun private state,
  events, logs, API and Celery results for question, URL, path, full text, excerpt, key and opaque
  exception sentinels; include non-empty positive and negative controls.
- [x] 2.5 Add red tests for strict `first_rag_at > last_ingestion_terminal_at`, one report per run,
  one committed RAG event and no report when full-text citations are unresolved or unbound.
- [x] 2.6 Produce one fixed PostgreSQL/pgvector manifest, per-case JSON, raw output, network guard,
  secret scan and report-consistency artifacts; distinguish expected pre-fix failures from errors,
  vacuous passes and unexpected results.
- [x] 2.7 Submit the four-part DS Batch A report and stop. Do not modify production code, current
  specs, GLM assertions or task checkboxes beyond 2.x before Codex static review.

### Codex Batch A Review Note (2026-08-13)

- Batch A v3.1 is accepted only as the entry baseline for Batch B's data and checkpoint foundation.
  It proves the current premature-RAG defect, missing checkpoint foundation, direct ingestion bypass,
  and missing durable data contracts. It does not close the later concurrency, reconciliation,
  partial-result, citation/report, or fault-recovery behavior gates.
- Batch B is authorized to implement Tasks 3.1-3.6. Before each later production batch starts, DS
  MUST first add executable red tests for the behavior owned by that batch: ownership and duplicate
  resume before Batch C; wakeup reconciliation, timing, partial results and report gates before Batch
  D; real concurrency and worker-failure evidence before Batch E.
- Tests that only inspect a field, task name or source string MUST NOT be reported as proof of runtime
  behavior. Pure gate helpers are test scaffolding until the production path calls the same policy or
  an integration test feeds production results into the gate.
- Every DS handoff MUST use exactly four top-level sections in this order: `Implemented`, `Raw
  Evidence`, `Complete Tests`, and `Remaining Work`. Tables are limited to compact numeric summaries;
  behavioral claims, defects, file paths, causality and deferred scope must be written as prose or
  bullets. Machine-readable JSON and raw command output remain authoritative over tables.

## 3. Batch B — Durable Data And Checkpoint Foundation

- [x] 3.1 Pin the approved LangGraph/PostgreSQL saver and psycopg pool dependency ranges; add an
  idempotent `setup_langgraph_checkpoints` command and backend startup/readiness integration while
  proving Celery workers never execute checkpoint DDL.
- [x] 3.2 Add ProjectRun workflow lifecycle, lease, safe private state and timestamps; migrate existing
  rows without changing their existing status or public behavior.
- [x] 3.3 Add ProjectWorkflowDependency with run/paper uniqueness, project consistency, optional job,
  terminal timestamp and stable error code; add PaperIngestionJob terminal timestamp.
- [x] 3.4 Add nullable unique workflow ownership on ReportVersion and nullable per-run event dedupe key;
  prove ordinary reports and legacy events remain compatible.
- [x] 3.5 Extend serializers, health output, settings and safe EventPublisher schemas with additive
  fields only; owner tokens, private state and checkpoint internals must remain absent.
- [x] 3.6 Run migration forward/backward safety, fresh PostgreSQL checkpoint setup twice, model
  constraints, health, focused API and complete backend regression; submit the four-part report and
  stop for Codex review.

### Codex Batch B Review Ledger (2026-08-13)

- **P2-B-CX-01 — BLOCKED:** `ProjectRun` is missing the approved `started_at`, `waiting_at` and
  `completed_at` lifecycle timestamps. Add them as nullable fields and prove legacy-row compatibility
  and forward/backward migration behavior.
- **P2-B-CX-02 — BLOCKED:** checkpoint setup is not integrated into backend startup and health does
  not report `durable_workflow_enabled` plus `workflow_checkpointer_ready`. Backend startup MUST run
  the idempotent setup after Django migrations; Celery workers MUST remain DDL-free. Readiness MUST be
  derived from the effective configured database/checkpoint store without exposing credentials.
- **P2-B-CX-03 — BLOCKED:** `ProjectRunSerializer` does not expose the approved safe additive workflow
  phase, resume count, lifecycle timestamps, dependency summary and report ID. Private owner, lease,
  draft and checkpoint fields MUST remain absent. Add HTTP-response tests, not model-only assertions.
- **P2-B-CX-04 — BLOCKED:** dependency and report ownership rows do not yet enforce project
  consistency. A dependency MUST reject a paper that is foreign, excluded or unlinked to the run's
  project, and a workflow report MUST reject a `source_run` from another project. Add non-empty own
  controls and foreign/excluded/unlinked negative controls through the approved creation boundary.
- **P2-B-CX-05 — BLOCKED:** Batch B evidence is incomplete. The fixed artifact directory MUST include
  focused raw output, full regression output, migration forward/backward output, setup-twice output,
  health/API output, machine-readable runtime/checkpoint manifest, secret scan, report-consistency
  result and a UTF-8 four-section report. Report consistency MUST compare a unique machine-readable
  Markdown summary block with JSON; it MUST NOT rely on manual prose or a table.

Batch C is not authorized until P2-B-CX-01 through P2-B-CX-05 are resolved and Tasks 3.1-3.6 receive
Codex approval. Runtime graph, interrupt/resume, owner acquisition and reconciliation remain forbidden
in this repair package.

### Codex Batch B CX Re-review (2026-08-13)

- **P2-B-CX-01 — RESOLVED:** nullable lifecycle timestamps, additive migration and forward/backward
  migration coverage are present.
- **P2-B-CX-02 — BLOCKED:** safe checkpoint connection handling, startup ordering and fail-closed
  readiness are present, but the approved explicit deployment switch is absent from both
  `.env.example` and the backend Docker Compose environment. Add
  `PAPERLENS_DURABLE_WORKFLOW_ENABLED=1` to the PostgreSQL demo/deployment path while preserving the
  explicit `0` rollback and SQLite-default-off behavior.
- **P2-B-CX-03 — RESOLVED:** the safe additive HTTP response fields are present and private owner,
  lease and draft fields remain excluded.
- **P2-B-CX-04 — BLOCKED:** `api.workflow_data` declares itself the mandatory workflow report creation
  boundary, but `agent.project_workflow._save_report` still calls `ReportVersion.objects.create`
  directly and omits `source_run`. Route the production workflow report write through the boundary
  and add a production-path positive control plus project-mismatch and duplicate negative controls.
  Ordinary non-workflow report creation remains out of scope.
- **P2-B-CX-05 — BLOCKED:** `verify_all.py` hardcodes every case `actual` as `PASS`, treats the mere
  presence of `OK` as a suite verdict and never parses or compares the Markdown report's required
  unique machine-readable summary block. Replace manual verdict construction with fail-closed raw
  output parsing, compare the report block field-by-field with JSON, remove stale temporary artifacts,
  and add mutations for suite failure/error, Markdown summary, git/diff provenance and migration
  forward/backward evidence.

**Drift verdict: `BLOCKED`.** The durable data/checkpoint architecture remains aligned with the
approved change, but Batch B cannot be completed while a production workflow bypasses the declared
data boundary and the evidence verifier can report a false PASS. The next package is limited to the
three blocked findings above; Batch C runtime graph work remains unauthorized.

### Codex Batch B Final Re-review (2026-08-13)

- **P2-B-CX-02 — RESOLVED:** the PostgreSQL demo/deployment path now explicitly enables the durable
  workflow flag in `.env.example` and Docker Compose while preserving explicit rollback and
  SQLite-default-off behavior.
- **P2-B-CX-04 — RESOLVED:** the existing production workflow report path now validates run/project
  identity, routes through `create_workflow_report`, records `source_run` and converges duplicate
  persistence on one report. Ordinary report creation remains unaffected.
- **P2-B-CX-05 — BLOCKED:** focused and full raw outputs parse successfully and the current Markdown
  summary matches the eight fields that `finalize.py` checks, but the evidence contract remains
  incomplete. The summary contains thirteen authoritative fields while five are not compared;
  mutations do not alter, duplicate or remove the Markdown summary block; no captured process exit
  code is verified despite the report claiming non-zero exits fail closed; and the generated case
  records are suite-derived rather than parsed per named focused case. Add exit-code artifacts,
  compare every summary field, generate named focused-case results from verbose raw output, and add
  the required Markdown-block and diff-provenance mutations.

**Drift verdict: `BLOCKED`.** Production structure is now aligned and no new architectural drift was
found. Batch C remains blocked only on the reproducibility/evidence gap in P2-B-CX-05. The next repair
MUST modify evidence scripts, raw artifacts and the internal report only; production code and OpenSpec
requirements are frozen.

### Codex Batch B Evidence Final Review (2026-08-13)

- **P2-B-CX-05 — BLOCKED:** command exit files, fail-closed suite parsing, Markdown mutations and
  clean-state restoration are now present. However, the focused raw output contains 44 named tests
  while `parse_per_case` captures only 11 because Django `-v2` places docstrings, warnings or the
  terminal `ok` on following lines. `cases.json` consequently contains fourteen aggregate gates rather
  than the claimed named focused cases. In addition, the verifier rewrites the runtime manifest while
  verifying it, skips the `mutation_suite` field in the claimed thirteen-field Markdown comparison,
  and treats typed health booleans as passing without requiring enabled, ready and available to be
  true for the live PostgreSQL evidence.

The final evidence-only correction MUST parse all 44 unique named test records and require their count
to equal `Ran 44 tests`; store named test cases separately from aggregate gates; keep verification
read-only; include the mutation result in the final thirteen-field comparison; and require all three
live health values to be true. Add parser controls for inline, multiline/docstring, intervening-log,
FAIL and ERROR output. No production code, migration, dependency, OpenSpec requirement or runtime
workflow change is authorized.

**Drift verdict: `BLOCKED`.** This is the last Batch B blocker. When the corrected artifacts prove
44/44 named cases, all thirteen report fields and a read-only clean-state PASS, Codex will approve
Tasks 3.1-3.6 and authorize Batch C without another production-code review cycle.

### Codex Batch B Evidence Closeout Review (2026-08-13)

- The corrected parser and read-only verifier independently reproduce 44/44 named focused cases,
  268/268 full regression, 13/13 Markdown fields, 8/8 parser controls, unchanged manifest hashes and
  strict OpenSpec 15/15. The evidence logic is accepted.
- **P2-B-CX-05 — P2 CLEANUP REQUIRED:** the fixed artifact directory still contains a stale
  `verifier-output.txt` with a `UnicodeDecodeError` traceback rather than the final PASS output, while
  `sensitive-scan.json` reports CLEAN because the scanner does not classify generic `Traceback` or
  `UnicodeDecodeError`. Replace that output from the clean read-only verifier run, extend the internal
  artifact scanner to reject unexpected traceback/error output, rerun scan and consistency, and
  record the final input/output hashes. No new implementation report or production test rerun is
  required.

**Drift verdict: `BLOCKED`.** There is no production or architectural blocker. Batch C authorization
is held only until the stale contradictory artifact is removed and the clean scan is regenerated;
after that Codex will approve Tasks 3.1-3.6 directly.

### Codex Batch B Approval And Batch C Gate (2026-08-13)

- Batch A Tasks 2.1-2.7 and Batch B Tasks 3.1-3.6 are approved. The fixed evidence independently
  reproduces 44/44 named focused tests, 268/268 complete regression, 8/8 parser controls, 13/13 report
  fields, read-only manifest comparison, clean artifact scan and strict OpenSpec 15/15.
- Safe EventPublisher schema additions for newly introduced runtime lifecycle transitions are
  intentionally completed with Batch C Task 4.5 when those producers exist; Batch B approval covers
  the additive serializer, settings, readiness and existing safe schema compatibility foundation.
- **P2 evidence packaging debt:** `artifact-manifest.json` currently includes its own prior hash and
  therefore cannot self-verify, and its stored `verifier-output.txt` hash predates the final captured
  output. The manifest is not an authoritative Batch B gate. Before Batch E/GLM acceptance, exclude
  the manifest itself (or use a canonical detached manifest), regenerate it after all outputs, and
  require an independent hash verification PASS. This P2 does not invalidate the raw outputs or the
  independently rerun verifier and does not block Batch C.

**Drift verdict: `DRIFT RESOLVED`.** The durable data/checkpoint foundation matches the approved
proposal, Stage B and Phase 1 invariants remain intact, and Batch C Tasks 4.1-4.7 are authorized.
Batch D reconciliation, terminal callbacks, partial-result policy and frontend work remain forbidden
until a separate Codex review.

## 4. Batch C — Checkpointed Graph And Idempotent Ownership

- [x] 4.1 Compile the fixed workflow graph with PostgreSQL checkpoint persistence and
  `thread_id=str(ProjectRun.id)`; checkpoint state must contain only approved scalar/ID fields.
- [x] 4.2 Replace direct workflow job creation with Phase 1 IngestionService and create explicit
  dependency rows for ready, pending and unavailable papers.
- [x] 4.3 Implement `await_ingestion` using LangGraph interrupt, releasing the owner and worker after a
  deduplicated waiting transition; resume must use the same thread identity.
- [x] 4.4 Implement database owner acquisition/renewal/release with row locking and a 300-second lease;
  configure start/resume tasks with late acknowledgement and worker-loss rejection; valid-owner
  duplicates exit safely and expired owners resume from checkpoint.
- [x] 4.5 Make node transitions, committed RAG identity and report persistence idempotent using event
  dedupe keys and ReportVersion source-run uniqueness.
- [x] 4.6 Enforce the durable-workflow feature flag and checkpointer readiness at the existing endpoint;
  unavailable mode returns stable `workflow_unavailable` and never invokes the legacy graph.
- [x] 4.7 Turn the corresponding Batch A tests green, run focused and full regression, preserve Stage B
  and Phase 1 invariants, submit the four-part report and stop for Codex review.

### Codex Batch C Static Review (2026-08-13)

- **P2-C-CX-01 - BLOCKED (P0):** workflow ingestion does not preserve run causality or the Phase 1
  convergence boundary. `search_sources` calls `add_papers_to_project`, which can already enqueue
  ingestion, and `_enqueue_ingestion_for_run` then scans every non-excluded project paper, uses a
  different run-scoped idempotency key, derives `file_name` from the raw URL path, omits
  `IngestionService.claim_build`, and treats any active version as ready without proving current
  compatible full text. Restore the approved `add_candidates` boundary, carry only bounded target
  paper IDs, create dependencies only for this run's targets, and use the same safe source identity,
  digest filename, job/build convergence and active-compatible readiness rules as Phase 1. Prove an
  unrelated project paper cannot control the run, repeated workflow/Agent enqueue creates one job and
  one build, two runs may share a build while retaining separate dependencies, and URL path/query
  sentinels never enter a job or public surface.
- **P2-C-CX-02 - BLOCKED (P1):** `renew_owner` is defined and unit-tested but is never called by the
  production graph or task. The lease is acquired once before the whole graph and can expire during
  a slow node, allowing overlapping owners. Pass the token only through ephemeral runtime context,
  renew at every committed node boundary, and stop safely when renewal fails. A stale owner MUST NOT
  mark the shared run failed or persist a report/event after takeover. Add production-path expiry,
  renewal, takeover and owner-loss controls; the token remains absent from checkpoints, broker
  payloads, events, logs and APIs.
- **P2-C-CX-03 - BLOCKED (P1):** node transitions are not fully idempotent. Most `workflow_node`
  events use the non-deduplicated publisher, and completion is emitted once by the graph and again by
  the Celery task. Give every logical node terminal/wait/resume/retrieval/completion/failure event a
  stable attempt-free dedupe key, remove or converge duplicate producers, and prove exact event
  counts after replay and duplicate resume, not only report and `rag_committed` counts.
- **P2-C-CX-04 - BLOCKED (P1):** duplicate/redelivered start semantics and checkpointer connection
  lifecycle are not closed. A `start` delivery always supplies fresh graph input even when the run is
  already waiting, and the global async connection is closed from a different event loop with errors
  silently ignored. Re-entry MUST choose safe skip/resume from authoritative run/checkpoint state and
  MUST NOT restart search/enqueue while waiting. Use a task-scoped saver connection or a bounded,
  explicitly closed pool/session and prove repeated start/resume invocations do not grow PostgreSQL
  sessions or reuse an event-loop-bound connection incorrectly.
- **P2-C-CX-05 - BLOCKED (P1 evidence):** the report claims the draft is private-only, but the fixed
  `sensitive-scan.json` records `draft_in_private_only=false` while still declaring the scan clean.
  Add a non-empty opaque draft sentinel positive control: it MUST exist in `ProjectRun.draft_output`
  and the persisted report, and MUST be absent from checkpoint tables/pending writes, events, logs,
  API, Celery results and serializer output. Report consistency MUST fail when this field is false.

**Drift verdict: `BLOCKED`.** The LangGraph/checkpoint/interrupt direction is still aligned and the
feature-flag fail-closed path is accepted, but Tasks 4.1-4.7 cannot be approved while the run target,
lease, event-idempotency, re-entry and private-state contracts above remain unresolved. The next DS
package is limited to P2-C-CX-01 through P2-C-CX-05. Batch D Tasks 5.x and GLM acceptance remain
forbidden.

### Codex Batch C Second Re-review (2026-08-14)

- **P2-C-R2-01 - BLOCKED (P0 owner fencing):** the context-local token and expired-lease exception are
  present, but most production nodes renew only after their durable side effect. Search persists
  papers before renewal; add writes memberships before renewal; enqueue creates/claims/dispatches
  ingestion before renewal; RAG commits its event before renewal; draft writes private output before
  renewal; and persist creates the report before renewal. The current lease test calls `_renew_lease`
  directly and therefore cannot prove stale-owner side effects are blocked. Reorder each node so
  expensive read/model work may happen first, ownership is then renewed immediately before the first
  durable/external side effect, and no write/publish/dispatch follows a failed renewal. Wrap every
  post-acquisition task path, including the initial run update/event, in guaranteed context cleanup
  and owner release. Add production-node controls that expire or replace the owner immediately before
  each side-effect boundary and assert zero membership/job/build/task/event/draft/report writes.
- **P2-C-R2-02 - BLOCKED (P0 scope and readiness):** target narrowing and the separate
  `add_candidates` node are present, but excluded memberships are still selected by enqueue. A job,
  build and Celery task are created before `create_workflow_dependency` rejects the excluded paper.
  Validate accepted project membership before all ingestion side effects and return only accepted
  target IDs from the add boundary. In addition, `_has_ready_fulltext` checks parser/status/count but
  not the effective embedding model, version and dimension or a matching persisted chunk; stale or
  empty metadata can be marked ready. Reuse the active-compatible retrieval identity and prove own
  active positive plus excluded, foreign, unlinked, stale-model, stale-version, wrong-dimension,
  zero-chunk and missing-chunk negatives all produce no premature ready state or side effect.
- **P2-C-R2-03 - BLOCKED (P1 re-entry and event contract):** waiting duplicate-start resume is covered,
  but an expired-owner `running` run still follows the fresh start path and can restart search instead
  of continuing its checkpoint. Implement authoritative re-entry for both waiting and previously
  started/running runs without moving Batch D reconciliation into this package. The active delta also
  requires a deduplicated resume event, but no `workflow_resumed` schema/producer/evidence exists.
  Add one stable resume event and exact replay counts. Make `CheckpointerSession` ownership explicit:
  `build_project_workflow()` must not silently allocate an uncloseable session when no session is
  supplied, and close failures must have a safe observable outcome rather than being swallowed.
- **P2-C-R2-04 - BLOCKED (P1 evidence/provenance):** the draft test manually calls `_store_draft` and
  `_save_report`, constructs a synthetic Celery result, and does not capture logs; it does not prove
  the claimed production graph surfaces. The key `p2c-cx-evidence.json` and shared-build JSON have no
  checked-in generator, while `verify_evidence.py` rewrites its own `cases.json` and
  `report-consistency.json`. Replace this with a deterministic production-task scenario that returns
  a non-empty opaque draft, captures actual task output and logs, scans checkpoint/pending-write,
  event, serializer and HTTP surfaces, and generates the machine-readable evidence before a read-only
  verifier runs. Input hashes must remain unchanged during verification. Remove the untracked
  `backend/dbg_bc02.py` debug script and regenerate the detached manifest from the final worktree.

**Drift verdict: `BLOCKED`.** The second package resolves the graph shape, bounded target IDs, safe
source naming, basic dedupe and task-scoped production saver path, but it does not yet provide true
owner fencing, fail-before-side-effect project validation, running-run checkpoint re-entry or
reproducible private-state evidence. Tasks 4.1-4.7 remain unchecked. The next package is limited to
P2-C-R2-01 through P2-C-R2-04; Tasks 5.x and GLM acceptance remain forbidden.

### Codex Batch C Third Re-review (2026-08-14)

- **P2-C-R3-01 - BLOCKED (P0 owner fencing):** renewal now precedes the main side effects in most graph
  nodes, and the production-path owner-loss control is materially stronger. The fencing contract is
  still incomplete at orchestration boundaries. `await_ingestion` refreshes dependencies, writes the
  waiting transition and publishes the waiting event before renewing; the resume event is published
  before a fresh renewal; one renewal protects the entire multi-paper enqueue loop; and the Celery
  wrapper writes terminal `done` or generic `error` state without proving that it still owns the run.
  A lease can therefore expire or be replaced after the last graph renewal and a stale worker can
  dispatch a later paper, publish resume/waiting state or terminalize the shared run. Fence immediately
  before every durable publish/write/dispatch group, renew inside the per-paper enqueue loop, and
  re-check ownership before task-level waiting/done/error commits. Owner loss MUST produce the existing
  safe skipped result and MUST NOT mark the run done/error. Add production-task controls that replace
  the owner immediately before each of these boundaries and assert zero stale job/build/delivery/event,
  waiting, report and terminal-run writes.
- **P2-C-R3-02 - BLOCKED (P0 Phase 1 convergence regression):** URL source identity is not canonical
  across entry points. The URL API passes the raw URL to `IngestionService.claim_build`, while Agent
  and workflow paths pass `sha256(url)`; `claim_build` hashes its input again for `source_sha256` and
  the pipeline key. The same paper and URL can consequently claim two global builds. Move URL source
  identity derivation behind one `IngestionService` boundary and make the URL API, Agent auto-queue and
  durable workflow use that exact canonical value for request keys, safe filenames and build claims.
  Prove the same URL submitted through all three paths converges on one global version/build, while
  projects retain scoped jobs and raw URL/path/query sentinels remain absent from public and artifact
  surfaces.
- **P2-C-R3-03 - BLOCKED (P1 four-proof evidence):** the detached artifact manifest verifies 27/27
  files and the read-only verifier currently returns PASS, but the fixed Markdown report is outside
  that manifest and has no unique machine-readable summary or report-to-JSON consistency result.
  The fourth completion proof is therefore missing. Add one unique summary JSON block to the report
  and a read-only consistency check that compares its claimed counts, exits, gates, provenance and
  remaining red case with the fixed raw artifacts. Include the report and consistency result in a
  detached final manifest and add mutations for a changed count, changed verdict and duplicate summary
  block.

**Drift verdict: `BLOCKED`.** The third package resolves the prior graph-shape, scope, readiness,
re-entry, event-deduplication, connection-lifecycle and private-state findings, and its artifact
verifier is reproducible. The remaining blockers are two existing cross-phase invariants (single
owner and single global ingestion build) plus the missing report-consistency proof. This is one final
bounded Batch C repair package; Tasks 4.1-4.7 remain unchecked, and Batch D and GLM acceptance remain
forbidden until all three findings are green together.

### Codex Batch C Final Static Review (2026-08-14)

- **P2-C-R3-01 - RESOLVED:** the production graph now renews before waiting/resume and node side
  effects, the per-paper enqueue path is fenced, and the Celery wrapper refuses waiting, done and
  error writes after owner replacement. The five-boundary production-task control is accepted for
  Batch C. Real process overlap, broker interruption and lease-expiry timing remain the already
  specified Batch E fault gate rather than a new Batch C implementation requirement.
- **P2-C-R3-02 - RESOLVED IN CODE / EVIDENCE FOLLOW-UP:** URL identity is now derived by the single
  `IngestionService.enqueue_url_job` boundary and the URL API, Agent and workflow pass the same raw URL
  into the existing Phase 1 build identity. The duplicate hash-of-hash build path is removed. Extend
  the focused control to use two projects so the existing scoped-job claim is directly demonstrated:
  one global build, one job per project, and no raw URL/path/query on public or artifact surfaces.
- **P2-C-R3-03 - BLOCKED (P1 false-PASS verifier):** the report is now in the 33-file detached manifest
  and its unique 20-field summary currently matches the fixed artifacts. However,
  `verify_report_consistency.py` hardcodes `focused_batch_c_pass=14`,
  `focused_batch_b_pass=44` and `verdict=PASS` instead of deriving them from named case status, suite
  exit and gate results. A failing Batch C case can therefore still be reported as 14/14 PASS. Derive
  per-group pass/fail/error counts, parser completeness, focused exit and final verdict from the raw
  focused output plus fixed gate artifacts; fail closed on any unexpected result. Add mutations for a
  real Batch C case changing PASS to FAIL, non-zero focused exit, a missing named terminal record and
  a provenance/untracked-worktree mismatch. Also correct the Markdown prose that says 19 fields while
  the summary/verifier contain 20.

**Drift verdict: `BLOCKED` (evidence-only).** Batch C production behavior is `NO DRIFT` and the two P0
implementation findings are resolved. Tasks 4.1-4.6 may be treated as implementation-complete, but
4.7 and the Batch C gate remain open until the false-PASS verifier and the cross-project positive
control above are corrected. This correction MUST NOT modify production code or rerun the unchanged
268/322 suites; rerun only the affected focused tests and regenerate the report, consistency result,
mutations and detached manifest. Batch D and GLM acceptance remain paused for this final evidence
closure.

### Codex Batch C Final Evidence Recheck (2026-08-14)

- The dynamic verifier correction and two-project URL/build control are accepted. The focused parser
  derives 14/14 Batch C and 44/44 Batch B results from 58 named records, verifies the separate expected
  Batch A reconciliation red case, checks focused/full/regression exits, and computes the final verdict
  from gates. The eight report-consistency mutations and unique 23-field summary are present.
- **P2-C-R3-04 - BLOCKED (P2 packaging):** an independent read-only manifest check currently fails for
  exactly one file: `report-consistency-final-check.txt`. Its live SHA-256 is
  `9ed9c218423328c7734d682aa05c65bec426c49011a6213deb251f64df9952ad`, while the detached manifest
  stores the stale value `8e7aac38678faf883017151b42c3c3386dbaef60587b832d3a76540e85b6c2ca`.
  Regenerate the final consistency output/check first, generate the detached manifest last, then run
  the independent manifest verifier. Do not modify production code, tests, report claims or fixed raw
  suite outputs, and do not rerun backend/frontend suites.

**Drift verdict: `BLOCKED` (single evidence-packaging mismatch).** Production behavior remains
`NO DRIFT`; Tasks 4.1-4.6 remain accepted in substance. Task 4.7 and Batch D authorization require one
clean 33/33 detached-manifest verification from the unchanged worktree.

### Codex Batch C Approval And Batch D Authorization (2026-08-14)

- **P2-C-R3-04 - RESOLVED:** the two final consistency files are byte-identical with SHA-256
  `9ed9c218423328c7734d682aa05c65bec426c49011a6213deb251f64df9952ad`; the detached manifest verifies
  33/33, the evidence verifier returns PASS, and the dynamic report-consistency verifier recomputes
  23/23 fields with one expected Batch A reconciliation red case.
- Tasks 4.1-4.7 are approved. The accepted Batch C surface includes the PostgreSQL checkpointer,
  bounded checkpoint state, Phase 1 ingestion convergence, interrupt/resume, database owner lease,
  idempotent events/report ownership, feature-flag fail-closed behavior and reproducible private-state
  evidence. The implementation remains within the approved LangGraph/Celery ownership boundary.
- The evidence provenance was independently verified immediately before this Codex-owned approval
  ledger update. This post-evidence `tasks.md` edit is approval metadata and MUST NOT trigger another
  evidence regeneration cycle; future evidence manifests should distinguish the implementation
  snapshot from subsequent Codex review-ledger changes.

**Drift verdict: `NO DRIFT`. Batch C verdict: `PASS`.** Batch D Tasks 5.1-5.7 are authorized. DS must
start with executable red tests for terminal callbacks, reconciliation, strict timing, deterministic
done/partial/error gates and minimal frontend status behavior. Batch E fault injection and GLM
acceptance remain forbidden until Batch D receives a separate Codex review.

## 5. Batch D — Recovery, Partial Results And Minimal UI

- [x] 5.1 Set ingestion terminal timestamps exactly once and use `transaction.on_commit` to synchronize
  affected dependencies and enqueue idempotent resume requests.
- [x] 5.2 Add a stateless Celery Beat service and 15-second bounded reconciliation task for ready waiting
  runs, expired owners, pending starts and never-started pending dependency jobs; it may enqueue only
  existing idempotent work and must never execute ingestion or advance graph state.
- [x] 5.3 Enforce strict timing so the first committed RAG begins after the latest dependency terminal
  timestamp and uses only current project-scoped active compatible full text.
- [x] 5.4 Implement deterministic done/partial/error gates and critic downgrade-only behavior; partial
  reports must disclose failed/unavailable dependencies and unsupported runs must create no report.
- [x] 5.5 Keep normalized search state as paper IDs, persist only the report draft in private
  non-serialized storage, and prove checkpoints, events, logs, API and Celery results contain no
  question, candidate payload, draft body or other prohibited content.
- [x] 5.6 Add minimal frontend types and Run Inspector labels for waiting, partial, phase, dependency
  counts and report link; do not add SSE replay, reconnect or store decomposition.
- [x] 5.7 Run focused backend/frontend tests and full regression, submit the four-part report and stop
  for Codex review.

### Codex Batch D Static Review (2026-08-14)

- **P2-D-CX-01 - BLOCKED (P0 Stage B evidence/citation regression):** the workflow retrieval node
  calls project-wide `query_project_rag` and reduces its result to `evidence_count`. The deterministic
  outcome, critic and report persistence paths consequently treat any returned item, including
  metadata fallback or evidence from a same-project paper that is not a successful workflow
  dependency, as usable full text. `draft_report` then performs a second project-wide retrieval and
  creates citation-looking markers without resolving those markers against canonical
  `EvidenceEnvelope` identities. This violates the frozen Stage B requirement that factual/report
  output needs resolved, answer-bound full-text evidence and the Phase 2 requirement that only
  successful active workflow dependencies may enter RAG context. Restrict retrieval to the run's
  `ready`/`succeeded` dependency paper IDs, exclude metadata fallback from the full-text gate, resolve
  canonical envelopes through `CitationResolver`, and prove the persisted draft/report binds its
  markers to resolved target evidence. The critic remains downgrade-only. Metadata-only, foreign,
  unrelated same-project, failed-dependency, stale-version, malformed-marker and unbound-marker
  controls MUST all produce `error` with zero workflow report; a canonical active target chunk plus
  a resolved bound marker is the required positive control.
- **P2-D-CX-02 - BLOCKED (P0 terminal transition atomicity):** `mark_terminal_once` mutates an
  in-memory job object and is not concurrency-safe. Two redeliveries may both observe `terminal_at`
  as null and save different timestamps. The active-build reuse branch also changes a job to
  `embedded` without stamping `terminal_at` or scheduling the dependency synchronization callback.
  Replace the scattered terminal writes with one database-atomic terminal transition/finalization
  boundary used by every embedded/failed path, including active reuse. A concurrent redelivery
  control MUST prove one terminal timestamp, one dependency terminal transition and bounded
  idempotent wakeup behavior.
- **P2-D-CX-03 - BLOCKED (P1 recovery/public-state consistency):** reconciliation repairs stale
  dependency rows but does not refresh the parent run's `last_ingestion_terminal_at`. In addition,
  the workflow task stores `partial` correctly in the database but hardcodes `done` in its log and
  Celery return payload. Reuse the shared dependency synchronization boundary from the on-commit
  path during reconciliation, and return/log the actual terminal status. Add controls proving that
  dependency timestamps, run serializer/API state, log status and Celery result agree for partial,
  done and lost-wakeup recovery.
- **P2-D-CX-04 - BLOCKED (P1 non-vacuous tests and repository hygiene):** current Batch D policy tests
  mock the production retrieval node or inject noncanonical evidence IDs, so they cannot detect
  P2-D-CX-01. The sensitive error check declares an exception token without forcing a production
  exception path. Replace these with real active `Text` rows, canonical IDs/hashes/versions,
  production `CitationResolver` and actual workflow query/report boundaries; inject one opaque
  exception through a real producer and scan every required surface. Remove untracked
  `backend/dbg_bd04.py` and `backend/dbg_bd05.py` before regenerating evidence.

**Drift verdict: `BLOCKED`.** The checkpoint, owner fencing, callback/reconciliation shape, timestamp
ordering scaffold and minimal frontend status surface remain within the approved Phase 2 design, and
the current 25-file package is internally reproducible. It does not prove the semantic evidence gate
or atomic terminal transition, so Tasks 5.1-5.7 remain unchecked. DS is authorized for one consolidated
P2-D-CX-01 through P2-D-CX-04 repair package. During development run only the affected focused tests;
after the fixed revision is frozen, run the combined focused suites and each required full regression
exactly once, then regenerate the raw artifacts, report consistency result and detached manifest.
Batch E and GLM acceptance remain forbidden until a separate Codex re-review records `NO DRIFT`.

### Codex Batch D Consolidated Repair Re-review (2026-08-14)

- **P2-D-R2-01 - BLOCKED (P0 unresolved answer binding):** dependency paper scoping and database
  reference resolution were added, but `_validate_resolved_evidence` only counts resolvable envelope
  objects. It never proves that the generated draft/report cites those objects. `draft_report` still
  invokes the project-wide `draft_report_section`, performs a second retrieval outside the run's
  dependency scope and stores that independent output; `persist_report` then authorizes persistence
  using the earlier integer count without parsing or resolving the draft's markers. A report with no
  citation, a fabricated citation-looking marker or same-project unrelated evidence can therefore be
  persisted after an unrelated envelope resolved earlier. Carry only bounded canonical evidence
  identities from the scoped retrieval, reload those identities from the database for drafting, and
  revalidate the final draft/report markers against that exact dependency-scoped resolved set before
  persistence. `resolved_reference_count` and `answer_bound_fulltext_count` MUST remain separate;
  only the latter may authorize done/partial. The production-path controls MUST prove the same draft
  and persistence boundary, not call `CitationResolver` independently or mock `query_hybrid_rag`.
- **P2-D-R2-02 - BLOCKED (P0 terminal boundary remains bypassed):** the new
  `finalize_job_terminal` function has no production call sites. Failure, normal success and active
  reuse still write status through the previous paths and call `mark_terminal_once` or no terminal
  callback at all. `mark_terminal_once` performs an atomic timestamp update but then assigns a second
  timestamp to the model object and callers save it again, so the winning database value can still be
  overwritten. `finalize_job_terminal` also invokes `_sync_and_wake` immediately rather than through
  the promised transaction-on-commit boundary. Replace all terminal writes with one actually used
  transactional service: first-wins database transition, refresh the winning timestamp, commit job
  and related run state, then register dependency sync/wakeup with `transaction.on_commit`. Remove the
  obsolete path. Add real concurrent finalizers plus normal-success, permanent-failure and active-reuse
  controls; sequential redelivery is insufficient.
- **P2-D-R2-03 - BLOCKED (P1 reconciliation/result consistency still incomplete):** lost-wakeup
  reconciliation still mutates dependency rows directly instead of reusing the shared synchronization
  boundary and therefore still omits the parent run timestamp refresh. The task now derives
  `final_status`, but its completion log and returned result still hardcode `done`. Require DB run,
  dependency summary, serializer/API, log and Celery result to agree for done, partial, error and
  lost-wakeup recovery.
- **P2-D-R2-04 - BLOCKED (P1 false semantic evidence):** debug scripts were removed and the fixed
  25-file package verifies 25/25, but the policy tests still mock `query_hybrid_rag` to return an
  arbitrary count and mock the draft generator. The citation evidence generator tests the resolver in
  isolation, not report binding. The sensitive test merely includes the string `RuntimeError` in its
  scan set without injecting an opaque exception through a production producer. Add non-vacuous
  end-to-end controls and make the report verifier fail when any required negative case is absent.

**Drift verdict: `BLOCKED`.** The implementation moved in the approved direction, but the four-proof
report overstates completion: P2-D-CX-01 and P2-D-CX-02 remain behaviorally unresolved, and the claimed
P2-D-CX-03 repair is only partial. Tasks 5.1-5.7 remain unchecked. One final consolidated repair is
authorized for P2-D-R2-01 through P2-D-R2-04; do not add another packaging-only round. Run focused
tests while developing, freeze one revision, then run each complete suite once and regenerate evidence.
Batch E and GLM acceptance remain forbidden.

### Codex Batch D Second Consolidated Re-review (2026-08-14)

- **P2-D-R3-01 - BLOCKED (P0 exact-evidence binding still bypassed):** the scoped retrieval now emits
  bounded canonical `evidence_ids`, but no later node consumes them. `draft_report` computes
  `bound_markers` and leaves the value unused, then still calls project-wide `draft_report_section`.
  `_verify_marker_binding` ignores the saved evidence IDs and treats every marker from every active
  chunk of a ready/succeeded dependency as resolved without running `CitationResolver` or checking
  current embedding compatibility. The draft can therefore bind to a chunk that was never retrieved,
  and marker collisions at paper/docname level can authorize unsupported content. Make the retrieved
  canonical IDs the sole evidence manifest for the run: reload exactly those chunks, resolve them
  again, generate the draft from that set, and bind final markers to the exact resolved identities.
  Do not derive eligibility from all active chunks or marker strings alone. Persist must recompute
  this relationship from the database manifest and draft, not trust state counts.
- **P2-D-R3-02 - BLOCKED (P0 terminal first-wins outcome not honored):** all three production paths
  now call `finalize_job_terminal`, and on-commit synchronization is present. However, callers ignore
  its first-wins result and authoritative refreshed job status. A losing failure path still marks its
  ingestion run failed and publishes `ingestion_failed` after another worker won with `embedded`; a
  losing success path still marks its run done, publishes completion and returns `embedded` after a
  failure won. The concurrency test runs four identical `embedded` finalizers and accepts up to four
  wakeups, so it cannot detect conflicting terminal outcomes or duplicate wakeups. Return the
  authoritative terminal job snapshot/outcome from the service and make every caller publish/log/
  return only that result. Add an embedded-versus-failed race in both winner orders and require one
  coherent terminal state plus at most one immediate wakeup for the run.
- **P2-D-R3-03 - BLOCKED (P1 semantic controls remain partly direct):** the opaque exception control,
  debug cleanup, reconciliation timestamp refresh and dynamic task status are accepted. The marker
  negatives still call `_verify_marker_binding` directly rather than traversing draft and
  `persist_report`; the positive policy path mocks both retrieval and draft generation. Replace only
  these controls with production-path report outcomes and add a negative proving a valid marker from
  an allowed dependency chunk that was not in the run's retrieved evidence manifest cannot authorize
  a report. Keep the existing fixed evidence and packaging machinery; regenerate it once after the
  production behavior is corrected.

**Drift verdict: `BLOCKED`.** P2-D-R2-03 and the opaque-exception portion of P2-D-R2-04 are resolved.
The remaining scope is limited to exact retrieved-evidence binding, authoritative mixed-outcome
terminal finalization and their non-vacuous controls. Tasks 5.1-5.7 remain unchecked. Do not modify
reconciliation, frontend, OpenSpec capability requirements, Phase 3 retrieval ranking or Phase 4 SSE.
After the corrected revision, run the affected focused tests and each unchanged complete suite once;
Batch E and GLM acceptance remain forbidden until Codex records `NO DRIFT`.

### Codex Batch D R3 Re-review (2026-08-14)

- **P2-D-R4-01 - BLOCKED (P0 production drafting ignores the evidence manifest):**
  `draft_report` reloads and resolves `state.evidence_ids`, but leaves the resulting `verified` list
  unused and still calls the ordinary project-wide `draft_report_section(project_id, question)`.
  That tool performs another unrestricted project RAG query and emits title/citation markers instead
  of canonical evidence IDs. The exact-manifest check in `persist_report` is directionally correct,
  but the default production drafting path cannot reliably produce the canonical markers that it
  requires. Add a workflow-specific drafting boundary that consumes only the reloaded, resolved
  manifest evidence and emits `[cite:<evidence_id>]`; it MUST NOT perform project-wide retrieval.
  Add one positive production-path control that does not mock this drafting boundary and proves the
  recalled canonical chunk produces one report, plus un-recalled, stale and marker-collision controls
  that produce no report.
- **P2-D-R4-02 - BLOCKED (P0 authoritative terminal result is not propagated by task callers):**
  `finalize_job_terminal` now supplies a coherent first-wins snapshot and schedules synchronization
  only for the winner. However, the PDF-load exception path ignores the returned snapshot and always
  re-raises, while the zero-chunk path always returns `failed`. If another delivery already won with
  `embedded`, those task surfaces contradict the authoritative database state. Propagate one mapping
  from the finalizer result to ingestion run status, event/log emission and Celery return/exception
  behavior for every permanent-failure path. Add task-level mixed-outcome controls in both winner
  orders; helper-only finalizer tests are insufficient.
- **P2-D-R4-03 - BLOCKED (P1 evidence overstates production coverage):** the fixed 25-file package,
  all three read-only verifiers and the 44-case focused suite are internally consistent, but the R3
  gate mocks `draft_report_section` to inject the canonical marker and the concurrency suite calls
  `finalize_job_terminal` directly. The stale-ID case also mutates evidence after a report has already
  been persisted. Replace only these seams with production-path controls and regenerate the existing
  evidence package once; do not add another packaging-only round.

**Accepted in this review:** exact manifest revalidation in `persist_report`, the core atomic
first-wins finalizer, winner-only on-commit synchronization, reconciliation/timestamp repairs, opaque
exception safety and the frozen frontend increment. **Drift verdict: `BLOCKED`.** This is one bounded
production-integration repair, not a redesign of Batch D. Tasks 5.1-5.7 remain unchecked. During
development run only the affected focused tests; after freezing the corrected revision, run each
required complete suite once and regenerate the fixed report package. Batch E and GLM acceptance
remain forbidden until Codex records `NO DRIFT`.

### Codex Batch D Approval And Batch E Authorization (2026-08-14)

- **P2-D-R4-01 - RESOLVED:** the workflow drafting node now consumes only the database-reloaded,
  CitationResolver-verified evidence manifest and emits canonical evidence-ID markers without a
  second project-wide retrieval. Persist-time binding independently rechecks the exact manifest.
  The production draft boundary is unmocked in the positive control; un-recalled, stale-between-draft-
  and-persist, marker-collision and fabricated-ID controls create no report.
- **P2-D-R4-02 - RESOLVED:** permanent failure, zero-chunk, generic failure, normal success and active
  reuse paths now propagate the authoritative first-wins result. Already-terminal delivery exits
  before creating a phantom ingestion run, while the atomic finalizer remains the authority inside
  the concurrent window. Mixed terminal controls cover both winner orders and the existing concurrent
  finalizer suite covers the race boundary; real multi-worker races remain the explicit Batch E gate.
- **P2-D-R4-03 - RESOLVED:** the fixed package independently verifies 24/24 artifacts, 51/51 focused
  cases, 268/268 backend regression, 322/322 Phase 1 plus Stage B regression, frontend 64/64 plus
  build, strict OpenSpec, report consistency, required negative controls and unchanged connection
  count. The three read-only verifiers return PASS.

The deterministic draft produced in this phase is an evidence/citation durability scaffold, not a
claim of DeepSeek report quality; meaningful model-output quality remains governed by Phase 5 live
evaluation. The unused legacy import/helper is cleanup debt and is not part of the runtime path or a
Batch E blocker.

**Drift verdict: `NO DRIFT`. Batch D verdict: `PASS`.** Tasks 5.1-5.7 are approved. DS is authorized
to execute Batch E Tasks 6.1-6.7 exactly as specified, using real PostgreSQL, Redis and non-eager
Celery fault injection. Do not expand into Phase 3 retrieval ranking, Phase 4 SSE or Phase 5 live LLM
quality. GLM independent acceptance remains paused until Batch E completes and Codex records a
separate static-review verdict.

## 6. Batch E — Real Fault And Concurrency Verification

- [x] 6.1 On real PostgreSQL/Redis/non-eager Celery, run 20 concurrent duplicate start/resume requests
  and prove one owner, one report, one committed RAG event and no duplicate ingestion build.
- [x] 6.2 Kill and restart workers at enqueue, waiting, resume and report persistence boundaries for 10
  repetitions each; every run must recover or reach an explicit stable terminal error.
- [x] 6.3 Drop an immediate terminal wakeup and prove Beat reconciliation resumes the run within 30
  seconds; interrupt Redis briefly and prove durable database/checkpoint state remains consistent.
- [x] 6.4 Verify all-success, partial-success, all-failed, existing-active, no-URL and cross-project
  scenarios with non-empty controls and strict timestamp ordering in every RAG-producing run.
- [x] 6.5 Scan checkpointer tables, pending writes, workflow private state, ProjectRunEvent, logs, API,
  Celery results and frontend fixtures for sensitive and opaque sentinels; leakage must be zero.
- [x] 6.6 Run Docker PostgreSQL full backend regression, frontend Vitest/build, migration drift,
  Django check, OpenSpec strict validation, network/secret scan and public-file audit.
- [x] 6.7 Generate machine-readable manifests, raw commands, case results, database recomputation,
  timings, failure injection records and report-consistency mutation tests from one fixed revision;
  submit the final DS four-part report and stop before GLM.

### Codex Batch E Static Review (2026-08-15)

- **P2-E-CX-01 - BLOCKED (P0 non-vacuous concurrency gate):** the 20-delivery raw result has nineteen
  skipped tasks and one failed winner; the authoritative run is `error` with zero reports and zero
  committed RAG events. `e61_concurrent.py` and `verify_evidence.py` incorrectly define single report
  and single RAG as `count <= 1`, so an empty failed workflow passes the gate. Require the winner to
  reach the expected useful terminal state and require exactly one workflow report and exactly one
  committed RAG event for this positive fixture, while retaining one owner, one build and nineteen
  safe duplicate exits. Record and fix the stable root error before rerunning the concurrency proof.
- **P2-E-CX-02 - BLOCKED (P1 kill/restart evidence predates the repair):** the authoritative
  `e62-kill-restart.json` still contains two enqueue cases with `stable=false` and therefore proves
  38/40, not the reported 40/40. No machine-readable post-repair 11/11 result is present. The verifier
  lowers the approved 100% gate to 80% and hardcodes `post_beat_recovery_verified=true`. The orphaned
  running reconciliation repair was made after the captured complete regression outputs, so those
  suites do not prove the final code revision. Preserve the original 38/40 failure artifact, then on
  one fixed repaired revision rerun all four boundaries for 10 repetitions and require 40/40 explicit
  recovered-or-stable-terminal outcomes without a percentage fallback.
- **P2-E-CX-03 - BLOCKED (P1 lost-wakeup/Redis result is missing):** the package contains only
  `e63_helper.py`; it contains no e63 result JSON or raw command output. `verify_evidence.py` hardcodes
  the claimed 7.8 seconds, zero issues and PASS instead of reading evidence. Capture start, wakeup and
  terminal timestamps, measured elapsed time, Beat identity, Redis interruption interval, checkpoint
  counts and database consistency into a fixed JSON/raw log, derive the under-30-second verdict from
  those values and add mutations for missing, delayed and inconsistent results.
- **P2-E-CX-04 - BLOCKED (P1 incomplete release evidence):** the detached package has no real
  worker/Beat fault logs, frontend Vitest/build outputs, cases JSON, report-consistency result,
  network/secret/public-file audit or final-code full-regression proof despite claiming them. The
  sensitive scan covers in-process scenario logs but not the actual worker/Beat fault surfaces. Add
  the missing raw artifacts, scan the real fault logs and checkpoint/pending-write surfaces, parse
  every required command exit, compare a unique machine-readable report summary with JSON and make
  mutations fail closed. Verifier constants or manual report prose are not evidence.

**Drift verdict: `BLOCKED`.** The orphaned-running reconciliation change is within the approved
stateless Beat boundary and Batch D remains accepted. Tasks 6.1-6.7 remain unchecked because Batch E
currently demonstrates a failed concurrency winner, 38/40 captured fault cases and hardcoded missing
evidence. DS is authorized for one consolidated Batch E correction and fresh fixed-revision fault
run; do not reopen Batch D or add product features. During diagnosis rerun only targeted probes. Once
the production fix is frozen, run the 20-delivery concurrency case, all 40 kill/restart repetitions,
lost-wakeup/Redis tests and each complete regression once, then generate the final package. GLM and
Tasks 7.x remain forbidden until Codex records `NO DRIFT`.

### Codex Batch E Correction Re-review (2026-08-15)

- **P2-E-CX-01 - RESOLVED:** `e61-final.json` now proves a non-empty 20-delivery positive control:
  one `done` winner, nineteen safe skips, exactly one report, exactly one committed RAG event and
  released ownership. The verifier uses exact equality for the report/RAG gate. The prior failed e61
  result remains historical evidence and MUST not be treated as part of the final passing package.
- **P2-E-CX-03 - RESOLVED:** `e63-final.json` records measured wakeup/Beat/terminal timestamps,
  6.2-second recovery, Redis stop/start times, positive checkpoint counts and an empty consistency
  issue list. The verifier derives these gates from JSON and the five requested e63 mutations fail
  closed.
- **P2-E-CX-02 - BLOCKED (P1 post-Beat result is still fabricated):** the current fixed-revision
  `e62-kill-restart.json` has 40 rows but only 34 are stable in the captured window; six enqueue rows
  remain `running` with held ownership. It contains no per-run post-Beat status, timestamp or recovery
  duration. The verifier sets every row's `stable_or_recoverable` count to the group total and still
  hardcodes `post_beat_all_recovered=true`, so it cannot prove 40/40. Add an authoritative post-Beat DB
  recheck for every unstable run, persist the resulting status/owner/recovery time into the evidence,
  and derive 40/40 solely from those fields. Missing, still-running, owner-held or over-30-second rows
  MUST fail. Correct the report's claimed two recovered rows; the current raw file has six.
- **P2-E-CX-04 - BLOCKED (P1 final-revision evidence is stale/incomplete):** the production
  `tasks.py` repair predates `e61-final`, but focused, backend, Phase 1/Stage B and quality outputs are
  older than the repair and were not rerun as claimed. The 35-file manifest still lacks frontend
  Vitest/build raw outputs, cases JSON, report-consistency output, real worker/Beat log scan and the
  network/secret/public-file audit. It also includes the obsolete failed `e61-concurrent.json`, while
  multiple `backend/dbg_*`, `backend/e6*` helpers and `backend/celerybeat-schedule` remain untracked.
  Clean runtime/debug files, preserve failures under `baseline-failed`, rerun the required suites on
  the final worktree, scan real fault logs, add the missing machine-readable artifacts and compare a
  unique report summary against them before generating the detached manifest last.

**Drift verdict: `BLOCKED` (fault evidence only).** The resume-without-checkpoint fallback and
orphaned-running reconciliation remain within the approved durable workflow design; no new Batch D
or architecture defect was found. P2-E-CX-01 and P2-E-CX-03 are closed. The final correction is
limited to truthful e62 post-Beat capture, fixed-revision regressions, hygiene and report consistency;
do not rerun or redesign accepted behavior unnecessarily. Tasks 6.1-6.7 and GLM acceptance remain
blocked until the final package is reproducible without constants or contradictory artifacts.

### Codex Batch E Final-Evidence Re-review (2026-08-15)

- **Global change-surface audit - NO UNRELATED DRIFT FOUND:** every modified production/frontend file
  maps to the approved Phase 2 checkpoint, dependency, ingestion convergence, recovery, event, API or
  minimal Run Inspector surface. Ordinary Chat, Phase 3 retrieval ranking, Phase 4 SSE and Phase 5
  live-model quality remain unchanged. Debug/runtime files identified in the previous review are no
  longer present. The fixed-revision focused, backend, Phase 1/Stage B, frontend and quality outputs
  are now newer than the final production-code modification and are accepted.
- **P2-E-CX-02 - BLOCKED (P1 impossible recovery timestamps):** `e62-final.json` contains all forty
  rows and terminal post-check states, but the six recovered enqueue rows have
  `post_beat_checked_at` roughly 6,700 seconds earlier than `killed_at`, producing negative
  `recovery_time_s`. The verifier never rejects negative or over-30-second recovery and its slow
  recovery branch is a no-op, so it proves eventual terminal state only, not the required recovery
  window. Wall-clock values from different hosts/containers cannot be mixed. Rerun only the affected
  enqueue fault case with one monotonic timer or a single authoritative DB clock and require
  `0 <= recovery_time_s <= 30` for every initially unstable run. Add negative and over-30 mutations.
- **P2-E-CX-04 - BLOCKED (P1 final-package contradiction):** the final directory and detached
  45-file manifest still include the obsolete failing `e61-concurrent.json` and pre-final
  `e62-kill-restart.json`, despite the report claiming failures were moved exclusively under
  `baseline-failed`. The report also calls the evidence verifier a report-consistency proof, but no
  independent report-summary comparison or mutation exists. Keep historical failures only under the
  baseline directory, create a unique machine-readable Markdown summary, compare it field-by-field
  with final JSON in a read-only report-consistency artifact, add summary tamper/duplicate mutations,
  and generate the detached manifest last.

**Drift verdict: `BLOCKED` (final evidence only).** Production behavior and the final-revision
regression set are accepted; no further production-code or broad-suite change is authorized. The
remaining package is limited to one correctly timed enqueue fault rerun plus report/manifest cleanup.
Tasks 6.1-6.7 and GLM acceptance stay blocked until these contradictions are removed.

### Codex Batch E Timing-Evidence Review (2026-08-15)

- **Gate clarification - reviewer correction recorded:** the 30-second requirement applies to lost
  wakeup detection and compensating dispatch, as stated by Task 6.3 and the orchestration capability.
  It does not require terminal completion within 30 seconds after a worker dies while a valid
  300-second owner lease remains. Task 6.2 still requires every repetition to converge to an explicit
  terminal state and release ownership. `design.md` now distinguishes dispatch latency from terminal
  completion latency; no production behavior or security invariant changed.
- **P2-E-CX-02 - BLOCKED (P1 mixed timing evidence):** the six new enqueue rows prove monotonic Beat
  dispatch in 4.6-4.7 seconds and eventual terminal state, but two rows have no measured terminal
  timestamp or duration. `e62-final.json` fills the required ten enqueue repetitions with four older
  rows whose `recovery_time_s` values are negative because they use the rejected mixed wall-clock
  method. The verifier deliberately ignores those negatives unless `time_source == "monotonic"`, so
  its PASS does not prove ten clean enqueue repetitions. Preserve the six new rows, run four more
  enqueue repetitions with the same monotonic orchestration, and rebuild the ten-row enqueue set.
  Each row MUST record monotonic kill and Beat-dispatch offsets, final database status, ownership
  release and either an exact terminal offset or a clearly typed post-check upper bound; negative or
  fabricated durations are forbidden. Beat dispatch MUST remain at most 30 seconds, while terminal
  convergence follows the lease-aware Task 6.2 contract.
- **P2-E-CX-04 - BLOCKED (P2 package hygiene/report accuracy):** the report says the remaining 34 rows
  are waiting/resume/persist cases, but the JSON actually contains thirty such rows plus four old
  enqueue rows. The worktree also still contains `backend/celerybeat-schedule`,
  `backend/e62-enqueue-timing.json`, `backend/e62eq.py` and `backend/show.py`. Remove these untracked
  runtime/debug files, correct the report composition, regenerate report consistency and create the
  detached manifest last. The final verifier must reject any negative duration in the authoritative
  enqueue set and any enqueue row lacking terminal-or-upper-bound timing semantics.

**Drift verdict: `BLOCKED` (evidence and hygiene only).** The production implementation remains
`NO DRIFT`, and the accepted regression outputs need not be rerun because this correction changes no
production or test code. DS is authorized for exactly one evidence-only correction: four monotonic
enqueue repetitions, verifier/report regeneration and removal of the four stray files. Do not modify
workflow production code, reopen Batch D, or rerun the other thirty fault cases. GLM remains paused.

### Codex Batch E Terminal-State Review (2026-08-15)

- **P2-E-CX-04 - RESOLVED:** the four stray runtime/debug files are gone, the authoritative aggregate
  now contains ten rows for each of enqueue, waiting, resume and persist, historical failed artifacts
  are excluded from the detached manifest, and the report/manifest/verifier chain is reproducible.
- **P2-E-CX-02 - BLOCKED (P1 non-terminal rows accepted as terminal):** enqueue repetitions 8 and 10
  have `final_status == "waiting_ingestion"`, no Beat-dispatch timestamp and no committed RAG/report,
  yet `recovered_or_stable_terminal` is set to true. `waiting_ingestion` is an interrupt state, not one
  of the terminal states `done`, `partial` or `error`; therefore Task 6.2 is still 8/10 for enqueue and
  38/40 overall. The verifier only rejects literal `running`/`pending` and consequently passes this
  invalid state. Repetition 9 also reports `done` with null report/RAG/build counts.
- **P2-E-CX-02 - BLOCKED (P1 ambiguous upper-bound evidence):** all five rows labelled
  `post_check_upper_bound` lack a measured terminal timestamp, and two are not terminal at all. To
  remove interpretation risk, upper-bound rows are not accepted in the release artifact. The final
  enqueue fixture MUST contain ten exact monotonic rows. Every row MUST provide measured kill, first
  compensating Beat-dispatch and terminal offsets; non-negative dispatch/recovery durations; a final
  `done|partial|error` database status; released ownership; and non-null report/RAG/build counts
  appropriate to that outcome.

**Drift verdict: `BLOCKED` (final evidence only).** No production-code change, complete-regression
rerun or redesign is authorized. Preserve the five valid exact enqueue rows and rerun only the five
invalid rows (reps 3, 4, 8, 9 and 10) with the same external monotonic orchestrator kept alive until
terminal observation. The verifier MUST require `terminal_semantics == "exact"`, accept only
`done|partial|error`, reject missing Beat/terminal timing, and validate the side-effect counts. Add
mutations for `waiting_ingestion`, missing Beat timing, missing terminal timing, null side-effect
counts and any non-exact semantics. Regenerate report consistency and the detached manifest last.
This evidence simplification does not change runtime behavior or the 300-second production lease.
GLM remains paused until this package passes Codex review.

### Codex Batch E Final Approval (2026-08-15)

- **P2-E-CX-02 - RESOLVED:** `e62-enqueue-10.json` contains ten unique repetitions and run IDs, all
  measured by the same external monotonic clock with exact kill, first Beat-dispatch and terminal
  offsets. All rows end in `done` or `error`, release ownership, contain non-null side-effect counts,
  satisfy the duration equations and keep Beat dispatch within 0.0-6.0 seconds. The generated
  `e62-final.json` matches those ten rows field-for-field and contains exactly ten rows for each fault
  boundary. Independent recomputation found no row-level violation.
- **Evidence and report gate - PASS:** the evidence verifier, report-consistency verifier, twenty
  behavioral mutations plus read-only/clean-state checks, detached 51-file manifest and OpenSpec
  strict validation all pass. The accepted focused/full/Phase 1/frontend outputs were not rerun after
  the evidence-only correction, as authorized; no production or test code changed during that
  correction.
- **Change-surface gate - NO DRIFT:** modified production/frontend files remain limited to the approved
  durable workflow, lifecycle API/event and minimal Run Inspector scope. Ordinary chat, Stage B
  evidence/security boundaries, Phase 1 immutable ingestion, Phase 3 retrieval ranking, Phase 4 SSE
  and Phase 5 live-model evaluation remain outside this change. Runtime/debug files are absent.

**Drift verdict: `NO DRIFT`. Batch E verdict: `PASS`.** Tasks 6.1-6.7 and Codex static review 7.1 are
approved. GLM is authorized to begin Task 7.2 independent acceptance against the fixed worktree. GLM
MUST not modify production code, reuse DS aggregate verdicts or weaken the frozen gates. Archive and
Tasks 7.3-7.5 remain blocked until Codex reviews GLM's raw evidence.

## 7. Independent Acceptance And Archive

- [x] 7.1 Codex performs static review and records exactly `NO DRIFT`, `DRIFT RESOLVED` or `BLOCKED`
  against proposal non-goals, Stage B, Phase 1, REST compatibility and all machine-readable evidence.
- [x] 7.2 GLM independently reruns concurrency, worker kill, broker interruption, lost wakeup, lease
  expiry, timestamp, uniqueness, project scope, checkpoint leakage and frontend contract tests without
  modifying production code or relying on DS aggregate verdicts. The fixed package MUST be
  self-contained: its verifier reads only captured artifacts, records required runtime versions from
  the actual worker/database containers, proves queue depth and Redis `unacked` both return to zero,
  and rejects missing/blank provenance, ambiguous controls or report/JSON disagreement.
- [x] 7.3 DS fixes Codex/GLM findings while GLM retains its original assertions unless Codex approves a
  specification correction; all repairs repeat focused and complete regression.
- [x] 7.4 Codex declares only PASS, PASS WITH KNOWN RISKS or FAIL after code, raw artifacts, complete
  tests and generated report agree; unresolved P0/P1 blocks archive.
- [x] 7.5 Merge capability deltas, archive the change with OpenSpec CLI, revalidate current specs,
  update only reverified public claims and submit a clean independent PR before Phase 3 starts.

### Codex GLM Task 7.2 P1 Review (2026-08-16)

- **P2-GLM-01 - CONFIRMED (P1 orphaned in-progress ingestion):** GLM killed the ingestion worker after
  the job entered `parsing`. Nine of ten waiting-boundary runs failed to converge inside the 420-second
  observation window; two recovered only after roughly 3,600 seconds and a second attempt, matching
  Redis/Celery visibility-timeout fallback. Live database recomputation still finds five runs in
  `waiting_ingestion` whose project job remains `parsing`, has `attempt_count == 1` and no
  `terminal_at`. Beat continues to run but emits no corrective wakeup for those jobs.
- **Root-cause correction:** the frozen fourth design scan covered a never-started `pending` job, while
  production reconciliation implements terminal-job dependency synchronization instead. Neither
  contract handles a worker that dies after setting the job to `parsing`. Resetting jobs by status or
  `updated_at` alone is forbidden because a valid long parse may have the same shape. The missing
  invariant is a private, fenced ingestion execution lease with heartbeat and expired-attempt
  redispatch; the active design and capability deltas now state that contract.
- **Accepted GLM evidence:** the independent 20-delivery concurrency controls, ten enqueue-boundary
  fault cases, strict timestamp controls and partial-result controls are non-vacuous and remain useful
  baseline evidence. GLM correctly stopped before S3-S8 and changed no production code. Its report is
  a valid `FAIL`, not a completed Task 7.2 PASS.

**Drift verdict: `BLOCKED`. Phase 2 verdict: `FAIL` pending repair.** Tasks 5.2 and 6.2 are reopened;
Tasks 7.2-7.5 and archive remain blocked. DS is authorized for one consolidated P2-GLM-01 repair:
red tests, private ingestion execution lease/heartbeat/fencing, expired-job reconciliation and focused
plus complete regression. Do not change GLM artifacts/assertions, broker visibility timeout, workflow
owner lease, Phase 1 index immutability, retrieval ranking, ordinary chat or frontend scope. After
Codex static review, GLM will resume from the failed waiting boundary before continuing S3-S8.

### Codex P2-GLM-01 Repair Static Review (2026-08-16)

- **P2-GLM-01-CX-01 - BLOCKED (P1 expired lease still treated as ownership):**
  `assert_execution_owner`, `heartbeat` and `finalize_job_terminal` validate the token and terminal
  state but not `execution_lease_expires_at > now`. Before Beat or a replacement task changes the
  token, an expired worker can therefore renew itself or commit chunks, activation and terminal state.
- **P2-GLM-01-CX-02 - BLOCKED (P1 check-then-write gaps):** `IngestionService.claim_build` performs
  its lease checks in transactions that end before `PaperIndexVersion.get_or_create` and before
  `job.save`. The initial ingestion run/status/event path and transient retry handoff also write after
  a separate ownership check. These paths do not satisfy the frozen same-transaction fence.
- **P2-GLM-01-CX-03 - BLOCKED (P1 recovery evidence can be erased):** `release_execution` may clear an
  already expired token, heartbeat and expiry. `execution_lost` then has no fact by which to recover an
  in-progress orphan. A transient handoff changes the job to `pending` with `attempt_count > 0`, while
  the current scanner only recovers never-started pending jobs with `attempt_count == 0`; loss of the
  retry publication can strand that job as well.
- **Missing controls:** existing tests reject a mismatched token and prove takeover, but do not hold the
  same token past expiry and attempt each protected side effect, do not pause between fence and write,
  and do not prove recovery after a released transient retry publication is lost.

**Drift verdict: `BLOCKED`.** The lease/heartbeat direction is approved, but the implementation does
not yet provide complete fencing. DS is authorized for one consolidated correction limited to
`ingestion_execution`, ingestion task transitions, `IngestionService.claim_build`, chunk/activation/
terminal fences and their first-party tests/evidence. Token match plus unexpired lease must be checked
inside the same transaction as every protected durable write. Preserve expired evidence for Beat and
make a lost transient retry publication recoverable. Do not modify GLM artifacts, frozen Phase 1
index semantics, workflow owner lease, broker visibility timeout, retrieval, chat, frontend or public
API. GLM remains paused until Codex approves the corrected package.

### Codex P2-GLM-01 Repair Re-review (2026-08-16)

- **P2-GLM-01-CX-01/02 and the expired-token portion of CX-03 - RESOLVED:** token plus unexpired
  lease checks are present in heartbeat, durable fences and terminalization; `claim_build`, initial
  run/status/events and transient state writes now use same-transaction fences. The new same-token
  expiry controls, ten real waiting-kill repetitions and 80-second heartbeat control are accepted.
- **P2-GLM-01-CX-04 - BLOCKED (P1 valid-release recovery fact erased):** `release_execution` still
  clears token, heartbeat and expiry for a valid non-terminal lease. The task calls it unconditionally
  in `finally`. If the heartbeat thread reports a transient database error, the task takes the
  fail-closed `hb.lost` exit; once the database recovers, `finally` can successfully clear all three
  fields while the job remains `parsing`. `execution_lost` explicitly requires either an expired lease
  or a released identity with a heartbeat, so this state is invisible to Beat and recreates the
  original permanent orphan. The current tests cover expired release and transient handoff, but not
  this valid-release path.

**Drift verdict: `BLOCKED`.** DS is authorized for one final narrow correction: non-terminal release
must preserve a timestamped recovery fact (or atomically move to another explicitly recoverable
state), terminalization remains the only path that clears all execution identity fields, and the
heartbeat-error exit must be proven recoverable without broker visibility timeout. Add a negative
control showing the old cleared-field shape is rejected by the recovery gate and a positive control
showing Beat leaves a fresh released worker alone, then redispatches it after the dynamic gate. Only
rerun `tests_p2_glm01`, the directly affected focused workflow suite, OpenSpec validation and evidence
consistency; the accepted real kill matrix, long-heartbeat proof and complete regressions need not be
rerun because this correction is local and introduces no migration/API change. GLM remains paused.

### Codex P2-GLM-01 Final Repair Approval (2026-08-16)

- **P2-GLM-01-CX-04 - RESOLVED:** valid non-terminal release now clears token/expiry but preserves a
  freshly stamped heartbeat recovery fact; expired release remains rejected, and terminalization is
  the only path that clears all three execution identity fields. The production task's real
  heartbeat-error path exits fail closed, remains invisible to Beat inside the dynamic gate, becomes
  redispatchable after the gate, and converges under a replacement claim.
- **Independent spot verification - PASS:** Codex reran the three `HeartbeatErrorRecoveryTests` in a
  Docker PostgreSQL test container and obtained 3/3 PASS. The fixed artifact verifier and OpenSpec
  strict validation also pass. A separate whole-module spot run encountered only a test-environment
  checkpoint-table setup failure in the pre-existing privacy test; the accepted fixed-environment
  39/39 raw artifact and the independently passing CX-04 class establish that this is not a lease
  behavior regression.
- **Four-proof gate - PASS:** production code, before/after raw evidence, accepted complete regression
  outputs and generated report/JSON agree. The ten real waiting-kill repetitions, 80-second heartbeat
  control, 304 backend regression and 156 Phase 1/Stage B regression remain valid fixed-revision
  evidence and were correctly not rerun for the final local release-semantics correction.

**Drift verdict: `NO DRIFT`. P2-GLM-01 repair verdict: `PASS`.** Tasks 5.2 and 6.2 are closed again.
GLM is authorized to resume Task 7.2 from the failed waiting-boundary test using its original
independent assertions, then continue S3-S8 if that boundary passes. GLM must not modify production
code or reuse DS aggregate verdicts. DS stops; Tasks 7.3-7.5 and archive remain blocked until Codex
reviews GLM's completed independent evidence.

### Codex GLM Task 7.2 Evidence-Closure Review (2026-08-16)

- **Behavioral acceptance - PROVISIONALLY ACCEPTED:** S2 waiting-kill, S3 lost wakeup, S4 workflow
  lease expiry, S5 broker interruption, S6 outcome/scope gates, S7 leakage and S8 frontend contract
  all have non-vacuous raw scenario evidence. The repaired ingestion execution lease converges in ten
  independent waiting-kill repetitions without relying on the one-hour broker visibility timeout.
- **P2-GLM-EV-01 - BLOCKED (runtime provenance):** `runtime-manifest.json` records LangGraph as
  `missing` and pgvector as blank even though the tested backend container has LangGraph 1.2.10,
  `langgraph-checkpoint-postgres` 3.1.2 and `psycopg-pool` 3.3.1, and PostgreSQL reports pgvector
  0.8.6. The collector used an unavailable module `__version__` and a malformed SQL quoting path;
  the verifier checks only that Git HEAD exists. Regenerate provenance from the actual tested
  containers using distribution metadata and fail closed on every required blank/missing version.
- **P2-GLM-EV-02 - BLOCKED (non-quiescent final snapshot):** `s5-record.json` captured queue depth zero
  but Redis `unacked == 11`, while the verifier ignores that field and the report labels it harmless
  without message-level evidence. The live environment later reached queue depth zero and unacked
  zero, which indicates a transient acceptance-cleanup issue rather than a durable workflow defect.
  Capture a bounded final drain snapshot and require both values to be zero; also prove a worker
  restart/drain does not create duplicate user-visible side effects.
- **P2-GLM-EV-03 - BLOCKED (verifier is not self-contained):** the S3 negative-control branch invokes
  Docker and `/tmp/glmr2_probe.py` during verification instead of reading a fixed eventlog artifact,
  and the stored `d_still_waiting_when_a_woke=false` field is ambiguous against the prose claim.
  Persist the authoritative D event and dependency timestamps, validate their ordering offline, and
  remove or rename the ambiguous field. Add mutations for missing eventlog, early resume, blank
  runtime versions and nonzero queue/unacked state.

**Drift verdict: `NO DRIFT` for production behavior; Task 7.2 remains `BLOCKED` for evidence closure.**
This is one GLM-only evidence correction. Do not modify production code, DS tests or accepted scenario
assertions, and do not rerun S2-S8. Regenerate only runtime provenance, final broker-quiescence and
fixed S3 negative-control evidence, then update the offline verifier, report consistency, mutations
and detached manifest. Tasks 7.3-7.5 remain blocked until this package passes Codex review.

### Codex GLM Task 7.2 Final Acceptance (2026-08-16)

- **P2-GLM-EV-01 - RESOLVED:** fixed provenance records PostgreSQL 16.14, pgvector 0.8.6, Redis
  7.4.10, Celery 5.6.3, LangGraph 1.2.10, `langgraph-checkpoint-postgres` 3.1.2 and
  `psycopg-pool` 3.3.1 from the actual tested containers using distribution metadata and parsed
  database output. The tested GLM worker records fake embedding and non-eager Celery.
- **P2-GLM-EV-02 - RESOLVED:** the final bounded quiescence snapshot starts and ends with queue depth
  zero and Redis unacked zero. Restart/drain changes report, RAG commit, active-build and completion-
  event counts by zero. The earlier `unacked == 11` remains truthful historical scenario evidence,
  not the final acceptance state.
- **P2-GLM-EV-03 - RESOLVED:** the evidence and report verifiers are offline and self-contained. The
  fixed S3 negative control proves the earliest resume timestamp follows the latest job/dependency
  terminal timestamp; the ambiguous boolean was removed. Missing/early evidence and all required
  provenance/quiescence/report mutations fail closed.
- **Independent Codex verification - PASS:** Codex reran the offline evidence verifier and report-
  consistency verifier, the thirty mutation/read-only/clean-state controls, detached manifest
  verification (124/124) and OpenSpec strict validation (15/15). Current Git production/untracked
  paths match the pre-closure runtime manifest; GLM changed no production code.

**Drift verdict: `NO DRIFT`. Task 7.2 verdict: `PASS`. Phase 2 technical verdict: `PASS`.** The
original GLM FAIL and its P2-GLM-01 repair trail remain preserved. Tasks 7.2-7.4 are approved. Only
Task 7.5 remains: merge the approved deltas, archive with OpenSpec CLI, revalidate current specs,
perform public/repository hygiene checks and prepare a clean Phase 2 commit/PR. No Phase 3 work may
start before that archive/PR boundary is complete.

### Codex Phase 2 Archive Closure (2026-08-16)

- OpenSpec CLI merged three added and three modified requirements into current specs and archived the
  change as `2026-08-16-make-research-workflow-durable`.
- Post-archive strict validation, public/repository hygiene, secret scanning and the clean Phase 2
  commit/PR boundary are the authoritative Task 7.5 release evidence.

**Phase 2 final verdict: `PASS`. Task 7.5 is complete.** Phase 3 remains a separate OpenSpec change.
