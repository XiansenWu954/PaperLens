## ADDED Requirements

### Requirement: Workflow-gated project retrieval

Durable research workflows MUST retrieve only after their ingestion dependencies are terminal and
MUST use only current project-scoped active full-text evidence.

#### Scenario: Wait before retrieval

- **GIVEN** a workflow has at least one pending ingestion dependency
- **WHEN** its next planned step is project RAG
- **THEN** retrieval MUST NOT execute or emit a committed retrieval event
- **AND** the workflow MUST remain `waiting_ingestion` with its checkpoint persisted.

#### Scenario: Retrieve after terminal dependencies

- **WHEN** every dependency is `ready`, `succeeded`, `failed` or `unavailable`
- **THEN** the workflow MAY execute project RAG
- **AND** `first_rag_at` MUST be strictly later than `last_ingestion_terminal_at`
- **AND** retrieval MUST use ProjectScopeResolver and only active compatible index versions.

#### Scenario: Exclude failed dependency data

- **WHEN** one or more workflow dependencies failed, are unavailable, or have only metadata
- **THEN** their partial, building, failed, superseded or metadata-only content MUST NOT enter RAG
  context or be represented as full-text evidence
- **AND** successful active dependencies MUST remain independently eligible.

#### Scenario: Repeated workflow retrieval

- **WHEN** duplicate resume delivery re-enters the retrieval boundary after a committed retrieval
- **THEN** the workflow MUST reuse the committed result identity or safely recompute without creating
  another committed retrieval event
- **AND** the selected evidence MUST remain project-scoped and citation-resolvable.
