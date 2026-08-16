## 1. Codex Specification And Dataset Gate

- [x] 1.1 Inspect `main@e5c26ca`, current dense/FTS/sparse code, compare integration, active-index
  filters, Stage B evidence contracts, Phase 2 workflow boundaries and current real-PDF evaluation.
- [x] 1.2 Freeze proposal, design and deltas for `production-hybrid-rag` and
  `agent-harness-and-mcp`, including interfaces, compatibility, gates, Non-Goals and stop conditions.
- [x] 1.3 Run `openspec validate --all --strict`, commit the OpenSpec-only baseline and record its SHA
  in the external handoff so the task file does not contain a self-referential commit identity.
- [ ] 1.4 Give a clean GLM session the dataset-only preregistration handoff. Accept only a sealed
  held-out artifact hash, schema/count manifest and annotation-quality report; no production code.
- [ ] 1.5 Give DS only the approved specs, dev/calibration assets and held-out hash. Production code
  remains forbidden until 1.3 and 1.4 are complete.

## 2. GLM Preregistration — Sealed Held-out Dataset

- [ ] 2.1 Use the frozen gold-v2 schema to select the held-out twelve-paper split across all six topics
  without reading or changing production retrieval behavior.
- [ ] 2.2 Create 48 held-out cases with the frozen category distribution, graded canonical chunk labels,
  compare per-paper obligations and hard-negative/scope controls.
- [ ] 2.3 Validate every referenced PDF, active chunk hash, page/section and label; reject empty positives,
  duplicate cases, split overlap and labels that resolve only by metadata or stale versions.
- [ ] 2.4 Store labels only under gitignored
  `docs/internal/phase3-hybrid-retrieval/held-out-preregistration/`; publish to
  Codex a SHA-256, paper/case/category counts and schema version without revealing labels to DS.
- [ ] 2.5 Submit exactly four sections (`已实现`, `原始证据`, `完整测试`, `仍未完成`) and stop. Tables may
  only supplement the prose case matrix.

### Codex Preregistration Review Ledger

- [ ] P3-PREREG-CX-01: Correct the stale-trap exclusion entry so the active source and superseded
  source each bind to their actual PDF SHA-256 while the manifest still contains exactly fourteen
  distinct paper identities.
- [ ] P3-PREREG-CX-02: Recompute paper-split overlap from the final twelve held-out plus two trap
  identities, preserve a sealed legacy/smoke universe snapshot or digest evidence, and remove the
  obsolete pre-replacement candidate list as the source of the PASS claim.
- [ ] P3-PREREG-CX-03: Make the sealed verifier truly read-only and extend runtime/offline checks to
  bind accepted PDF bytes, canonical content hashes, page-or-section anchors, active/superseded
  versions, parser/chunker identity and real BGE-M3 dense/sparse index facts.
- [ ] P3-PREREG-CX-04: Generate a final detached artifact manifest after verifier and mutation output,
  report mutations separately from the read-only proof, and require all summary claims to recompute
  from the sealed artifacts without trusting stored PASS fields.

## 3. DS Batch A — Red Tests And Baseline

- [ ] 3.1 Add explicit red tests where the relevant sparse-only chunk is outside dense Top-K and the
  relevant FTS-only chunk is outside dense Top-K; require non-empty dense controls and exact expected IDs.
- [ ] 3.2 Add red tests for three independent candidate lists, pre-fusion union, weighted contributions,
  stable ties, empty/unavailable routes and invalid plan values.
- [ ] 3.3 Add PostgreSQL and Python-fallback red tests for own active positive evidence and foreign,
  excluded, unlinked, empty, building, superseded, failed and stale negatives on every route.
- [ ] 3.4 Add compare red tests proving each validated target receives a separate Top-3 plan and one
  paper cannot displace another; retain metadata/evidence-gap and abstention behavior.
- [ ] 3.5 Add safe-trace tests proving route IDs/ranks/scores/counts/timings survive while raw query,
  title, excerpt, content, sparse weights, URL, key and raw exception never leave memory.
- [ ] 3.6 Reproduce stale assumptions in the twelve-paper/thirty-case evaluator without mocking the
  retrieval contract; preserve it as a smoke baseline rather than an official quality verdict.
- [ ] 3.7 Produce fixed PostgreSQL/pgvector/fake-provider manifests, named case JSON, raw output, guard,
  leak scan and report consistency. Submit the four-section report and stop for Codex review.

## 4. DS Batch B — Independent Routes And Weighted Fusion

- [ ] 4.1 Add immutable plan/candidate/result/trace structures and an `execute_retrieval` core while
  preserving the existing list-returning wrapper and call signatures.
- [ ] 4.2 Add one-call BGE-M3 dense+sparse query encoding and deterministic provider-unavailable behavior;
  offline tests must use fake/local providers and make zero model-network calls.
- [ ] 4.3 Implement independent PostgreSQL dense, FTS and sparse routes with shared fail-closed scope and
  active-compatible-index predicates; sparse MUST search beyond dense candidates.
- [ ] 4.4 Add the non-atomic concurrent JSONB `jsonb_ops` GIN migration plus forward/backward and row/
  active-version preservation tests.
- [ ] 4.5 Implement independent Python fallback sparse scoring over every eligible scoped chunk.
- [ ] 4.6 Fuse the candidate union using configurable weighted RRF and deterministic tie-breaking;
  capture raw route score, contribution, availability and latency.
- [ ] 4.7 Add settings, `.env.example` and compatibility behavior for the deprecated lexical alias,
  conflict observability and invalid-value fail-closed validation.
- [ ] 4.8 Run focused, migration and complete backend regression; submit four sections and stop.

