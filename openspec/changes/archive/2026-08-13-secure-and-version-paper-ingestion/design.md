## Context

See `proposal.md` for motivation. The current `PaperIngestionJob` has four coarse states and no
stable request identity. `download_pdf` trusts environment proxies and automatic redirects, upload
views read the whole file into memory, and `ingest_text(replace_existing=True)` deletes current
chunks before replacement insertion is guaranteed. `Text` is unique by `(paper, chunk_index)` and
there is no database identity for one complete paper index. Project tools can add metadata but do
not use one shared ingestion service.

Stage B contracts remain authoritative: project identity comes from trusted context, evidence must
resolve to the current project and active compatible chunks, and every outward event passes through
`EventPublisher`. PostgreSQL/pgvector, Redis/Celery, BGE-M3, local media storage, and existing API
paths remain the production/demo baseline.

## Goals / Non-Goals

**Goals:**

- Make PDF acquisition fail closed against SSRF, oversized input, invalid PDF content, and unsafe
  redirects without buffering whole files.
- Give each complete paper index a durable identity and atomically switch the active version.
- Deduplicate project requests and global builds across concurrent workers and redelivery.
- Let user uploads, URL ingestion, Agent add actions, and later workflows use one ingestion service.
- Preserve Stage B evidence, event, logging, API, and frontend compatibility contracts.

**Non-Goals:**

- Do not make LangGraph wait/resume durable; that belongs to `make-research-workflow-durable`.
- Do not change dense/FTS/sparse fusion, Agent prompts, model selection, MCP, or report generation.
- Do not add cloud object storage, antivirus, OCR, multi-user authorization, or a new task framework.
- Do not garbage-collect superseded versions in this change; retention is preferable to losing the
  rollback boundary.

## Decisions

### 1. Separate global index versions from project ingestion requests

Add `rag.PaperIndexVersion` because chunks and papers are global and may be shared by several
projects. It contains `paper`, lifecycle status, source SHA-256, pipeline signature, parser identity,
embedding model/version/dimension, chunk count, safe failure fields, and lifecycle timestamps.

Database constraints enforce one row per `(paper, source_sha256, pipeline_signature)` and at most
one `active` version per paper. `Text` receives a non-null `index_version` foreign key and uniqueness
changes from `(paper, chunk_index)` to `(index_version, chunk_index)`. Existing paper and embedding
fields remain for compatibility and efficient filtering.

`api.PaperIngestionJob` remains project-scoped and references a global version when known. It gains
`idempotency_key`, `source_kind`, `attempt_count`, `file_size`, `error_code`, `retryable`, and the
expanded state machine. `(project, paper, idempotency_key)` is unique. Multiple project jobs may
reference one global build without exposing one project's job to another.

Alternative rejected: making versions project-scoped would duplicate identical embeddings and
permit inconsistent active evidence for the same global paper.

Published versions are immutable. `active`, `superseded`, and `failed` versions retain their chunk
sets for audit and rollback; all new chunk writes target a dedicated `building` version. A legacy
compatibility adapter follows the same rule and may replace only chunks in its own building version.
It may not defer this invariant until `IngestionService` exists. Paper-wide deletion such as
`Text.objects.filter(paper=paper).delete()` is therefore outside the permitted design.

`PaperIngestionJob.index_version` is an audit relationship. The implementation must prevent normal
version lifecycle operations from silently nulling it. New jobs start with `attempt_count=0`; a
worker increments the count when an execution attempt actually begins.

### 2. Use one acquisition boundary with injectable resolver and transport

Create `SafePdfFetcher` in the RAG ingestion boundary. URL normalization accepts only HTTPS with no
userinfo and the default TLS port. The production resolver evaluates every A/AAAA answer with
`ipaddress` and rejects any address that is not globally routable. The transport connects to a
selected validated address directly, preserves the original hostname for TLS SNI/certificate and
HTTP Host validation, verifies the connected peer, disables `trust_env`, and does not follow
redirects automatically.

Redirect handling is explicit and repeats normalization, DNS validation, connection pinning, and
peer validation for every hop. The fetcher streams into a temporary content-addressed file while
hashing, stops above the configured 50 MiB limit, and accepts content only after `%PDF-` validation.
Resolver, transport, clock, and byte stream are injectable so offline tests prove behavior without
network access.

Alternative rejected: validating DNS and then calling ordinary `httpx.get(hostname)` leaves a DNS
rebinding interval and lets the HTTP stack resolve an unvalidated destination.

### 3. Stream uploads through the same validated file artifact

The upload endpoint iterates Django uploaded-file chunks, enforces the byte limit while writing,
computes SHA-256, validates PDF magic, and atomically renames the temporary file into
`MEDIA_ROOT/papers/<paper_id>/<sha256>.pdf`. Submitted names are retained only as sanitized display
metadata. Partial files are removed on validation or write failure.

Both upload and URL paths return a `ValidatedPdfArtifact` containing server path, SHA-256, size, and
safe source kind. Parsing never accepts arbitrary paths or unvalidated response bytes.

### 4. Stage first, activate in one short transaction

The worker claims or reuses a `PaperIndexVersion`, parses the validated artifact, chunks content,
and embeds outside the activation transaction. Empty/short parses, zero chunks, vector-count
mismatch, non-finite values, wrong dimension, or incompatible embedding metadata fail the version.

