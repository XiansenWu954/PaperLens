# Spec: project-constitution

## Purpose

Define the canonical architecture, framework boundaries, evidence requirements, and
documentation-truth rules that every PaperLens change must follow.
## Requirements
### Requirement: Canonical V3 Architecture

PaperLens MUST maintain one current architecture truth across its collaboration
constitution, current capability specs, runtime configuration, and public status output.

#### Scenario: Primary data infrastructure

- **WHEN** an implementation or test describes the V3 integration/demo runtime
- **THEN** PostgreSQL + pgvector MUST be treated as the primary database and vector index
- **AND** Redis + Celery MUST be treated as the background execution infrastructure
- **AND** SQLite MUST be identified only as an explicitly limited fallback or unit-test path.

#### Scenario: Current embedding provider

- **WHEN** current architecture documentation describes the default embedding provider
- **THEN** it MUST match the effective Django setting and `.env.example`
- **AND** the model name, dimension, and embedding version MUST be stored with indexed chunks
- **AND** tests MUST use a deterministic fake provider unless a real-model test is explicitly enabled.

#### Scenario: Configuration locations

- **WHEN** configuration loading is documented
- **THEN** root `.env` MUST be identified as the Docker Compose input
- **AND** `backend/.env` MAY be identified as the standalone Django input
- **AND** neither location nor any documentation MAY contain committed secret values.

### Requirement: Framework Responsibility Boundaries

Each Agent-related framework MUST have a single stated responsibility and MUST NOT duplicate
another component's ownership without an approved change.

#### Scenario: Normal project chat

- **WHEN** a user sends a normal project chat request
- **THEN** the deterministic router and bounded ReAct Harness MUST remain the stable execution path
- **AND** LangGraph MUST NOT be required solely to route ordinary chat tools.

#### Scenario: Durable research workflow

- **WHEN** a workflow requires checkpointing, waiting, resumption, approval, or multi-stage state
- **THEN** LangGraph MAY own the workflow state
- **AND** Celery MUST execute idempotent work units without becoming a competing workflow owner.

#### Scenario: External MCP capability

- **WHEN** PaperLens exposes a capability through MCP
- **THEN** the capability MUST be stable and meaningful to an external client
- **AND** it MUST reuse the same project scope, tool schema, and policy contracts as internal calls.

### Requirement: Evidence-Based Framework Changes

PaperLens MUST NOT replace or add a major framework without a measured, reversible decision.

#### Scenario: Propose a new framework

- **WHEN** a change proposes a new Agent runtime, vector database, search service, workflow engine,
  or service boundary
- **THEN** its design MUST identify a reproduced limitation in the current system
- **AND** it MUST record same-dataset quality, latency, reliability, and complexity baselines
- **AND** it MUST define compatibility, feature-flag, and rollback behavior.

### Requirement: Documentation Truth Discipline

Current specs and machine-readable artifacts MUST take precedence over manual summaries.

#### Scenario: Documentation conflict

- **WHEN** a README, handoff, report, current spec, or runtime configuration disagrees
- **THEN** the conflict MUST be recorded and resolved through an OpenSpec change
- **AND** an executor MUST NOT silently choose or rewrite the value that makes a test pass.

#### Scenario: Historical change record

- **WHEN** current architecture facts change
- **THEN** archived changes MUST remain immutable historical records
- **AND** only current specs and a new approved change may describe the new truth.

### Requirement: Governed Role Separation

PaperLens changes MUST preserve independent ownership of product requirements, implementation, and
acceptance decisions.

#### Scenario: Implementation handoff

- **WHEN** an approved change is handed to DS for implementation
- **THEN** Codex MUST remain owner of scope, requirements, gates, and stop conditions
- **AND** DS MUST limit changes to the approved scope and first-party verification
- **AND** DS MUST stop before independent acceptance unless Codex explicitly authorizes handoff.

#### Scenario: Independent acceptance

- **WHEN** Codex authorizes GLM to evaluate a candidate
- **THEN** GLM MUST independently derive results from raw artifacts and runtime behavior
- **AND** GLM MUST NOT modify production code by default
- **AND** DS MUST NOT weaken or rewrite GLM assertions without a Codex-approved spec correction.

### Requirement: Finding Classification And Traceability

Every material finding MUST be classified and traceable without turning capability specs into an
implementation changelog.

#### Scenario: Finding exposes a durable contract gap

- **WHEN** a defect changes or reveals a missing security boundary, data invariant, public behavior,
  compatibility promise, or release gate
- **THEN** the affected capability spec MUST be updated through an approved delta
- **AND** its design decision, repair task, positive control, negative control, and raw evidence MUST
  be linked from the active change.

#### Scenario: Finding is implementation-local

- **WHEN** a defect affects only an implementation detail, test fixture, mock, parser, verifier, or
  temporary file without changing durable behavior
- **THEN** it MUST be recorded in tasks and internal evidence
- **AND** it MUST NOT add a capability requirement solely to preserve a chronological log.

#### Scenario: Repeated local defect reveals a systemic gap

- **WHEN** the same defect class recurs or bypasses multiple producers or trust boundaries
- **THEN** Codex MUST reassess whether a missing invariant caused the recurrence
- **AND** a confirmed invariant gap MUST be promoted to a capability spec before closure.

### Requirement: Phase Drift Gate

Every implementation phase MUST be checked against its approved product intent before advancing.

#### Scenario: Phase completion review

- **WHEN** DS requests approval to enter the next task group
- **THEN** Codex MUST compare the implementation and artifacts with proposal Goals, Non-Goals,
  public interfaces, framework boundaries, and inherited security/evidence contracts
- **AND** the review MUST record `NO DRIFT`, `DRIFT RESOLVED`, or `BLOCKED`
- **AND** unresolved P0/P1 findings or unverifiable claims MUST block advancement.

#### Scenario: Framework or scope expansion

- **WHEN** implementation introduces an unapproved framework responsibility, autonomous action,
  service boundary, model dependency, or user-visible capability
- **THEN** work MUST stop
- **AND** the expansion MUST be removed or proposed as a separate approved OpenSpec change.

### Requirement: Specification Conflict Resolution

Specification conflicts MUST be resolved explicitly rather than interpreted in favor of a passing
implementation.

#### Scenario: Current documents disagree

- **WHEN** current capability specs, approved active deltas, AGENTS, design, tasks, reports, README,
  or runtime configuration disagree
- **THEN** the conflict MUST be recorded with both sources
- **AND** current capability specs plus approved active deltas MUST define intended behavior pending
  Codex resolution
- **AND** archived changes MUST remain historical and MUST NOT be rewritten.

#### Scenario: Test conflicts with approved behavior

- **WHEN** a first-party or independent test assertion conflicts with an approved requirement
- **THEN** Codex MUST classify the conflict as an implementation defect, test defect, or spec change
- **AND** only a documented Codex-approved correction may change the assertion or requirement.
