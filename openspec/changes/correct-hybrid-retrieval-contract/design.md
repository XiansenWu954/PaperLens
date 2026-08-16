## Context

`rag.retrieval.hybrid_retrieve_texts` currently obtains independent pgvector and FTS lists, then, when
the provider supports sparse encoding, replaces the FTS list with a sparse reordering of the dense
list. That implementation cannot retrieve an item outside dense Top-K and does not represent three
independent retrieval signals. It also returns only `Text` rows, discarding route raw scores and
fusion contributions needed to diagnose quality.

The existing `Text.sparse_weights` JSONB field already stores BGE-M3 lexical token weights. BGE-M3
supports dense and sparse retrieval from one model, PostgreSQL `jsonb_ops` GIN supports top-level key
existence, and RRF combines independently ranked lists without requiring score calibration. The
smallest production-aligned change is therefore an indexed independent sparse route over the current
field, not a new vector database or a normalized posting service.

## Goals And Non-Goals

Goals:

- Make dense, FTS and BGE-M3 sparse genuinely independent scoped recall routes.
- Preserve active-index, project-scope, evidence and event-safety invariants.
- Make candidate generation and fusion deterministic and diagnosable without exposing query text or
  full text outside the in-memory retrieval boundary.
- Give every compare target an independent evidence opportunity.
- Establish a sufficiently large, split-isolated quality gate and independent acceptance process.

Non-goals:

- No LangChain, new vector database, LLM query expansion or Agent framework change.
- No Phase 4 SSE work or frontend retrieval-debug UI.
- No Phase 5 DeepSeek answer/Judge conclusions.
- No change to chunking, embedding dimension, active-index lifecycle, CitationResolver or public tool
  response schemas.
- No production-default reranker enablement in this change.

## Retrieval Architecture

### In-memory plan and result

Add immutable internal structures:

- `RetrievalPlan`: raw query, scoped paper IDs, per-route K, final K, RRF constant and weights,
  optional compare targets, sparse token limit and reranker request.
- `RouteCandidate`: text ID, route, rank, route raw score and route latency.
- `FusedCandidate`: text ID, final rank, RRF score, best route rank and per-route contributions.
- `RetrievalResult`: ordered `Text` rows, route results, fused candidates, total duration and reranker
  status.

The raw query is necessary in memory but MUST be excluded from dataclass representation intended for
serialization, EventPublisher payloads, logs and API results. A safe trace contains numeric IDs,
route names, ranks, scores, counts, durations, embedding identity and stable status codes only.

`execute_retrieval(plan) -> RetrievalResult` becomes the structured core. The existing
`hybrid_retrieve_texts` builds a plan and returns `result.texts` unchanged for compatibility.

### Query encoding

The embedding provider gains one internal query-modality operation. BGE-M3 obtains dense and sparse
representations in one model invocation. Providers without sparse capability return dense plus an
explicit unavailable sparse route. Tests use the fake provider and MUST NOT download a model.

### Independent routes

All routes use the same fail-closed scope predicate:

- explicit `paper_ids=[]` matches nothing;
- only current `active` index versions are eligible;
- chunk and index embedding model, version and dimension must agree with current configured metadata;
- foreign, excluded, unlinked, building, superseded, failed and stale chunks are ineligible.

Dense returns pgvector cosine similarity and FTS returns PostgreSQL rank. Sparse selects documents
whose JSONB token map contains at least one of the strongest query tokens, using a `jsonb_ops` GIN
index, then computes the exact BGE sparse dot product for all matching eligible documents before
applying sparse Top-K. Query sparse weights are deterministically sorted by descending absolute
weight and token ID, capped at 64. The Python fallback independently scores every eligible scoped
chunk and never derives sparse candidates from dense results.

The migration uses PostgreSQL `AddIndexConcurrently` with `atomic = False`. SQLite receives no index
operation. Migration tests cover forward/backward behavior and preserve existing rows and active
versions.

### Weighted RRF

Fusion operates on the union of all route candidate IDs:

`fused_score = sum(route_weight / (rrf_k + rank))`.

