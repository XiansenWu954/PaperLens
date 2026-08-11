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
Agent and research operations MUST be represented as project-scoped runs with inspectable events.

#### Scenario: Record event
- **WHEN** a project run emits a tool, status, or error event
- **THEN** the event is persisted with event type, payload, and timestamp.

### Requirement: Report versions
Project reports MUST be versioned instead of overwritten.

#### Scenario: Create report version
- **WHEN** POST `/api/projects/<id>/reports`
- **THEN** a new report version is saved and listed newest-first.

#### Scenario: Report lifecycle logging
- **WHEN** report versions are listed or saved
- **THEN** the backend logs request-correlated project/report lifecycle events with safe metadata such as project id, report id, title preview, source, content length, duration, and status.
- **AND** full report content is not written to application logs.
