## ADDED Requirements

### Requirement: Gold-v2 retrieval evaluation

PaperLens MUST provide a versioned real-PDF retrieval evaluation that measures independent routes,
fusion and per-paper comparison coverage without using final-answer LLM judgment.

#### Scenario: Build the versioned dataset

- **WHEN** the Phase 3 gold-v2 dataset is frozen
- **THEN** it MUST contain thirty real computer-science papers across six topics and 120 cases
- **AND** cases MUST be split by paper into 48 dev, 24 calibration and 48 held-out cases
- **AND** category totals MUST be 48 factual, 18 terminology or abbreviation, 12 cross-language,
  12 author/year, 20 compare and 10 hard-negative or scope cases.

#### Scenario: Label positive evidence

- **WHEN** a case expects full-text evidence
- **THEN** each gold label MUST identify the paper, canonical chunk content hash, page or section and
  a relevance grade from zero to three
- **AND** every compare case MUST define a separate gold obligation for each target paper.

#### Scenario: Preserve the smoke dataset

- **WHEN** the existing twelve-paper/thirty-case evaluation runs
- **THEN** it MUST use current public retrieval interfaces and active-index scope rules
- **AND** its result MUST be reported only as smoke regression evidence, not the Phase 3 quality verdict.

### Requirement: Split-isolated retrieval tuning

Development tuning and official held-out acceptance MUST use disjoint labels and independently
generated evidence.

#### Scenario: Tune development parameters

- **WHEN** DS selects route K, weights or RRF constant
- **THEN** it MAY use only dev and calibration labels and only the preregistered parameter grid
- **AND** the chosen configuration, code revision and dataset hashes MUST freeze before held-out runs.

#### Scenario: Seal held-out labels

- **WHEN** implementation work begins
- **THEN** an independent GLM session MUST already have stored held-out labels in a gitignored artifact
  and provided its schema/count manifest and SHA-256 to Codex
- **AND** DS MUST NOT read, modify, materialize or infer those labels.

#### Scenario: Run independent acceptance

- **WHEN** Codex authorizes held-out acceptance for a fixed commit
- **THEN** GLM MUST independently validate the sealed hash and recompute results from raw PostgreSQL,
  BGE-M3 and trace evidence
- **AND** it MUST NOT use DS aggregators, verdict fields or adjusted labels.

### Requirement: Retrieval quality and provenance gate

The Phase 3 verdict MUST be calculated from fixed raw results and complete runtime provenance using a
read-only fail-closed verifier.

#### Scenario: Compute path metrics

- **WHEN** dense, FTS, sparse, hybrid or reranker paths are evaluated
- **THEN** the report MUST include Paper Recall@5, MRR, Context Precision@5, graded nDCG@5, compare
  obligation recall, scope leakage and warmed p50/p95 latency as applicable
- **AND** it MUST include route candidate IDs/ranks/scores and fused contributions without raw content.

#### Scenario: Pass the base hybrid gate

- **WHEN** the sealed held-out results are verified
- **THEN** Paper Recall@5 MUST be at least 0.92, MRR at least 0.90, Context Precision@5 at least 0.85
  and compare obligation Recall@5 at least 0.80
- **AND** hybrid Recall@5 MUST NOT be more than 0.02 below the best single route
- **AND** all preregistered sparse-only and FTS-only traps MUST be recovered.

#### Scenario: Pass stability, performance and isolation gates

- **WHEN** the fixed dataset runs three times after warm-up
- **THEN** every core metric delta MUST be no greater than 0.005
- **AND** hybrid p95 MUST be no greater than twice dense-only p95 on the same runtime
- **AND** cross-project, excluded, stale and metadata-as-fulltext leakage MUST be zero.

#### Scenario: Verify evidence and report consistency

- **WHEN** a report verdict is generated
- **THEN** test counts, metrics, hashes, configuration, dependency versions, database/pgvector identity,
  model identity, latency and verdict MUST be parsed from fixed raw artifacts
- **AND** missing, duplicate, stale, tampered, hardcoded or contradictory evidence MUST fail closed.

### Requirement: Reranker A/B approval gate

Reranker measurements MUST remain separate from the base hybrid verdict and MUST NOT automatically
change the production default.

#### Scenario: Qualify a future default change

- **WHEN** reranker A/B completes on the sealed held-out set
- **THEN** default enablement MAY be proposed only if Context Precision@5 improves by at least 0.03,
  Recall@5 decreases by no more than 0.01 and warmed p95 increases by no more than 50 percent
- **AND** every scope, evidence, stability and provenance gate remains satisfied
- **AND** Codex MUST approve a separate configuration change before the default changes.
