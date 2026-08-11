# Spec delta: agent-harness-and-mcp

## MODIFIED Requirements

### Requirement: Project Tool Scope

Project Agent tools MUST respect the current project paper library boundary through the
canonical ProjectScopeResolver, including paper metadata, full-text chunks, report evidence,
citation graphs, and MCP calls.

Library inventory scope MAY include excluded memberships with explicit status; evidence scope
MUST exclude them. Both scopes MUST exclude foreign and globally unlinked papers.

#### Scenario: Project RAG scope

- **WHEN** project RAG is queried
- **THEN** evidence MUST be drawn only from non-excluded papers linked to trusted context's project
- **AND** an explicitly empty paper subset MUST return no evidence rather than global evidence.

#### Scenario: Citation graph scope

- **WHEN** the project citation graph is generated
- **THEN** graph nodes MUST be drawn only from non-excluded papers linked to trusted context's project.

#### Scenario: Scope implementation source

- **WHEN** a new project-level tool or retrieval capability is added
- **THEN** it MUST use the canonical resolver and trusted execution context
- **AND** it MUST add current/foreign/excluded/empty-scope tests before release.

#### Scenario: Non-destructive add

- **WHEN** papers are added to a project through the Agent tool
- **THEN** global papers MUST be preserved and project membership MUST be idempotent
- **AND** the target project MUST come from trusted execution context.

### Requirement: Harness Answer Quality Event

The Harness quality event MUST report separate retrieval, reference-resolution,
citation-binding, and claim-support states.

#### Scenario: Quality event semantics

- **WHEN** a chat run emits `quality_check`
- **THEN** no single `grounded` or `verified` boolean may substitute for the four dimensions
- **AND** any compatibility verdict MUST be derived from the structured dimensions and capability policy.

#### Scenario: Reference validation source

- **WHEN** the Harness reports a resolved citation reference
- **THEN** that status MUST come from the database-backed CitationResolver
- **AND** the Harness MUST NOT infer resolution solely from marker presence or tool-result fields.

### Requirement: Project MCP Tools

The MCP server MUST expose stable project-level read/query tools through the same trusted
scope and typed evidence contracts used by the in-application Agent.

#### Scenario: MCP project tool call

- **WHEN** an external client calls a project MCP tool
- **THEN** the adapter MUST authorize or establish project context before dispatch
- **AND** the call MUST use the audited project tool executor
- **AND** its structured result MUST conform to the declared output schema.

#### Scenario: MCP excludes internal mutation tools

- **WHEN** MCP tools are listed
- **THEN** project deletion, paper deletion, project clearing, and report overwrite tools MUST be absent.
