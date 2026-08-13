## Why

PaperLens can queue PDF ingestion, but the current path downloads arbitrary URLs, reads uploads
fully into memory, deletes the active index before replacement is known to be valid, and allows
duplicate Celery jobs to race. Search-added papers therefore remain metadata-only or can leave a
project without usable evidence after a failed replacement. This change makes ingestion safe,
idempotent, observable, and rollback-capable before durable workflows or live Agent evaluation
depend on it.

## What Changes

- Add a global, versioned paper index lifecycle so a complete new index becomes active atomically
  and the previous active index remains queryable until commit succeeds.
- Add streaming upload and HTTPS fetch validation with strict byte, redirect, address, PDF magic,
  parse, chunk, embedding-count, and vector-dimension limits.
- Add stable ingestion idempotency across projects and workers, bounded Celery retry policy, and an
  explicit failed-job retry operation.
- Expand ingestion states and safe progress events without exposing URLs, file contents, prompts,
  or raw exceptions.
- Queue at most three newly added papers with validated PDF URLs after an Agent add action; papers
  without an ingestible URL remain metadata-only and request user upload.
- Extend the Evidence Board to represent each ingestion phase and provide a clear retry action.
- Preserve the current upload, URL-ingest, job-list, project-paper, RAG, chat, and SSE paths.
- Do not change Agent routing, MCP, LangGraph workflow ownership, retrieval fusion, embedding model,
  or report generation in this change.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `production-hybrid-rag`: PDF acquisition, versioned atomic indexing, idempotency, retries, active
  index selection, and ingestion observability become release-gated behavior.
- `project-paper-library`: Project paper responses, Agent add behavior, ingestion states, and user
  retry behavior gain explicit metadata-only versus indexed semantics.

## Impact

- Backend models and migrations for global index versions and project-scoped ingestion requests.
- PDF ingestion, Celery task execution, project tools, serializers, upload/ingest endpoints, and
  project-scoped retrieval filters.
- Existing frontend API types, project store, Evidence Board controls, and ingestion progress UI.
- Docker PostgreSQL/Redis/Celery integration tests, security tests, migration tests, and local media
  storage. No new Agent framework or service is introduced.

Compatibility is maintained through additive response fields and preserved existing paths. The
rollback boundary is the prior active paper index and the Stage B baseline commit. Release requires
zero SSRF bypasses, one active version under ten concurrent identical requests, no empty active
index, exact chunk/vector cardinality, successful non-eager Celery redelivery, and complete backend
and frontend regression gates.
