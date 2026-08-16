## ADDED Requirements

### Requirement: Independent hybrid retrieval routes

Project retrieval MUST generate dense, PostgreSQL full-text and BGE-M3 sparse candidates as
independent ranked lists over the same project-scoped, active-compatible chunk set.

#### Scenario: Sparse evidence is outside dense Top-K

- **GIVEN** an eligible chunk has the strongest sparse lexical score but is outside dense Top-K
- **WHEN** hybrid retrieval runs with sparse available
- **THEN** the chunk MUST remain eligible for the sparse Top-K and fused candidate union
- **AND** sparse candidates MUST NOT be derived by reordering dense candidates.

#### Scenario: FTS evidence is outside dense Top-K

- **GIVEN** an eligible chunk is an FTS match but is outside dense Top-K
- **WHEN** hybrid retrieval runs
- **THEN** the chunk MUST remain eligible for the FTS Top-K and fused candidate union.

#### Scenario: A route is empty or unavailable

- **WHEN** one route returns no candidates or the configured provider cannot supply that modality
- **THEN** the route MUST have an explicit empty or unavailable status
- **AND** it MUST NOT replace, narrow or fabricate candidates for another route.

#### Scenario: Scope and active-index parity

- **WHEN** dense, FTS, sparse or Python fallback evaluates candidates
- **THEN** every route MUST apply the same project-paper scope and current active index model/version/
  dimension compatibility predicates
- **AND** foreign, excluded, unlinked, building, superseded, failed, stale and explicit-empty-scope
  chunks MUST be ineligible.

### Requirement: Deterministic weighted rank fusion

Hybrid retrieval MUST fuse the union of independent route candidates with deterministic weighted
reciprocal-rank fusion and MUST retain enough safe route detail to reproduce the final order.

#### Scenario: Fuse independent candidate lists

- **WHEN** one or more routes return candidates
- **THEN** each candidate contribution MUST equal its configured positive route weight divided by
  `rrf_k + route_rank`
- **AND** the final score MUST be the sum of its available route contributions.

#### Scenario: Resolve a fused-score tie

- **WHEN** candidates have equal fused scores
- **THEN** the candidate with the best individual route rank MUST sort first
- **AND** a remaining tie MUST sort by ascending text ID.

#### Scenario: Invalid retrieval plan

- **WHEN** route K, final K, RRF constant, sparse token cap or route weight is missing, non-finite or
  outside its approved positive range
- **THEN** plan construction MUST fail closed with a stable configuration code
- **AND** no retrieval query MUST execute.

### Requirement: Single-pass BGE-M3 query modalities

The BGE-M3 provider MUST obtain dense and sparse query representations in one model invocation for a
single retrieval plan.

#### Scenario: Execute all BGE routes

- **WHEN** dense and sparse routes are enabled for one query
- **THEN** the provider MUST return both modalities from one query encoding call
- **AND** sparse scoring MUST use the stored BGE-M3 lexical weights of every matching eligible chunk.

#### Scenario: Run offline tests

- **WHEN** deterministic tests execute
- **THEN** they MUST use fake or explicitly local providers
- **AND** no model download or external network call is permitted.

### Requirement: Indexed sparse retrieval

Production PostgreSQL sparse retrieval MUST use the existing JSONB sparse-weight representation with
an indexable token-key prefilter and exact weighted scoring over eligible matches.

#### Scenario: Build the sparse index

- **WHEN** the Phase 3 migration runs on PostgreSQL
- **THEN** it MUST create a `jsonb_ops` GIN index concurrently without rewriting or deleting Text or
  PaperIndexVersion rows
- **AND** reversing the migration MUST remove only that index.

#### Scenario: Bound query terms

- **WHEN** a query produces more sparse token weights than the configured cap
- **THEN** the strongest weights MUST be selected deterministically by absolute weight and token ID
- **AND** the default cap MUST be 64.

### Requirement: Per-paper comparison retrieval

Comparison retrieval MUST preserve an independent full-text evidence obligation for every validated
target paper.

#### Scenario: Compare multiple full-text papers

- **WHEN** a user compares two to five validated project papers
- **THEN** retrieval MUST run one plan scoped to each target paper and return up to three chunks per
  paper
- **AND** candidates from one target MUST NOT consume another target's evidence allocation.

#### Scenario: A comparison target lacks evidence

- **WHEN** a target is metadata-only or its independent plan has no resolvable full-text result
- **THEN** that paper MUST appear in the structured evidence gap
- **AND** metadata MUST NOT satisfy the target's full-text answer-binding obligation.

### Requirement: Safe retrieval diagnostics

Retrieval diagnostics MUST make route behavior auditable without exposing sensitive or model-visible
content through persistent surfaces.

#### Scenario: Capture an evaluation trace

- **WHEN** an explicit in-process evaluation hook is provided
- **THEN** it MAY receive scoped text IDs, route names, ranks, numeric scores, fusion contributions,
  availability, embedding identity and durations
- **AND** it MUST NOT receive query text, title, excerpt, content, sparse maps, URL, key or raw error.

#### Scenario: Publish retrieval events

- **WHEN** EventPublisher emits a hybrid retrieval summary
- **THEN** it MAY include route availability, candidate counts and durations
- **AND** full route traces and content MUST remain absent from REST, MCP, SSE and DB event payloads.

### Requirement: Experimental reranker boundary

A local BGE reranker MUST remain an explicit default-off experiment until separately approved.

#### Scenario: Reranker disabled

- **WHEN** the default configuration is used
- **THEN** no reranker model MUST load or download
- **AND** fused RRF order MUST proceed directly to the existing downstream evidence scorer.

#### Scenario: Reranker experiment unavailable

- **WHEN** an explicit A/B run requests the reranker but the model is unavailable or fails
- **THEN** the experiment MUST record a stable unavailable or failed status
- **AND** it MUST NOT report the pre-rerank order as a successful reranked result.