Validated `Text` rows are inserted under a `building` version and are invisible to retrieval. A
short `transaction.atomic()` block locks the paper/version rows, verifies persisted cardinality,
marks the prior active version `superseded`, and marks the new version `active`. Any failure rolls
back the switch; the previous active row and chunks remain queryable. `ProjectScopeResolver`, RAG,
read, compare, citation resolution, and Python fallback all filter through the active compatible
version, not merely `embedding_version` strings.

The legacy backfill is deterministic and model-free. It derives legacy provenance from persisted,
ordered chunk content identities and reads the compatible embedding identity from explicit
migration configuration. It does not instantiate an embedding provider, access a model cache, or
silently replace missing configuration with a different selection rule.

### 5. Derive separate request and build identities

An explicit `Idempotency-Key` is normalized and hashed with project, paper, source kind, and pipeline
signature. Without a header, upload requests derive it from file SHA-256; URL requests derive it
from canonical URL plus pipeline signature. This identifies the project request.

After acquisition, the global build key is SHA-256 over paper ID, file SHA-256, parser/chunk
configuration, and embedding metadata. A unique constraint selects one build owner. Losing workers
cannot create a second version; another job attaches to the same version and receives its terminal
state. Completion or failure updates every project job attached to that version.

### 6. Centralize queueing and bounded retry policy

Add an `IngestionService` used by REST views, `add_papers_to_project`, and later workflows. It owns
job creation/reuse, source validation metadata, Celery enqueueing, and structured response shapes.
The Agent tool receives only queued/reused/upload-required summaries and never performs fetch,
parse, embedding, or database activation itself.

Celery uses late acknowledgement and rejects work on worker loss. Only connection failures,
timeouts, HTTP 408/429, and 5xx call `self.retry`, with at most three retries and exponential backoff
plus jitter. Validation, SSRF, other 4xx, parsing, cardinality, and dimension errors are permanent.
The explicit retry API increments attempt count and reuses the same request/build identity; it is
available only for a scoped failed retryable job.

### 7. Preserve APIs while making lifecycle explicit

Existing upload, URL-ingest, job-list, project-paper, chat, and RAG paths remain. New requests return
201; idempotent reuse returns 200 with `deduplicated=true`. Add
`POST /api/projects/<project_id>/ingestion-jobs/<job_id>/retry`.

Serializers add lifecycle state, active version ID, embedding identity, indexed time, chunk count,
error code, retryability, and `fulltext_ready`. Existing fields remain. `fulltext_ready` is computed
from a compatible active version with non-zero chunks, never from URL presence or job text.

### 8. Keep progress safe and front-end states local

All task events use `EventPublisher`; event schemas are extended only with allowlisted IDs, state,
counts, duration, retryability, and safe error code/hash. URLs, paths, filenames, content, vectors,
peer addresses, and raw exceptions remain internal. Logs follow the same safe summary contract.

Pinia retains project-scoped ingestion jobs and maps intermediate states without blocking unrelated
papers, chat, reports, or graph data. Evidence Board disables duplicate commands while active,
offers retry only when permitted, and keeps upload available when a new artifact is required.

## Risks / Trade-offs

- **Custom connection pinning is security-sensitive** -> isolate it behind a small fetcher, deny on
  ambiguous DNS/peer state, add injected and real-loopback adversarial tests, and keep HTTPS-only.
- **Data migration may expose mixed historical vectors** -> create versions per paper/embedding
  group, activate only the newest group compatible with explicit migration settings, leave others
  superseded, and prove deterministic output with historical-state migration tests.
- **A compatibility writer may mutate published history** -> require building-only writes and
  building-scoped replacement even before the centralized ingestion service is introduced.
- **Nullable job/version links weaken incident reconstruction** -> retain referenced versions and
  enforce the relationship at the database boundary unless a later OpenSpec change defines safe
  garbage collection.
- **Building versions increase storage** -> retain them for rollback in this change and report size;
  add retention only through a later specification.
- **Worker death can leave `building` rows** -> unique build identity and retry claim rules resume or
  fail the same version; retrieval never reads building rows.
- **Automatic ingestion can consume compute unexpectedly** -> queue only newly created memberships,
  cap at three per Agent add call, and report deferred/upload-required papers.
- **HTTPS-only rejects some legacy sources** -> normalize supported scholarly sources to HTTPS and
  require user upload when a source cannot provide a safe HTTPS artifact.

## Migration Plan

1. Add `PaperIndexVersion`, nullable `Text.index_version`, and additive job fields/status values.
2. Data-migrate existing chunks into immutable versions grouped by paper and embedding identity;
   activate the newest group compatible with configured embedding metadata.
3. Verify every `Text` has a version, then make the foreign key non-null, replace uniqueness, and
   add partial/identity constraints.
4. Deploy code that reads only active compatible versions and writes only through
   `IngestionService`; keep prior versions intact.
5. Run migration rollback tests, Docker PostgreSQL concurrency tests, and one real non-eager Celery
   ingestion before enabling Agent auto-queue.
6. Roll back application code by disabling auto-queue and reading the previously recorded active
   version. Do not reverse the data migration after new versions exist; a corrective forward
   migration is required.
