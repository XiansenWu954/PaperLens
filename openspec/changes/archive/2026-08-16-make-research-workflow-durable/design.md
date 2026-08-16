## Context

See `proposal.md` for motivation. The current `project_workflow` compiles LangGraph without a
checkpointer and executes one `ainvoke` inside one Celery task. Its ingestion node creates jobs
directly and then advances immediately to RAG. `ProjectRun` has no waiting/partial states, lease,
dependency records or lifecycle timestamps; events and reports have no workflow-level uniqueness.

Phase 1 is the required foundation: `IngestionService` now supplies idempotent project jobs, shared
global builds, immutable published index versions and atomic activation. Stage B scope, evidence,
citation, capability and EventPublisher contracts remain frozen. PostgreSQL, Redis and Celery are
the production/demo path; fake providers remain the normal deterministic test path.

## Goals / Non-Goals

**Goals:**

- Release workers while ingestion is pending and recover the same graph after process or broker loss.
- Establish one database-backed owner for workflow progression and idempotent side effects.
- Make ingestion completion a persisted dependency fact, not an assumption based on enqueue success.
- Enforce deterministic evidence gates before full or partial report persistence.
- Make timing, ownership, checkpoint safety and duplicate suppression independently auditable.

**Non-Goals:**

- Do not change ordinary Chat routing, tool budgets, prompts, MCP or Function Calling contracts.
- Do not change dense/FTS/sparse retrieval, ranking, query planning or RAG quality thresholds.
- Do not implement SSE replay/cursors, broad Pinia refactoring or a new workflow UI.
- Do not use live DeepSeek or BGE-M3 results as this phase's quality acceptance signal.
- Do not add LangChain, a second workflow engine or a framework-specific public API.

## Decisions

### 1. ProjectRun is the product workflow identity; LangGraph is the checkpoint engine

`ProjectRun.id` becomes the LangGraph `thread_id` as a decimal string. The graph compiles with a
PostgreSQL checkpointer and never creates its own public workflow identity. `ProjectRun` adds
`workflow_phase`, owner lease fields, resume count and lifecycle timestamps. The public status state
machine is:

`pending -> running -> waiting_ingestion -> running -> done | partial | error`.

`workflow_phase` records the last committed graph boundary independently of user-facing status.
This lets operations diagnose a waiting or failed run without exposing checkpoint internals.

Alternative rejected: treating Celery task IDs as workflow identity. Task IDs change on retry and
reconciliation and cannot provide stable report or dependency ownership.

### 2. Use the official PostgreSQL checkpointer with explicit one-time setup

Pin compatible dependency ranges:

- `langgraph>=1.2.10,<1.3`
- `langgraph-checkpoint-postgres>=3.1.2,<3.2`
- `psycopg-pool>=3.3,<4`

An idempotent `setup_langgraph_checkpoints` management command calls the saver setup operation. The
backend container runs it after `manage.py migrate` and before Daphne. Celery workers only verify
readiness and never run DDL. Health reports `durable_workflow_enabled` and
`workflow_checkpointer_ready` without connection strings or table contents.

Use a dedicated PostgreSQL connection/pool rather than Django's connection object because the saver
requires its own connection lifecycle and autocommit behavior. The initial implementation uses the
default PostgreSQL schema because the selected Python saver does not expose a stable schema option;
table names and package version are recorded in deployment evidence.

Alternative rejected: an in-memory saver outside tests because worker restart would discard state.

### 3. Keep checkpoint state minimal and reload sensitive data

The graph state contains only IDs, bounded lists of IDs, counts, status/error codes, result hashes,
decision flags and report ID. It does not contain the question, rewritten queries, search payloads,
PDF locations, full-text evidence, report draft or raw errors.

