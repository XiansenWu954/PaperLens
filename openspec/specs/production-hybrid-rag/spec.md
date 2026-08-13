# Spec: production-hybrid-rag

## Purpose

PaperLens supports a V3 production-aligned project RAG path:

- PostgreSQL + pgvector vector storage for demo/deployment.
- SQLite-compatible fallback for local unit tests.
- Configured embedding provider (BGE-M3 default) with index version tracking.
- Postgres FTS + dense vector search + RRF fusion.
- Celery-backed PDF ingestion jobs with page/section/offset metadata.
- Explicit LangGraph workflow for long research expansion.
- Deterministic 30+ RAG quality evaluation metrics.

The originating change has been completed and archived under
`openspec/changes/archive/add-production-hybrid-rag-v3`.
## Requirements
### Requirement: Configured Embedding Provider And Index Version

The current default embedding provider MUST be defined by one canonical environment/settings
contract and every indexed chunk MUST record enough metadata to prevent incompatible vectors
from being mixed.

#### Scenario: Current default provider

- **WHEN** the backend starts without a test override
- **THEN** the effective provider and model MUST match `.env.example`, Django settings, health
  output, and current architecture documentation.

#### Scenario: Deterministic test provider

- **WHEN** the normal offline test suite runs
- **THEN** it MUST use a deterministic fake embedding provider
- **AND** real BGE-M3 loading MUST require an explicit real-model test flag.

#### Scenario: Embedding provider change

- **WHEN** the default embedding model, dimension, or encoding behavior changes
- **THEN** a new embedding version MUST be created
- **AND** incompatible old and new vectors MUST NOT be queried as one index
- **AND** a measured reindex plan and rollback path MUST be approved through OpenSpec.

### Requirement: Safe PDF Acquisition

Every remote or uploaded PDF MUST pass bounded acquisition validation before parsing or
embedding begins.

#### Scenario: Stream a valid upload

- **GIVEN** a project paper and a multipart `file` whose content begins with `%PDF-`
- **WHEN** the upload is no larger than the configured limit, default 50 MiB
- **THEN** the service MUST stream it to a content-addressed media path while computing SHA-256
- **AND** it MUST NOT read the whole request into application memory
- **AND** the stored path MUST be derived from server values, never the submitted filename.

#### Scenario: Reject an invalid upload

- **WHEN** an upload is empty, oversized, lacks PDF magic, or exceeds parser page/text limits
- **THEN** no Celery task or index version MUST become active
- **AND** the API MUST return a stable validation code without file content or parser details.

#### Scenario: Fetch a public PDF URL

- **GIVEN** an HTTPS URL whose DNS answers are all globally routable
- **WHEN** the service fetches the resource
- **THEN** it MUST disable environment proxy inheritance, pin the validated destination for the
  connection while preserving TLS hostname verification, stream at most 50 MiB, and require PDF
  magic before returning bytes
- **AND** it MUST manually follow at most five HTTPS redirects, revalidating every destination.

#### Scenario: Reject an SSRF destination

- **WHEN** the initial URL, any DNS answer, connected peer, or redirect targets loopback, private,
  link-local, multicast, unspecified, reserved, carrier-grade NAT, or another non-global address
- **THEN** the fetch MUST fail with `unsafe_pdf_url`
- **AND** no request to that destination MUST be sent.

### Requirement: Versioned Atomic Paper Index

Each paper MUST use explicit immutable index versions and at most one active version.

#### Scenario: Build and activate a new version

- **WHEN** parsing produces non-empty chunks and embedding produces exactly one normalized vector
  of the configured dimension for every chunk
- **THEN** all chunks MUST be associated with a `building` index version
- **AND** one transaction MUST activate that version and supersede the previous active version
- **AND** retrieval MUST read only the active version compatible with current embedding metadata.

#### Scenario: Replacement fails before commit

