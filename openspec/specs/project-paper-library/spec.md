# Spec: project-paper-library

## Purpose

Define project membership, DBLP-first CS discovery, import and export, ingestion
state, and non-destructive paper-library management.
## Requirements
### Requirement: DBLP default source
Paper search MUST include DBLP by default for CS metadata.

#### Scenario: Default sources
- **WHEN** `datasources.registry.search(query)` is called without explicit sources
- **THEN** DBLP, OpenAlex, and ArXiv are included.

### Requirement: Project paper library
Projects MUST maintain their own paper membership without duplicating global papers.

#### Scenario: Remove project paper
- **WHEN** DELETE `/api/projects/<id>/papers/<paper_id>`
- **THEN** the paper is removed from the project only.

### Requirement: Project Paper Ingestion Contract

The project paper library MUST distinguish metadata membership from an active full-text index and
provide compatible ingestion operations.

#### Scenario: Read project paper ingestion state

- **WHEN** a client reads project papers or ingestion jobs
- **THEN** the response MUST retain existing fields and add the latest project job ID, lifecycle
  status, active index version ID, embedding model/version, indexed time, chunk count, error code,
  retryability, and `fulltext_ready`
- **AND** `fulltext_ready` MUST be true only when the paper has a compatible active index with at
  least one chunk.

#### Scenario: Retry a failed project job

- **GIVEN** a failed ingestion job belongs to the current project and remains retryable
- **WHEN** POST `/api/projects/<project_id>/ingestion-jobs/<job_id>/retry` is called
- **THEN** a new attempt MUST reuse the same validated source and global build identity
- **AND** the response MUST identify whether work was queued or deduplicated
- **AND** foreign, non-failed, or non-retryable jobs MUST fail closed without resource disclosure.

### Requirement: Bounded Automatic Ingestion After Add

An Agent paper-add action MAY enqueue safe ingestion as a bounded, auditable follow-up, but MUST
NOT treat metadata as indexed evidence.

#### Scenario: Newly added papers have PDF URLs

- **WHEN** `add_papers_to_project` creates project memberships with candidate HTTPS PDF URLs
- **THEN** it MUST enqueue at most three ingestion requests for papers created by that tool call
- **AND** its structured result MUST separately list added memberships, queued jobs, reused jobs,
  and papers requiring user upload
- **AND** URL safety MUST still be decided by the ingestion service, not by the model or data source.

#### Scenario: Existing or non-ingestible paper

- **WHEN** a paper already has a compatible active index, was not newly added, lacks a PDF URL, or
  exceeds the three-paper bound
- **THEN** the add action MUST NOT start an unnecessary build
- **AND** the result MUST preserve metadata membership while reporting the appropriate indexed,
  deferred, or upload-required state.

#### Scenario: Automatic ingestion fails

- **WHEN** an automatically queued job fails
- **THEN** the paper MUST remain in the project as metadata
- **AND** factual, comparison, and report capabilities MUST continue to reject it as full-text
  evidence
- **AND** the user MUST receive a retry or upload action instead of a false completion claim.

### Requirement: Evidence Board Ingestion Experience

The Evidence Board MUST present ingestion progress and recovery using familiar project-library
controls.

#### Scenario: Intermediate state

- **WHEN** a paper is pending, downloading, parsing, embedding, or committing
- **THEN** the row MUST show the current state and disable duplicate upload/ingest commands
- **AND** project papers and other workspace panels MUST remain usable.

#### Scenario: Failed state

- **WHEN** a paper job is failed
- **THEN** the row MUST show safe failure copy and an enabled retry command only when retryable
- **AND** upload MUST remain available when a new file is required.

#### Scenario: Indexed state

- **WHEN** a compatible active index is embedded
- **THEN** the row MUST show chunk count, embedding model/version, and indexed time
- **AND** it MUST NOT infer readiness merely from a PDF URL or completed metadata add action.