Nodes reload the question from `ProjectRun`, dependencies through project-scoped relations and active
evidence through `ProjectScopeResolver`. The search node normalizes and upserts global paper records;
checkpoint state carries only their paper IDs, and the add node creates project memberships from
those IDs. The draft report is persisted in a private `ProjectRun.draft_output` field excluded from
serializers, events and logs. It is covered by retention and sentinel tests. Checkpoint audit is
stricter: neither the draft, paper payloads nor user questions may enter LangGraph tables.

Alternative rejected: serializing full node output into graph state because it duplicates sensitive
content in opaque framework tables and weakens Stage B auditability.

### 4. Model ingestion readiness explicitly

Add `ProjectWorkflowDependency` with unique `(run, paper)`, project-consistent relations to the paper
and optional `PaperIngestionJob`, status, terminal timestamp and stable error code. It distinguishes:

- `ready`: an active compatible full-text index existed when the dependency was established; its
  verification time is also its terminal timestamp;
- `pending`: the workflow created or reused a non-terminal project ingestion job;
- `succeeded`: the linked job reached `embedded` with active full text;
- `failed`: the linked job reached `failed`;
- `unavailable`: no safe ingestible URL and no active full text.

The enqueue node uses `IngestionService` and the same HTTPS candidate and digest filename rules as
Phase 1. It never directly creates a job. `PaperIngestionJob.terminal_at` is set exactly once on
`embedded` or `failed`; dependency synchronization is idempotent and records the run's maximum
terminal timestamp.

Alternative rejected: querying all project jobs by status on every resume because unrelated jobs
would control the workflow and cross-run causality would be ambiguous.

### 5. Interrupt while waiting; terminal commits request resume

The fixed graph is:

`plan_expansion -> search_sources -> add_candidates -> enqueue_ingestion -> await_ingestion ->`
`query_hybrid_rag -> critic -> draft_report -> persist_report`.

`await_ingestion` refreshes dependency rows. If any is pending, it records `waiting_ingestion`, emits
one deduplicated wait event and calls LangGraph `interrupt()` with IDs/counts only. The Celery task
returns normally so no worker slot is held. Resume uses the same thread ID and a bounded safe command.

When an ingestion job commits `embedded` or `failed`, `transaction.on_commit` schedules dependency
synchronization and a resume request for affected waiting runs. This hook is a wakeup, not the owner:
the resume task must acquire the run lease and re-read all authoritative state.

Alternative rejected: graph-side polling, which holds or repeatedly schedules one task per waiting
run and still has a failure window around worker termination.

### 6. Use a database lease plus deduplicated side effects

A workflow task acquires the run with `select_for_update`. It may proceed when no unexpired owner
exists or when it already owns the supplied token. A random opaque token and 300-second expiry are
stored; every committed node boundary renews the lease. The token is never serialized publicly.
Completion, partial completion, failure or interrupt releases the owner.

Workflow start/resume tasks use late acknowledgement and worker-loss rejection. Their inputs contain
only the run ID and a stable wakeup reason; the owner token is generated after the database lock and
never enters the broker payload. Redelivery therefore re-enters the same lease/checkpoint boundary.

`ProjectRunEvent.dedupe_key` is nullable and unique with run when present. Logical keys use stable
run/node/attempt-free identities such as `node:query_hybrid_rag:committed`; they do not include
questions or payload hashes. `ReportVersion.source_run` is nullable one-to-one. Project membership
and ingestion already have their own Phase 1 uniqueness boundaries.

RAG may be recomputed after a crash before commit, but `first_rag_at`, the committed retrieval event
and result identity are written atomically. `persist_report` uses `get_or_create(source_run=run)` in a
transaction and never overwrites an existing workflow report.

Alternative rejected: Redis locks because database state, checkpoint state and report uniqueness
would not share one durable authority.

### 7. Add immediate wakeup plus Celery Beat reconciliation

Add one Celery Beat service and a periodic task every 15 seconds. It selects bounded pages of:

- `waiting_ingestion` runs whose dependencies are all terminal;
- `running` workflow runs whose owner lease expired;
- pending workflow runs that were created but not started.
- workflow dependencies whose project ingestion job is still pending with no executed attempt after
  the enqueue grace period.
