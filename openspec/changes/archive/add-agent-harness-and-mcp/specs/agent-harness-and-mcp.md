# Spec delta: agent-harness-and-mcp

## ADDED Requirements

### Requirement: Execution harness
Agent chat MUST run through an execution harness instead of directly calling model/tools from views.

#### Scenario: Harness event
- **WHEN** a tool is executed
- **THEN** the harness emits and persists a tool event.

#### Scenario: Harness answer quality event
- **WHEN** an Agent run has composed its final answer
- **THEN** the harness emits and persists a `quality_check` event before `done`.
- **AND** the event reports whether the answer is grounded in project evidence, partial because of tool errors, blocked by policy, missing source markers, or lacking evidence.

### Requirement: Aggregate Agent quality report
The backend MUST provide a deterministic aggregate quality report for the project Agent core.

#### Scenario: Quality report command
- **WHEN** `python manage.py evaluate_agent_quality` is run
- **THEN** it reports pass/fail, overall score, duration, intent routing accuracy, Function Calling trajectory accuracy, RAG grounding metrics, prompt contract coverage, execution harness coverage, MCP schema drift, and data-source policy.

#### Scenario: Quality report logs
- **WHEN** the aggregate quality report starts and completes
- **THEN** it emits structured logs with event name, status, score, duration, intent accuracy, tool trajectory accuracy, grounded answer rate, and MCP safe-surface status.

#### Scenario: Quality report artifact
- **WHEN** `python manage.py evaluate_agent_quality --write-report` is run
- **THEN** it writes a JSON report under `backend/eval/reports/` for resume/demo evidence.

### Requirement: Project MCP tools
The MCP server MUST expose stable project-level read/query tools.

#### Scenario: MCP list tools
- **WHEN** MCP tools are listed
- **THEN** project RAG, project paper listing, and project citation graph tools are present.
