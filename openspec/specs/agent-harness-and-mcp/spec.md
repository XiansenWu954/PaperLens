# Spec: agent-harness-and-mcp

## Purpose

Define the execution harness, project tool observability and scope, quality evaluation,
and stable MCP read surface for PaperLens agents.

## Requirements

### Requirement: Execution harness
Agent chat MUST run through an execution harness instead of directly calling model/tools from views.

#### Scenario: Harness event
- **WHEN** a tool is executed
- **THEN** the harness emits and persists a tool event.

#### Scenario: Harness tool timeout
- **WHEN** a project Agent tool exceeds the configured execution timeout
- **THEN** the harness emits a structured `tool_result` error instead of hanging the chat stream.
- **AND** the final answer is marked partial and keeps the session usable.

#### Scenario: Live model observability
- **WHEN** live DeepSeek answer synthesis is enabled
- **THEN** the harness emits `llm_call` and `llm_result` events around the model call.
- **AND** those events expose model name, duration, answer length, and fallback status without storing API keys, full prompts, or full paper text.

#### Scenario: Harness run lifecycle logs
- **WHEN** a project chat run starts, completes, or fails
- **THEN** the harness logs safe structured lifecycle events with request, project, run, and session IDs, status, duration, detected intent, and allowlisted metrics when available.
- **AND** user-message text, prompts, evidence excerpts, paper text, API keys, and raw exception messages are not logged.

### Requirement: Project tool observability
Project Agent tools MUST emit structured logs when they are executed directly through the tool executor.

#### Scenario: Project tool completion
- **WHEN** `execute_project_tool` completes a tool call
- **THEN** it logs the trusted correlation IDs, sanitized tool name, duration, status, safe reason code, and allowlisted numeric/result summary.

#### Scenario: Sensitive input handling
- **WHEN** tool arguments contain a query or question
- **THEN** logs MUST NOT contain the query, question, prompt, evidence excerpt, paper text, or raw argument payload.
- **AND** the system MAY record safe lengths, counts, or irreversible hashes.

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

### Requirement: Intent classifier golden matrix
The deterministic project Agent intent classifier MUST be evaluated against a golden matrix before adding an LLM router.

#### Scenario: Generic paper wording
- **WHEN** a user asks to compare, summarize, or explain "papers"
- **THEN** the classifier routes to project RAG/report tools instead of listing the library unless an explicit list/show inventory action is present.

#### Scenario: Citation source request
- **WHEN** a user asks the answer to cite paper sources
- **THEN** the classifier routes to project RAG instead of citation graph unless a graph/map/network action is present.

#### Scenario: Intent evaluation command
- **WHEN** `python manage.py evaluate_intents` is run
- **THEN** it reports expected/actual intent, tools, blocked status, and failure diagnostics for the golden matrix.

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

### Requirement: Live Agent output evaluation
The backend MUST provide an optional network evaluation for real model output quality.

#### Scenario: Live evaluation command
- **WHEN** `python manage.py evaluate_live_agent --include-network --write-report` is run with a valid DeepSeek key
- **THEN** it creates an archived scratch project, executes live project chat cases, evaluates answer usefulness and grounding, and writes a JSON report.

#### Scenario: Live evaluation failure diagnostics
- **WHEN** a live case fails due to tool routing, missing source markers, insufficient evidence, or low critic score
- **THEN** the report includes the final answer, expected tools, actual tools, evidence count, search results, added papers, source marker status, and critic notes.

#### Scenario: Structured graph evaluation
- **WHEN** a live evaluation case is based on deterministic Citation Map artifacts
- **THEN** the evaluator MAY use structured checks instead of an LLM critic.
- **AND** those checks MUST verify graph counts, representative nodes, edge details when present, and the `referenced_works` relationship basis.
- **AND** the case MUST still verify expected tool calls and forbidden tool policy.

### Requirement: PDF/RAG quality evaluation
The backend MUST provide a repeatable quality gate for the PDF ingestion and project RAG chain.

#### Scenario: PDF/RAG evaluation command
- **WHEN** `python manage.py evaluate_pdf_rag --write-report` is run
- **THEN** it creates an archived scratch project, generates local PDF fixtures, ingests PDF bytes, persists text chunks, runs project-scoped RAG cases, and writes a JSON report.

#### Scenario: PDF/RAG live answer evaluation
- **WHEN** `python manage.py evaluate_pdf_rag --include-live-agent --use-critic --write-report` is run with a valid DeepSeek key
- **THEN** it validates the final full-text Agent answer with a live model call and critic.
- **AND** the answer MUST include project evidence source markers and core expected terms from the full-text evidence.

#### Scenario: PDF/RAG evaluation logs
- **WHEN** PDF/RAG evaluation or PDF ingestion runs
- **THEN** it emits structured logs for evaluation start/completion, fixture ingestion, case completion, PDF ingestion completion/failure, and chunk persistence.
- **AND** logs MUST NOT include API keys, full prompts, full paper text, or full PDF contents.

### Requirement: Project MCP Tools
The MCP server MUST expose stable project-level read/query tools through the same trusted
scope and typed evidence contracts used by the in-application Agent.

#### Scenario: MCP list tools
- **WHEN** MCP tools are listed
- **THEN** project paper search, project RAG, project paper listing, and project citation graph tools are present.

#### Scenario: MCP schema source
- **WHEN** project MCP tool schemas are built
- **THEN** they match the corresponding `PROJECT_AGENT_TOOLS` Function Calling contracts.

#### Scenario: MCP excludes internal mutation tools
- **WHEN** MCP tools are listed
- **THEN** project deletion, paper deletion, project clearing, report overwrite, and internal
  project write/drafting tools MUST be absent.

#### Scenario: MCP project tool call
- **WHEN** an external client calls a project MCP tool
- **THEN** the adapter MUST authorize or establish project context before dispatch
- **AND** the call MUST use the audited project tool executor
- **AND** its structured result MUST conform to the declared output schema.