## 5. DS Batch C — Product Integration And Safety

- [ ] 5.1 Route project RAG through the compatibility wrapper without changing EvidenceEnvelope,
  CitationResolver, metadata fallback or capability-policy semantics.
- [ ] 5.2 Route compare through one sub-plan per validated target paper, Top-3 each, and preserve typed
  evidence gaps and per-paper answer-binding obligations.
- [ ] 5.3 Prove durable workflows still restrict retrieval to ready/succeeded dependency paper IDs and
  retain checkpoint, owner, timing, manifest-binding and single-report behavior.
- [ ] 5.4 Extend only the safe `hybrid_retrieval` summary schema with route availability/count/duration;
  keep full traces behind an in-process eval hook and absent from REST, MCP, SSE and DB events.
- [ ] 5.5 Implement the default-off reranker adapter and explicit unavailable/failed experiment status;
  do not enable it in production defaults.
- [ ] 5.6 Run Stage B, Phase 1, Phase 2 and full backend regression plus leak scans; submit four sections
  and stop for Codex drift review.

## 6. DS Batch D — Evaluation Contract And Development Tuning

- [ ] 6.1 Repair the twelve-paper/thirty-case evaluator against public/current retrieval interfaces and
  keep it as a smoke suite with active-index and project-scope controls.
- [ ] 6.2 Implement the gold-v2 schema, dataset verifier and dev/calibration assets: eighteen papers,
  72 cases, split-by-paper, graded canonical chunk labels and no held-out labels.
- [ ] 6.3 Implement dense/FTS/sparse/hybrid ablation, compare-obligation metrics, graded nDCG, warmed
  latency, route traces, three-run stability and a read-only fail-closed verifier.
- [ ] 6.4 Tune only the frozen weight/K/RRF grid on dev/calibration; freeze the chosen config, code SHA,
  dataset hashes and container/runtime provenance before held-out acceptance.
- [ ] 6.5 Run reranker A/B with an explicit real-model flag. Report quality and p95 deltas but keep the
  production default disabled regardless of the development result.
- [ ] 6.6 Add mutations for missing/duplicate cases, split overlap, stale/missing chunk labels, changed
  held-out hash, metric/report tampering, unavailable route reported as successful and raw trace leaks.
- [ ] 6.7 Submit four sections and stop. DS MUST NOT access, materialize or infer held-out labels.

## 7. DS Batch E — Fixed-revision Validation

- [ ] 7.1 On one fixed revision, run Docker PostgreSQL/pgvector with real BGE-M3 and prove three
  independent routes, GIN-backed sparse candidate selection and deterministic weighted fusion.
- [ ] 7.2 Run smoke plus dev/calibration three times; record all route metrics, warmed p50/p95, model
  invocation counts, index plan, memory/runtime provenance and stability deltas.
- [ ] 7.3 Run complete backend, Stage B, Phase 1, Phase 2, migration drift, Django check, frontend
  regression/build and OpenSpec strict validation; parse counts from raw output.
- [ ] 7.4 Scan logs, events, API, MCP, SSE, Celery results, evaluation traces and reports for project,
  stale evidence and sensitive-data violations.
- [ ] 7.5 Generate detached machine-readable artifacts, report consistency and fail-closed mutations;
  submit four sections and stop. Do not contact GLM directly.

## 8. Codex Drift Review And GLM Independent Acceptance

- [ ] 8.1 Codex compares the diff with Goals/Non-Goals and frozen Stage B/Phase 1/Phase 2 invariants;
  authorize GLM only with `NO DRIFT` or resolved findings and a fixed commit SHA.
- [ ] 8.2 GLM independently materializes the sealed held-out artifact, validates its hash and runs real
  BGE-M3 dense/FTS/sparse/hybrid/reranker paths without using DS aggregators or verdicts.
- [ ] 8.3 GLM independently recomputes Paper Recall@5, MRR, Context Precision@5, nDCG@5, compare
  obligation recall, leakage, warmed latency, three-run stability and route-independence traps.
- [ ] 8.4 GLM checks PostgreSQL GIN/index behavior, active/current scope, candidate union, RRF arithmetic,
  deterministic ties and trace/event/API leakage from raw DB/log/artifact evidence.
- [ ] 8.5 GLM submits four sections with `PASS`, `PASS WITH KNOWN RISKS` or `FAIL`; any P0 stops the run.

## 9. Release Gates And Archive

- [ ] 9.1 Require zero cross-project, excluded, stale or metadata-as-fulltext leakage and 100 percent
  recovery of preregistered sparse-only and FTS-only traps.
- [ ] 9.2 Require held-out Paper Recall@5 >= 0.92, MRR >= 0.90, Context Precision@5 >= 0.85,
  compare obligation Recall@5 >= 0.80 and hybrid Recall no more than 0.02 below the best single route.
- [ ] 9.3 Require warm hybrid p95 <= 2x dense-only p95 and three fixed-data runs with every core metric
  delta <= 0.005.
- [ ] 9.4 Keep reranker default off. A later Codex approval may enable it only if precision gain >= 0.03,
  recall loss <= 0.01, p95 increase <= 50 percent and all safety gates remain green.
- [ ] 9.5 Require code, fixed raw evidence, complete regression and report/JSON agreement; record final
  `PASS`, `PASS WITH KNOWN RISKS` or `FAIL` plus `NO DRIFT`, `DRIFT RESOLVED` or `BLOCKED`.
- [ ] 9.6 On PASS, merge deltas into current specs, archive the change, audit public files/secrets and
  create the Phase 3 commit/PR. Phase 4 remains a separate change.
