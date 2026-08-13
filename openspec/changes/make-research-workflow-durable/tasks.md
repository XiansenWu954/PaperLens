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

- [ ] 2.1 Add explicit non-default red tests proving the current graph performs RAG before ingestion
  terminal state, has no persistent checkpoint/wait boundary and directly creates ingestion jobs.
- [ ] 2.2 Add red tests for duplicate start/resume/report/event side effects, owner lease conflict and
  expiry, lost wakeup, disabled/unready checkpointer and stable fail-closed API errors.
- [ ] 2.3 Add red tests for dependency scope and lifecycle: ready active evidence, pending ingestion,
  partial failure, all failure, unavailable URL, excluded/unlinked/foreign paper and terminal times.
- [ ] 2.4 Add red tests scanning LangGraph checkpoint/pending-write tables, ProjectRun private state,
  events, logs, API and Celery results for question, URL, path, full text, excerpt, key and opaque
  exception sentinels; include non-empty positive and negative controls.
- [ ] 2.5 Add red tests for strict `first_rag_at > last_ingestion_terminal_at`, one report per run,
  one committed RAG event and no report when full-text citations are unresolved or unbound.
- [ ] 2.6 Produce one fixed PostgreSQL/pgvector manifest, per-case JSON, raw output, network guard,
  secret scan and report-consistency artifacts; distinguish expected pre-fix failures from errors,
  vacuous passes and unexpected results.
- [ ] 2.7 Submit the four-part DS Batch A report and stop. Do not modify production code, current
  specs, GLM assertions or task checkboxes beyond 2.x before Codex static review.

## 3. Batch B — Durable Data And Checkpoint Foundation

- [ ] 3.1 Pin the approved LangGraph/PostgreSQL saver and psycopg pool dependency ranges; add an
  idempotent `setup_langgraph_checkpoints` command and backend startup/readiness integration while
  proving Celery workers never execute checkpoint DDL.
- [ ] 3.2 Add ProjectRun workflow lifecycle, lease, safe private state and timestamps; migrate existing
  rows without changing their existing status or public behavior.
- [ ] 3.3 Add ProjectWorkflowDependency with run/paper uniqueness, project consistency, optional job,
  terminal timestamp and stable error code; add PaperIngestionJob terminal timestamp.
- [ ] 3.4 Add nullable unique workflow ownership on ReportVersion and nullable per-run event dedupe key;
  prove ordinary reports and legacy events remain compatible.
- [ ] 3.5 Extend serializers, health output, settings and safe EventPublisher schemas with additive
  fields only; owner tokens, private state and checkpoint internals must remain absent.
- [ ] 3.6 Run migration forward/backward safety, fresh PostgreSQL checkpoint setup twice, model
  constraints, health, focused API and complete backend regression; submit the four-part report and
  stop for Codex review.

## 4. Batch C — Checkpointed Graph And Idempotent Ownership

- [ ] 4.1 Compile the fixed workflow graph with PostgreSQL checkpoint persistence and
  `thread_id=str(ProjectRun.id)`; checkpoint state must contain only approved scalar/ID fields.
- [ ] 4.2 Replace direct workflow job creation with Phase 1 IngestionService and create explicit
  dependency rows for ready, pending and unavailable papers.
- [ ] 4.3 Implement `await_ingestion` using LangGraph interrupt, releasing the owner and worker after a
  deduplicated waiting transition; resume must use the same thread identity.
- [ ] 4.4 Implement database owner acquisition/renewal/release with row locking and a 300-second lease;
  configure start/resume tasks with late acknowledgement and worker-loss rejection; valid-owner
  duplicates exit safely and expired owners resume from checkpoint.
- [ ] 4.5 Make node transitions, committed RAG identity and report persistence idempotent using event
  dedupe keys and ReportVersion source-run uniqueness.
- [ ] 4.6 Enforce the durable-workflow feature flag and checkpointer readiness at the existing endpoint;
  unavailable mode returns stable `workflow_unavailable` and never invokes the legacy graph.
