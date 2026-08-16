# Spec: project-workspace

## Purpose

Define persistent research projects, runs, events, chat sessions, reports, and
dashboard APIs that organize PaperLens work.
## Requirements
### Requirement: Project workspace
PaperLens MUST persist research work inside `ResearchProject` records.

#### Scenario: Create project
- **WHEN** POST `/api/projects` with title and optional description
- **THEN** a project is created and returned.

### Requirement: Project runs

Agent and research operations MUST be represented as project-scoped runs with inspectable,
durable lifecycle state and safe events.

#### Scenario: Record event

- **WHEN** a project run emits a tool, status, workflow or error event
- **THEN** the event MUST be persisted with event type, allowlisted payload and timestamp
- **AND** it MUST preserve the Stage B EventPublisher safety boundary.

#### Scenario: Create a durable workflow run

- **WHEN** POST `/api/projects/<id>/workflows/research-expand` receives a valid question while the
  durable workflow subsystem is available
- **THEN** it MUST return 201 with one project-scoped workflow run
- **AND** the existing path and required response fields MUST remain compatible
- **AND** the response MUST add workflow phase, resume count, lifecycle timestamps, dependency
  summary and report ID without exposing owner tokens or checkpoint internals.

#### Scenario: Represent workflow lifecycle

- **WHEN** a workflow progresses through queueing, execution, ingestion waiting, completion,
  partial completion or failure
- **THEN** its status MUST be one of `pending`, `running`, `waiting_ingestion`, `done`, `partial`,
  or `error`
- **AND** lifecycle timestamps MUST be stored when the corresponding transition occurs.

#### Scenario: Record workflow dependency

- **WHEN** a workflow requires a project paper to be ready for RAG
- **THEN** it MUST store one dependency for that run and paper with status `ready`, `pending`,
  `succeeded`, `failed` or `unavailable`
- **AND** pending dependencies MUST reference their project-scoped ingestion job
- **AND** foreign, excluded or unlinked papers MUST NOT become workflow dependencies.

#### Scenario: Record dependency terminal state

- **WHEN** a dependency is verified `ready` or reaches `succeeded`, `failed` or `unavailable`
- **THEN** it MUST record a terminal timestamp and stable error code when applicable
- **AND** the parent run MUST track the latest terminal timestamp across its dependencies.

#### Scenario: Keep ingestion execution ownership private

- **WHEN** a project ingestion job is claimed, heartbeated, recovered or terminalized
- **THEN** its execution token, heartbeat and execution-lease expiry MUST remain server-internal
- **AND** serializers, API responses, events, logs, checkpoints and Celery result payloads MUST NOT
  expose those fields
- **AND** terminalization MUST clear the execution lease without changing the immutable active-index
  contract.

#### Scenario: Record deduplicated event

- **WHEN** a workflow emits a node, wait, resume, retrieval, completion or failure event
- **THEN** the event MUST pass through EventPublisher with allowlisted safe fields and correlation IDs
- **AND** a non-empty event dedupe key MUST be unique within the run
- **AND** repeated delivery of the same logical transition MUST NOT create another event.

#### Scenario: Preserve project isolation

- **WHEN** workflow dependencies, events or run details are listed
- **THEN** only records belonging to the requested project MUST be returned
- **AND** foreign project IDs, paper data, job data and report data MUST fail closed without
  existence disclosure.

### Requirement: Report versions

Project reports MUST be versioned instead of overwritten, and workflow-produced reports MUST have
unique run ownership.

#### Scenario: Create report version

- **WHEN** POST `/api/projects/<id>/reports` creates a user or Agent report outside a durable workflow
- **THEN** a new report version MUST be saved and listed newest-first
- **AND** its workflow source run MAY be absent.

#### Scenario: Persist workflow report once

- **WHEN** a durable workflow passes its deterministic evidence and citation gates
- **THEN** it MUST create at most one ReportVersion whose source run is that workflow ProjectRun
- **AND** duplicate resume or worker redelivery MUST return or reuse that report rather than create
  another version.

#### Scenario: Persist partial workflow report

- **WHEN** a workflow completes with usable evidence but one or more dependencies failed or were
  unavailable
- **THEN** its report MUST identify the run as partial and include a structured evidence-gap summary
- **AND** failed papers MUST NOT be presented as full-text-supported sources.

#### Scenario: Reject unsupported workflow report

- **WHEN** a workflow has no resolved answer-bound full-text evidence
- **THEN** no workflow-owned report MUST be created
- **AND** the run MUST expose only a stable failure code and safe user-facing status.

#### Scenario: Report lifecycle logging

- **WHEN** report versions are listed or saved
- **THEN** the backend MUST log request-correlated project/report lifecycle events with safe metadata
  such as project ID, report ID, source run ID, source, content length, duration and status
- **AND** full report content, report title derived from a question, prompts and evidence excerpts
  MUST NOT be written to application logs.