- **GIVEN** a paper has an active index
- **WHEN** download, parse, embedding, cardinality validation, database insertion, or activation fails
- **THEN** the previous active index MUST remain queryable
- **AND** the attempted version MUST be failed or safely resumable
- **AND** an empty or partial version MUST NOT become active.

#### Scenario: Migrate existing chunks

- **WHEN** the versioning migration runs against existing `Text` rows
- **THEN** rows MUST be grouped by paper and compatible embedding version into index versions
- **AND** the newest compatible group MUST become active while incompatible groups remain
  superseded
- **AND** every migrated `Text` row MUST reference an index version before the field becomes
  non-null.

#### Scenario: Preserve published index versions

- **GIVEN** a paper has an `active`, `superseded`, or `failed` index version
- **WHEN** a compatibility ingestion path or a new ingestion request writes replacement chunks
- **THEN** it MUST NOT insert, update, or delete chunks belonging to those published versions
- **AND** new chunks MUST be written only to a dedicated `building` version
- **AND** replacement cleanup MUST be scoped to that `building` version
- **AND** the previous active and superseded versions MUST remain available for rollback and audit.

#### Scenario: Deterministic legacy migration

- **WHEN** legacy chunks are assigned to index versions
- **THEN** grouping and active-version selection MUST depend only on explicit migration inputs and
  persisted data
- **AND** the migration MUST NOT initialize or download an embedding model
- **AND** provider initialization failure MUST NOT silently change the selected active version
- **AND** the same legacy fixture and configuration MUST produce the same version identities and
  lifecycle states on every run.

#### Scenario: Preserve ingestion audit linkage

- **WHEN** a project ingestion job references a global index version
- **THEN** normal replacement, supersession, or cleanup MUST NOT silently erase that relationship
- **AND** deletion behavior MUST be enforced by a database constraint or an explicitly approved
  retention policy.

### Requirement: Idempotent Ingestion Execution

Project ingestion requests and global paper index builds MUST be idempotent across concurrent API
calls, projects, Celery workers, retries, and task redelivery.

#### Scenario: Client repeats an ingestion request

- **GIVEN** the same project, paper, pipeline signature, and `Idempotency-Key`
- **WHEN** the upload or URL-ingest endpoint is called again
- **THEN** it MUST return the existing project ingestion job with `deduplicated=true`
- **AND** it MUST NOT enqueue another Celery build
- **AND** a new request MUST return 201 while a reused request returns 200.

#### Scenario: Different requests contain the same PDF

- **WHEN** concurrent jobs obtain the same paper, file SHA-256, and pipeline signature
- **THEN** they MUST converge on one global index version
- **AND** ten concurrent requests MUST produce at most one active version and one chunk set.

#### Scenario: Transient worker failure

- **WHEN** acquisition receives a timeout, connection failure, HTTP 408, 429, or 5xx
- **THEN** Celery MUST retry at most three times with exponential backoff and jitter
- **AND** redelivery MUST resume or reuse the same build rather than duplicate it.

#### Scenario: Permanent ingestion failure

- **WHEN** validation, unsafe URL, non-retryable 4xx, empty parse, vector dimension, or chunk/vector
  cardinality validation fails
- **THEN** the job MUST enter `failed` without automatic retry
- **AND** persisted and emitted errors MUST contain only a stable code, safe copy, and error hash.

### Requirement: Ingestion Lifecycle Observability

Ingestion MUST expose project-scoped progress without exposing sensitive source data.

#### Scenario: Lifecycle progression

- **WHEN** a job progresses normally
- **THEN** its states MUST follow `pending`, `downloading`, `parsing`, `embedding`, `committing`,
  `embedded`
- **AND** the corresponding allowlisted events MUST carry project, run, paper, job, request, task,
  status, count, and duration identifiers when available.

#### Scenario: Safe failure visibility

- **WHEN** a job fails
- **THEN** project APIs and events MUST expose `failed`, a stable `error_code`, retryability, and a
  fixed user-facing message
- **AND** they MUST NOT expose the URL, filename path, PDF text, prompt, embedding content, API key,
  connected address, or raw exception.