- [ ] 4.7 Turn the corresponding Batch A tests green, run focused and full regression, preserve Stage B
  and Phase 1 invariants, submit the four-part report and stop for Codex review.

## 5. Batch D — Recovery, Partial Results And Minimal UI

- [ ] 5.1 Set ingestion terminal timestamps exactly once and use `transaction.on_commit` to synchronize
  affected dependencies and enqueue idempotent resume requests.
- [ ] 5.2 Add a stateless Celery Beat service and 15-second bounded reconciliation task for ready waiting
  runs, expired owners, pending starts and never-started pending dependency jobs; it may enqueue only
  existing idempotent work and must never execute ingestion or advance graph state.
- [ ] 5.3 Enforce strict timing so the first committed RAG begins after the latest dependency terminal
  timestamp and uses only current project-scoped active compatible full text.
- [ ] 5.4 Implement deterministic done/partial/error gates and critic downgrade-only behavior; partial
  reports must disclose failed/unavailable dependencies and unsupported runs must create no report.
- [ ] 5.5 Keep normalized search state as paper IDs, persist only the report draft in private
  non-serialized storage, and prove checkpoints, events, logs, API and Celery results contain no
  question, candidate payload, draft body or other prohibited content.
- [ ] 5.6 Add minimal frontend types and Run Inspector labels for waiting, partial, phase, dependency
  counts and report link; do not add SSE replay, reconnect or store decomposition.
- [ ] 5.7 Run focused backend/frontend tests and full regression, submit the four-part report and stop
  for Codex review.

## 6. Batch E — Real Fault And Concurrency Verification

- [ ] 6.1 On real PostgreSQL/Redis/non-eager Celery, run 20 concurrent duplicate start/resume requests
  and prove one owner, one report, one committed RAG event and no duplicate ingestion build.
- [ ] 6.2 Kill and restart workers at enqueue, waiting, resume and report persistence boundaries for 10
  repetitions each; every run must recover or reach an explicit stable terminal error.
- [ ] 6.3 Drop an immediate terminal wakeup and prove Beat reconciliation resumes the run within 30
  seconds; interrupt Redis briefly and prove durable database/checkpoint state remains consistent.
- [ ] 6.4 Verify all-success, partial-success, all-failed, existing-active, no-URL and cross-project
  scenarios with non-empty controls and strict timestamp ordering in every RAG-producing run.
- [ ] 6.5 Scan checkpointer tables, pending writes, workflow private state, ProjectRunEvent, logs, API,
  Celery results and frontend fixtures for sensitive and opaque sentinels; leakage must be zero.
- [ ] 6.6 Run Docker PostgreSQL full backend regression, frontend Vitest/build, migration drift,
  Django check, OpenSpec strict validation, network/secret scan and public-file audit.
- [ ] 6.7 Generate machine-readable manifests, raw commands, case results, database recomputation,
  timings, failure injection records and report-consistency mutation tests from one fixed revision;
  submit the final DS four-part report and stop before GLM.

## 7. Independent Acceptance And Archive

- [ ] 7.1 Codex performs static review and records exactly `NO DRIFT`, `DRIFT RESOLVED` or `BLOCKED`
  against proposal non-goals, Stage B, Phase 1, REST compatibility and all machine-readable evidence.
- [ ] 7.2 GLM independently reruns concurrency, worker kill, broker interruption, lost wakeup, lease
  expiry, timestamp, uniqueness, project scope, checkpoint leakage and frontend contract tests without
  modifying production code or relying on DS aggregate verdicts.
- [ ] 7.3 DS fixes Codex/GLM findings while GLM retains its original assertions unless Codex approves a
  specification correction; all repairs repeat focused and complete regression.
- [ ] 7.4 Codex declares only PASS, PASS WITH KNOWN RISKS or FAIL after code, raw artifacts, complete
  tests and generated report agree; unresolved P0/P1 blocks archive.
- [ ] 7.5 Merge capability deltas, archive the change with OpenSpec CLI, revalidate current specs,
  update only reverified public claims and submit a clean independent PR before Phase 3 starts.