- workflow dependencies whose non-terminal ingestion job lost its executing worker after an attempt
  began, as proven by an expired database execution lease rather than status age alone.

Rows are claimed with database locking and `skip_locked` where supported. Reconciliation may enqueue
the existing idempotent ingestion job or a workflow resume task, but never fetches/parses/embeds or
advances graph state itself. Its target is to detect the missing wakeup and enqueue compensation
within 30 seconds. Terminal completion is measured separately and may wait for the approved
300-second owner lease to expire; it must still converge to a stable terminal state with ownership
released.

An ingestion attempt has a separate, private database execution identity from the workflow owner.
`PaperIngestionJob` stores an execution token, heartbeat timestamp and execution-lease expiry. A
worker claims the lease atomically, renews it at a measured interval while parsing/embedding and
checks the same token before every durable index, activation, terminal-state or event side effect.
Beat may redispatch an idempotent job only after that execution lease expires. A stale worker whose
token no longer matches must exit without publishing, activating an index or terminalizing the job.
The token and lease fields are internal and never enter API, events, logs, checkpoints or Celery
result payloads. Status age alone is not evidence that a worker died because legitimate PDF parsing
can be long-running.

Token equality is necessary but not sufficient ownership evidence. Every heartbeat and durable
side-effect fence MUST atomically require both the expected token and an execution-lease expiry later
than the database transaction's current time. An expired attempt cannot revive its own lease, clear
the expired evidence, create or attach a build, change job/run state, persist chunks, activate an
index, publish an event or terminalize the job before a replacement worker claims it. The ownership
check and protected write MUST share one transaction and job-row lock; check-then-write across
separate transactions is not fencing. A voluntary transient-retry handoff must leave enough persisted
state for reconciliation to recover a lost retry publication without relying on broker visibility
timeout.

The default execution lease is 60 seconds and the default heartbeat interval is 10 seconds, exposed
as `PAPERLENS_INGESTION_EXECUTION_LEASE_SECONDS` and
`PAPERLENS_INGESTION_HEARTBEAT_SECONDS`. Validation requires positive values and heartbeat strictly
below the lease. With the existing 15-second Beat interval, the fault gate derives its maximum
compensating redispatch latency from runtime configuration as lease + Beat interval + 5 seconds of
scheduler tolerance; it must not hardcode a passing duration. These settings affect attempt recovery
only and do not replace the 300-second workflow owner lease.

Alternative rejected: immediate events only, because a transaction can commit successfully while
broker publication fails, leaving a durable run with no future wakeup.

### 8. Enforce deterministic full/partial/error policy before critic

After all dependencies are terminal, set `last_ingestion_terminal_at`; immediately before the first
RAG call set `first_rag_at` in a transaction and require strict timestamp ordering. Retrieval uses
only active compatible chunks from project-visible, non-excluded papers.

The deterministic gate computes successful, failed and unavailable dependencies plus resolved,
answer-bound full-text citations:

- all usable and bound: candidate `done`;
- some failed/unavailable but at least one usable and bound: candidate `partial`;
- no usable full text or unbound/unresolved citations: `error`, no report.

The critic may downgrade a candidate result to error. It cannot upgrade error, convert metadata to
full text or remove the required partial evidence-gap disclosure. The report draft stays private
until these checks pass.

### 9. Preserve API paths and keep frontend changes additive

The existing POST endpoint still returns 201 for an accepted run. When disabled or checkpoint storage
is unavailable it returns a stable service-unavailable response with `workflow_unavailable` and does
not create an executing run. ProjectRun serialization adds safe phase/timestamp/resume/dependency/
report fields while keeping existing required fields.

The Run Inspector adds labels for waiting and partial status, phase, dependency counts and report
link. It does not add SSE, reconnect or a new store architecture; those remain Phase 4.

