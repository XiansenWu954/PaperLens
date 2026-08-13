## Context

See `proposal.md`. PaperLens already uses OpenSpec and four-part implementation reports, but review
findings have not always been promoted to durable requirements when they revealed missing security
or data invariants. Older orchestration wording also predates the current deterministic-chat and
durable-workflow boundary.

## Goals / Non-Goals

**Goals:**

- Preserve one auditable chain from product intent to requirement, implementation, test, and verdict.
- Keep role ownership stable across handoffs and context compaction.
- Detect architectural drift before another task group starts.
- Keep specs concise by promoting only durable behavior and invariants.

**Non-Goals:**

- Do not modify PaperLens production behavior in this change.
- Do not rewrite archived historical changes.
- Do not require every line-level bug fix to become a capability requirement.
- Do not add an Agent framework, testing framework, or external service.

## Decisions

### 1. Classify every finding before closing it

Use four destinations: capability spec for durable behavior, design for implementation decisions,
tasks for concrete repair work, and internal evidence for raw failures/results. A repeated defect is
promoted when it demonstrates a missing invariant rather than an isolated implementation mistake.

Alternative rejected: writing every bug into specs would make the normative surface unreadable;
keeping everything in reports would lose the product contract after handoff.

### 2. Maintain a traceability ledger inside each active change

Each P0/P1 finding records an ID, affected requirement/scenario, code boundary, positive and
negative controls, artifact path, DS status, Codex decision, and GLM status when applicable. The
change's `tasks.md` is the authoritative work-state summary; large raw evidence stays internal.

### 3. Separate implementation and independent acceptance

Codex owns requirements and gates, DS owns implementation and first-party verification, and GLM
owns independent audit. DS cannot alter independent assertions, while GLM cannot repair production
code. Codex alone resolves a genuine specification conflict.

### 4. Make drift review a phase gate

Before handoff or phase advancement, compare changed files and behavior against the proposal's
Goals, Non-Goals, public interfaces, framework boundaries, and frozen Stage B invariants. Record one
of `NO DRIFT`, `DRIFT RESOLVED`, or `BLOCKED`, with links for every exception.

### 5. Preserve bounded Agent framework ownership

Normal chat remains deterministic router plus bounded ReAct Harness. LangGraph is invoked only by
an explicit durable research workflow requiring checkpointing, waiting, resumption, approval, or
multi-stage state. Reasoning modes are runtime configuration evaluated by quality, latency, and
reliability; they are not permanent spec constants.

## Risks / Trade-offs

- **Governance becomes ceremony** -> keep the ledger limited to material P0/P1, contract changes,
  and recurring defects; ordinary implementation fixes remain task/report entries.
- **Two active changes conflict** -> each change owns disjoint capabilities unless both explicitly
  record the dependency; Codex resolves overlap before implementation continues.
- **Reports drift from code** -> require fixed raw artifacts and machine-readable consistency checks
  for release claims, while treating manual summaries as non-authoritative.

## Migration Plan

1. Add governance deltas and update `openspec/AGENTS.md`.
2. Audit active specs and active changes for direct contradictions.
3. Add missing Phase 1 invariants to its own ingestion change rather than this governance change.
4. Validate all specs and changes strictly with the installed OpenSpec CLI.
5. Archive this change after review; future changes inherit the updated constitution.
