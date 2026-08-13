# Spec: project-constitution

## ADDED Requirements

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