### 10. Fail closed and roll back by disabling new starts

`PAPERLENS_DURABLE_WORKFLOW_ENABLED` defaults on when `DATABASE_URL` selects PostgreSQL and defaults
off for SQLite fallback; `.env.example` and Docker Compose set it explicitly to `1`. Tests may enable
it with a verified PostgreSQL fixture. Disabled mode rejects new starts and leaves existing records
inspectable; it never invokes the legacy graph. Rollback deploys previous code only after disabling
new starts and draining or marking active durable runs with a stable operator code. Checkpoint tables
are retained for audit.

### 11. Make independent acceptance artifacts self-contained and quiescent

An independent acceptance package is a release artifact, not a pointer to a still-running test
environment. Its verifier MUST derive every verdict from immutable files in the fixed artifact
directory. It MUST NOT call Docker, Redis, PostgreSQL, a temporary script or another live service
while verifying an already captured package. Runtime queries used during execution are written to
scenario-specific JSON or raw logs first, and the detached artifact manifest is generated last.

Runtime provenance is collected from the containers that actually execute the accepted scenarios.
Python package versions use distribution metadata rather than module `__version__` attributes;
PostgreSQL extension versions use a successfully parsed database query. Missing, blank or
unparseable required versions fail the package. At minimum the package records PostgreSQL, pgvector,
Redis, Celery, LangGraph, `langgraph-checkpoint-postgres`, `psycopg-pool`, the embedding provider and
eager/non-eager mode.

Acceptance ends only after a bounded quiescence check. The Celery queue depth and Redis transport
`unacked` state MUST both reach zero after the tested workflows are terminal and workers have had a
bounded drain interval. A nonzero value is not automatically a product defect, but it blocks a clean
`PASS` artifact until every message is decoded to task name/ID and associated run/job, its idempotent
terminal behavior is proven, and a final zero-state snapshot is captured. Restarting workers for
that drain check must not create another report, RAG commit, active build or terminal event.

Negative controls use explicit timestamps and authoritative terminal states. Ambiguous booleans such
as `still_waiting` are not accepted when their value or name conflicts with the prose claim. The
verifier must reject missing scenario files, missing runtime fields, non-quiescent broker state,
duplicate report summary blocks and any report/JSON mismatch. Evidence-only corrections do not
authorize production changes or rerunning already accepted long fault matrices.

## Risks / Trade-offs

- **[Checkpointer package tables are outside Django migrations]** -> Use a version-pinned, idempotent
  setup command, readiness check and real fresh-database deployment test.
- **[Interrupt nodes restart from their beginning]** -> Perform no external side effect before
  `interrupt()` without a database idempotency key; refresh dependencies at the top of the node.
- **[Lease expiry can overlap a slow node]** -> Renew at graph boundaries, keep side effects behind
  database uniqueness, and fault-test expiry during each boundary.
- **[Beat introduces another process]** -> Keep it stateless, bounded and enqueue-only; LangGraph and
  ProjectRun remain authoritative.
- **[Private workflow storage contains report/search material]** -> Exclude it from APIs/events/logs,
  sanitize it, test sentinel leakage and define later retention independently of checkpoints.
- **[Partial reports can be overinterpreted]** -> Persist structured dependency gaps and require
  resolved bound citations for every supported claim.

## Migration Plan

1. Add pinned dependencies and data migrations with nullable/additive fields; backfill no workflow
   dependencies and leave existing runs unchanged.
2. Install checkpoint tables with the management command in a fresh PostgreSQL integration database;
   verify health before enabling starts.
3. Deploy backend and worker code with the feature flag disabled, then start Celery Beat.
4. Enable the flag and run one deterministic workflow through wait/resume/complete.
5. Run worker restart, lost-wakeup and duplicate-delivery gates before public use.
6. To roll back, disable new starts, stop Beat/resume tasks, preserve ProjectRun/dependency/checkpoint
   rows, and do not route work to the legacy implementation.
