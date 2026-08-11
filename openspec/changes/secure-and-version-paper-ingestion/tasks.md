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

- [ ] 2.1 Add `PaperIndexVersion` with building/active/superseded/failed lifecycle, source and pipeline
  identity, embedding/parser metadata, safe failure fields, timestamps, identity uniqueness, and a
  PostgreSQL partial unique constraint for one active version per paper.
- [ ] 2.2 Add nullable `Text.index_version`, backfill versions by paper and embedding identity, select
  only the newest current-compatible group as active, verify complete assignment, make the field
  non-null, and replace `(paper, chunk_index)` uniqueness with `(index_version, chunk_index)`.
- [ ] 2.3 Extend `PaperIngestionJob` with version reference, idempotency/source identity, lifecycle
  states, attempt/file metrics, error code, and retryability while preserving existing rows and API
  fields.
- [ ] 2.4 Run forward/backward migration tests on empty, clean current, mixed-version, and failed
  legacy fixtures; document why production rollback uses a corrective forward migration after new
  versions exist.

## 3. Safe PDF Acquisition

- [ ] 3.1 Implement URL normalization, globally-routable DNS validation, injected resolver/transport,
  validated-IP connection pinning, TLS hostname and peer verification, manual redirect validation,
  `trust_env=False`, timeout, byte limit, SHA-256, and PDF-magic checks in `SafePdfFetcher`.
- [ ] 3.2 Implement streamed multipart handling through temporary files and atomic content-addressed
  rename; sanitize display names and delete every partial artifact on failure.
- [ ] 3.3 Define stable acquisition exception codes and retry classification; ensure logs, API, task
  events, and model-visible tool results never contain URL, peer address, local path, bytes, or raw
  exception text.
- [ ] 3.4 Run the acquisition suite with the Stage B outbound network guard installed and add a
  separate local canary proving blocked connections cannot reach the original socket.

## 4. Atomic Build And Celery Execution

- [ ] 4.1 Implement `IngestionService` request-key/build-key derivation, scoped job get-or-create,
  global version claim/reuse, and structured queued/reused/upload-required responses.
- [ ] 4.2 Refactor parsing/chunking/embedding to write only to a building version and validate non-empty
  output, vector cardinality, finite normalized values, configured dimension, and embedding metadata.
- [ ] 4.3 Implement the short activation transaction that locks paper/version state, verifies persisted
  chunks, supersedes the previous active row, and activates exactly one new version without deleting
  rollback data.
- [ ] 4.4 Refactor Celery ingestion to use late acknowledgement, worker-loss rejection, explicit
  transient retry with three attempts/backoff/jitter, permanent failure handling, and propagation to
  all project jobs attached to the build.
- [ ] 4.5 Prove non-eager Redis/Celery execution, worker restart, task redelivery, concurrent request
  convergence, failed activation rollback, and test-database cleanup using real PostgreSQL/pgvector.

## 5. Project API, Agent Tool, And Frontend

- [ ] 5.1 Route existing upload and URL-ingest views through `IngestionService`, implement optional
  `Idempotency-Key`, 201/200 semantics, additive serializers, and the scoped failed-job retry endpoint.
- [ ] 5.2 Update `ProjectScopeResolver`, RAG, read, compare, citation resolution, and Python fallback to
  consume only the compatible active index; add stale/building/superseded negative controls with an
  active positive control.
- [ ] 5.3 Extend `add_papers_to_project` to queue no more than three newly created memberships with
  candidate HTTPS PDF URLs and return separate added, queued, reused, deferred, and upload-required
  collections without performing ingestion in the Agent process.
- [ ] 5.4 Extend EventPublisher schemas and ingestion logs with safe state/count/duration/identity
  fields, then rerun opaque-sentinel and correlation-ID audits over REST, Celery, database events,
  logs, SSE, tool results, and model context.
- [ ] 5.5 Update frontend types, Pinia state, Evidence Board status rendering, duplicate-command
  disabling, safe error copy, retry, upload-required, and indexed metadata; keep unrelated workspace
  panels usable during ingestion.

## 6. DS Verification And Evidence

- [ ] 6.1 Run focused model, migration, fetcher, ingestion, project scope, API, Agent tool, event, and
  frontend tests; all new red cases must turn green without weakening assertions or enabling network.
- [ ] 6.2 Run Docker PostgreSQL full backend regression, migration drift check, Django check, frontend
  Vitest, production build, OpenSpec strict validation, secret scan, and forbidden-public-path audit.
- [ ] 6.3 Run one real HTTPS scholarly PDF through non-eager Redis/Celery and BGE-M3, verify exact
  chunk/vector counts and active switch, then repeat the same request to prove deduplication.
- [ ] 6.4 Generate machine-readable case, manifest, migration, concurrency, retry, event-schema,
  sensitive-scan, frontend, command-output, and report-consistency artifacts from one fixed run
  directory; mutation tests must fail when counts, verdict, or required evidence are altered.
- [ ] 6.5 Submit the four-part DS report with exact files, case IDs, commands, pass/fail counts,
  durations, Docker state, and remaining risks; stop before GLM work and do not mark this change PASS.

## 7. Independent Acceptance And Archive

- [ ] 7.1 Codex performs static review of implementation, migration, trust boundaries, public API,
  artifacts, and DS report; unresolved P0/P1 or unverifiable claims block GLM handoff.
- [ ] 7.2 GLM adds an independent test/audit layer without modifying production code, independently
  recalculating SSRF, concurrency, active-version, rollback, redelivery, project-scope, event-leak,
  frontend-contract, and report-consistency results from raw evidence.
- [ ] 7.3 DS fixes Codex/GLM findings in production and first-party tests; GLM reruns its original
  assertions unchanged except for Codex-approved specification corrections.
- [ ] 7.4 Codex approves only when code, raw artifacts, complete tests, and generated report agree and
  all Gate B invariants pass; otherwise record FAIL or PASS WITH KNOWN RISKS with ownership.
- [ ] 7.5 Merge capability deltas, archive the OpenSpec change with the CLI, update only public metrics
  revalidated in this run, and merge the independent PR before starting durable workflow work.
