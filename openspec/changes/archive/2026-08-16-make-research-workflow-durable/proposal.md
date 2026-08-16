## Why

The existing research expansion endpoint starts one Celery task that runs an uncheckpointed
LangGraph from start to finish. It creates ingestion jobs directly, continues into RAG before those
jobs are terminal, and has no durable owner or resume boundary, so worker loss or duplicate delivery
can produce premature retrieval, stalled runs, or duplicate reports. Phase 1 now provides safe,
idempotent ingestion, making durable workflow coordination the next dependency before retrieval and
live Agent quality work can be trusted.

## What Changes

- Make `ProjectRun` the durable workflow identity with explicit waiting, partial-completion, lease,
  resume, and lifecycle timestamps.
- Persist LangGraph checkpoints in PostgreSQL and use `ProjectRun.id` as the stable thread identity.
- Add project-scoped workflow dependencies that link papers to ingestion jobs and record terminal
  outcomes without storing source URLs, full text, prompts, or raw errors.
- Pause the graph after ingestion is enqueued and resume only when every dependency is terminal;
  enforce that RAG begins strictly after the last ingestion terminal timestamp.
- Route workflow ingestion through the Phase 1 `IngestionService`; Celery remains an idempotent work
  executor and does not become a second workflow owner.
- Add database-backed owner leases, deduplicated events and report ownership so duplicate delivery,
  worker restart, or reconciliation cannot create duplicate user-visible side effects.
- Resume immediately after ingestion commit and add a 15-second Celery Beat reconciliation fallback
  for lost wakeups and expired owners.
- Permit an explicitly marked partial report only when deterministic evidence and citation gates pass;
  zero usable full text or unresolved citations fail without creating a report.
- Keep `POST /api/projects/<id>/workflows/research-expand` and its 201 creation response compatible;
  extend run responses and the existing run inspector with additive lifecycle fields.
- Add a feature flag that fails closed with `workflow_unavailable`; it never falls back to the old
  non-durable execution path.

This change does not modify hybrid retrieval ranking, ordinary project Chat, SSE subscription
semantics, model selection, prompts, MCP, or live LLM quality thresholds.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `agent-orchestration`: Define durable checkpoint, wait/resume, ownership, idempotent node, and
  partial-result behavior for explicit LangGraph workflows.
- `project-workspace`: Define workflow run states, dependency records, report ownership, additive API
  fields, and deduplicated audit events.
- `production-hybrid-rag`: Require all workflow ingestion dependencies to be terminal before RAG and
  allow only active, compatible project evidence after resume.

## Impact

- Backend models and migrations for workflow lifecycle fields, dependencies, ingestion terminal time,
  report ownership, and event deduplication.
- LangGraph orchestration, Celery workflow tasks, ingestion terminal callbacks, periodic reconciliation,
  EventPublisher schemas, health reporting, serializers, and the existing workflow endpoint.
- New `langgraph-checkpoint-postgres` and `psycopg-pool` dependencies plus a one-time, idempotent
  checkpoint setup command executed after Django migrations.
- Docker Compose gains Celery Beat; the frontend receives additive run status and dependency summary
  fields with minimal run-inspector changes.
- The rollback boundary is `PAPERLENS_DURABLE_WORKFLOW_ENABLED=0`, which disables new workflow starts
  without invoking the legacy path. Existing ingestion, Chat, RAG, report, and MCP contracts remain.
- Release requires deterministic timing, concurrency, worker-restart, lost-wakeup, partial-failure,
  checkpoint-safety, full-regression, and independent GLM acceptance evidence.