Defaults are dense/FTS/sparse weights `1.0/1.0/1.0`, route K `20/20/20`, final K from the existing
setting and `rrf_k=60`. Equal fused scores sort by best route rank and then ascending text ID. An empty
or unavailable route contributes nothing and never replaces another route.

Development tuning is limited to the preregistered grid: weights in `{0.5, 1.0, 1.5, 2.0}`, route K
in `{10, 20, 40}` and RRF K in `{30, 60}`. Only dev and calibration may select parameters. The chosen
configuration and dataset hashes are frozen before held-out execution.

## Product Integration

`query_project_rag` keeps its existing return contract and receives ordered texts from the compatibility
wrapper. EvidenceEnvelope creation and CitationResolver verification are unchanged.

`compare_papers` builds one sub-plan per validated target paper, scopes each to exactly that paper and
requests three chunks. Compare coverage is computed per target from returned resolvable full-text
evidence. Metadata-only or empty sub-plans retain the existing structured evidence-gap behavior and
capability abstention; candidates from one paper cannot fill another paper's obligation.

Durable workflows continue to pass their ready/succeeded dependency paper IDs into project RAG. This
change alters ranking only and does not change checkpoints, owner leases, resume behavior, retrieval
timing or report-binding rules.

The `hybrid_retrieval` event remains a safe summary event. It may add route availability, candidate
counts and route/total durations, but never query text, titles, excerpts, sparse token maps or raw
errors. Full route traces are available only through an explicit in-process evaluation hook.

## Reranker Experiment

The optional adapter is configured by `PAPERLENS_RAG_RERANKER_ENABLED=0` and a model setting. When
disabled it loads nothing. When explicitly enabled, it reranks the fused candidate pool before the
existing RCS stage and records `applied`, `unavailable` or `failed` with safe timings. An unavailable
experiment fails its A/B run rather than being reported as reranked; the product may still return the
pre-rerank fused order with the unavailable status.

Default enablement is outside this change. A later Codex decision requires Context Precision@5 gain
of at least 0.03, Recall@5 loss no greater than 0.01, warm p95 increase no greater than 50 percent and
no scope/evidence regression.

## Configuration Compatibility

Add explicit FTS K, sparse K, route weights, sparse token cap and reranker settings. The existing
`PAPERLENS_RAG_LEXICAL_K` remains a deprecated alias for FTS K for one release. Explicit FTS K wins;
when both values differ, startup emits a safe configuration-conflict event without values. Invalid
K, weights, token cap or RRF values fail closed during plan construction.

## Gold-v2 Evaluation

The existing twelve-paper/thirty-case suite remains a smoke regression and cannot produce the Phase 3
quality verdict. Gold-v2 contains thirty real CS papers over six topics and 120 cases split by paper:
48 dev, 24 calibration and 48 held-out. The category totals are 48 factual, 18 terminology or
abbreviation, 12 cross-language, 12 author/year, 20 compare and 10 hard-negative or scope cases.

Each positive label records paper identity, canonical chunk content hash, page/section and a relevance
grade from zero to three. Compare labels contain an obligation for every target paper. Dev and
calibration annotations may be used by DS. Before implementation tuning, a separate GLM session
creates the held-out labels in a gitignored sealed artifact; Codex records only its SHA-256 and schema
manifest in the implementation handoff.

The evaluator reports route-specific and hybrid Paper Recall@5, MRR, Context Precision@5, graded
nDCG@5, compare obligation recall, scope leakage and warmed route/total latency. It runs three times
on fixed data. Metrics, counts, provenance and verdict are derived from raw results by a read-only,
fail-closed verifier; no report field is hardcoded.

## Release And Rollback

Release gates are the exact thresholds in `tasks.md` and the capability deltas. The previous retrieval
implementation remains reachable only as a test oracle during development, not as a silent production
fallback. Operational rollback disables sparse route weight and reranker while retaining dense+FTS;
it does not drop the GIN index or mutate indexed evidence. Any security, evidence, active-index or
durable-workflow regression blocks advancement.
