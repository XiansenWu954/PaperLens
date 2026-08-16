## Why

PaperLens currently labels its project retriever as hybrid, but the production BGE-M3 sparse path
only reorders dense candidates. A passage that is outside dense Top-K can therefore never be
recovered by sparse lexical matching, even when it is the best exact-term match. The current RRF
path also lacks route weights, raw route scores and a stable trace contract, so route independence
and fusion quality cannot be audited. `compare_papers` retrieves each paper separately but does not
express those per-paper obligations through the same structured retrieval contract.

The existing real-PDF evaluation is useful as a smoke suite, but its twelve-paper, thirty-case,
term-based dataset and stale private-call assumptions are not sufficient for a production quality
claim. Phase 3 makes the retrieval contract real and establishes a preregistered, split-aware quality
gate before any DeepSeek answer-quality evaluation.

## What Changes

- Introduce internal `RetrievalPlan`, route-candidate, result and safe-trace structures while keeping
  the existing `hybrid_retrieve_texts(...) -> list[Text]` compatibility wrapper.
- Encode BGE-M3 dense and sparse query representations once, then run dense, PostgreSQL FTS and
  BGE-M3 sparse as three independent project-scoped retrieval routes.
- Add a concurrent PostgreSQL `jsonb_ops` GIN index over existing sparse weights and independently
  score sparse candidates from every eligible active chunk, never only dense candidates.
- Fuse the union of route candidates with deterministic weighted reciprocal-rank fusion and expose
  route counts, scores, contributions and latency only through a safe internal evaluation trace.
- Express comparison retrieval as one independently scoped sub-plan per target paper so one paper
  cannot displace another from a global Top-K.
- Keep a BGE reranker behind an explicit default-off experiment flag; enabling it by default requires
  a separate Codex decision after quality and latency gates pass.
- Retain the existing twelve-paper/thirty-case suite as a smoke regression and add a versioned gold-v2
  contract for thirty papers and 120 graded cases with dev, calibration and sealed held-out splits.
- Separate DS development evidence from GLM held-out preregistration and independent acceptance even
  when both agents use GLM-5.2.

This change does not modify project authorization, EvidenceEnvelope or CitationResolver semantics,
durable workflow ownership, SSE subscription, public REST/MCP schemas, DeepSeek prompts, answer
judging, ingestion lifecycle, or frontend behavior.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `production-hybrid-rag`: Define independent dense, FTS and sparse recall, deterministic weighted
  fusion, comparison obligations, safe retrieval traces and the default-off reranker boundary.
- `agent-harness-and-mcp`: Define the real-PDF gold-v2 retrieval evaluation, split isolation,
  provenance, metrics, release gates and independent acceptance requirements.

## Impact

- Retrieval planning, PostgreSQL and Python-fallback candidate generation, RRF fusion, compare-paper
  integration, safe retrieval telemetry and one concurrent GIN-index migration.
- Additive settings for per-route K/weights, sparse query token cap and the default-off reranker,
  while preserving `PAPERLENS_RAG_LEXICAL_K` as a one-release compatibility alias.
- Evaluation schema, data manifests, fixed split hashes, ablation runners, provenance, fail-closed
  verifiers and gitignored real-PDF/held-out artifacts.
- Release requires Stage B, Phase 1 and Phase 2 regressions, real PostgreSQL/pgvector/BGE-M3 evidence,
  zero scope leakage, the frozen quality thresholds and independent GLM acceptance.
