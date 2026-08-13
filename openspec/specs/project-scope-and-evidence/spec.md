# Spec: project-scope-and-evidence

## Purpose

Define trusted project authorization, canonical scope, typed evidence, deterministic
citation resolution, capability policy, and safe observability contracts.

## Requirements

### Requirement: Trusted Tool Execution Context

Every project Agent and MCP tool call MUST receive project authorization from a
server-created execution context rather than model-provided arguments.

#### Scenario: Model attempts project override

- **GIVEN** a run belongs to project A
- **WHEN** model output includes project B's `project_id` or another authorization field
- **THEN** the executor MUST call the tool with project A from trusted context
- **AND** the model-provided field MUST NOT reach the tool implementation
- **AND** a safe scope-violation audit event MUST be persisted.

#### Scenario: In-application Function Calling schema exposure

- **WHEN** an in-application Function Calling schema is generated for a project tool
- **THEN** the model-controlled schema MUST NOT expose project, run, session, or actor identity
- **AND** unknown input properties MUST be rejected.

#### Scenario: External MCP project selection

- **WHEN** an external MCP call has no pre-established project session context
- **THEN** its schema MAY accept a project ID as a resource selector
- **AND** the MCP adapter MUST authorize the selector before constructing trusted execution context
- **AND** the project tool implementation MUST receive identity only from that trusted context.

### Requirement: Central Project Scope Resolution

All project-level paper, chunk, graph, report-evidence, and MCP queries MUST use
one project scope resolver.

#### Scenario: Read current project paper

- **GIVEN** a non-excluded paper is linked to the current project
- **WHEN** a project tool reads its metadata or chunks
- **THEN** the resolver MUST return only that project's permitted resource.

#### Scenario: Read another project's paper

- **GIVEN** a paper or chunk exists globally but is not linked to the current project
- **WHEN** any project tool, RAG query, report operation, graph operation, or MCP call requests it
- **THEN** no metadata, excerpt, existence signal, or graph artifact from that resource may be returned
- **AND** the response MUST use the same scoped not-found semantics as a nonexistent resource.

#### Scenario: Excluded project paper

- **GIVEN** a ProjectPaper membership has status `excluded`
- **WHEN** a RAG, full-text read, comparison, graph, report, or other evidence-bearing operation runs
- **THEN** the paper and all of its chunks MUST be excluded from evidence.

#### Scenario: Explicit library inventory includes excluded membership

- **GIVEN** a ProjectPaper membership has status `excluded`
- **WHEN** the user or an inventory tool explicitly lists project library memberships
- **THEN** the paper MAY be returned with its `excluded` status so the user can inspect or restore it
- **AND** no foreign or globally unlinked paper may be returned
- **AND** the excluded paper's chunks MUST NOT be represented as usable evidence.

#### Scenario: Empty requested paper set

- **GIVEN** a caller explicitly requests an empty set of paper IDs
- **WHEN** the resolver builds the project query
- **THEN** the result MUST be empty
- **AND** it MUST NOT fall back to the current project or global paper set.

#### Scenario: Default project set

- **GIVEN** the caller does not provide a paper subset
- **WHEN** the resolver builds the project query
- **THEN** it MUST use all current non-excluded project memberships
- **AND** this omitted-subset state MUST be represented differently from an empty list.

### Requirement: Typed Full-Text Evidence

Full-text evidence used by project answers MUST carry a stable, database-verifiable identity.

#### Scenario: Produce full-text evidence

- **WHEN** project RAG, paper-section reading, or paper comparison returns a full-text chunk
- **THEN** it MUST produce an EvidenceEnvelope containing project ID, paper ID, database chunk ID,
  content hash, page/section metadata, retrieval source, and embedding version
- **AND** a positional chunk index alone MUST NOT identify the evidence.

#### Scenario: Metadata-only result

- **WHEN** a search, list, graph, or non-ingested paper produces metadata without a full-text chunk
- **THEN** it MUST be represented as metadata or a structured action artifact
- **AND** it MUST NOT be labeled as full-text evidence.

#### Scenario: Legacy tool result

- **WHEN** a migration-period tool result lacks a verifiable EvidenceEnvelope
- **THEN** the result MAY remain visible as a legacy artifact
- **AND** it MUST be marked unresolved and MUST NOT satisfy a full-text evidence gate.

### Requirement: Deterministic Citation Reference Resolution

Citation reference resolution MUST be based on current database state and project scope,
not on marker presence or Harness-created fields.

#### Scenario: Resolve a valid citation

- **GIVEN** an answer references an EvidenceEnvelope
- **WHEN** the citation resolver verifies the reference
- **THEN** the cited project, non-excluded paper, active chunk, and content hash MUST all match
- **AND** only then may `reference_resolution_status` be `resolved`.

#### Scenario: Marker without evidence

- **WHEN** an answer contains a citation-like marker that does not map to a valid EvidenceEnvelope
- **THEN** marker presence MAY be recorded
- **AND** reference resolution MUST be `unresolved`
- **AND** answer evidence tier MUST NOT become fulltext.

#### Scenario: Citation points outside project

- **WHEN** a marker or envelope points to another project's paper or chunk
- **THEN** resolution MUST fail without disclosing the foreign resource
- **AND** a safe scope-violation reason MUST be recorded.

#### Scenario: Stale chunk version

- **WHEN** a citation points to a chunk whose content hash or active index version has changed
- **THEN** resolution MUST be `unresolved`
- **AND** the system MUST require regeneration or re-binding rather than silently selecting another chunk.

### Requirement: Separate Evidence Quality Dimensions

Retrieval, reference resolution, citation binding, and claim support MUST be stored and
evaluated as separate dimensions.

#### Scenario: Evidence retrieved but unused

- **GIVEN** tools retrieved valid full-text evidence
- **WHEN** the answer contains no resolved reference to it
- **THEN** retrieval status MAY report full-text availability
- **AND** answer evidence tier MUST be `none`
- **AND** citation binding MUST be `unbound` when citation is required.

#### Scenario: Reference resolved but claim not judged

- **WHEN** an answer reference resolves to a valid project chunk but semantic support has not run
- **THEN** reference resolution MUST be `resolved`
- **AND** claim support MUST remain `pending`
- **AND** the Harness MUST NOT promote it to `supported`.

#### Scenario: Claim-level judge

- **WHEN** a deterministic rule or independent Judge evaluates a claim
- **THEN** it MUST receive only the claim and its resolved evidence excerpts/IDs
- **AND** it MUST output `supported`, `contradicted`, or `insufficient` separately from reference resolution.

### Requirement: Capability-Aware Evidence Policy

The Harness MUST decide minimum evidence from a structured capability contract rather than
answer keywords or generic evidence count.

#### Scenario: Factual project answer

- **WHEN** the Agent answers a factual question about project paper content
- **THEN** at least one resolved full-text citation MUST support the returned answer
- **AND** otherwise the user-visible answer MUST abstain or request more evidence.

#### Scenario: Multi-paper comparison

- **WHEN** the Agent compares two or more project papers
- **THEN** every compared paper MUST contribute resolved full-text evidence
- **AND** missing evidence for one side MUST be disclosed rather than filled from model knowledge.

#### Scenario: Report drafting

- **WHEN** the Agent drafts a factual report section
- **THEN** factual claims MUST bind to resolved project full-text evidence
- **AND** metadata-only candidates MUST be identified as hypotheses or omitted.

#### Scenario: Action result

- **WHEN** the Agent lists, searches, exports, adds, or describes a graph artifact without making
  paper-content claims
- **THEN** metadata or the structured tool artifact MAY satisfy the action
- **AND** the Harness MUST NOT require unrelated full-text citations.

#### Scenario: No relevant evidence

- **WHEN** the current project has no relevant full-text evidence for a factual request
- **THEN** the Agent MUST return an abstention or an explicit evidence-expansion option
- **AND** it MUST NOT cite unrelated project papers merely to appear grounded.

### Requirement: Scope And Evidence Observability

Scope decisions and citation resolution MUST be auditable without logging sensitive content.

#### Scenario: Tool audit event

- **WHEN** a project tool completes or is denied
- **THEN** the event MUST include request ID, project ID, run ID, tool name, scoped paper count,
  result count, status, duration, and safe reason code
- **AND** it MUST NOT include API keys, complete prompts, full excerpts, or paper bodies.

#### Scenario: Citation resolution event

- **WHEN** answer citations are resolved
- **THEN** the event MUST report total, resolved, unresolved, and reason counts
- **AND** it MUST be possible to trace the event to the same project run.

### Requirement: Project Scope Compatibility

Scope hardening MUST preserve current public product interfaces while closing unauthorized paths.

#### Scenario: Existing project RAG API

- **WHEN** a permitted caller invokes `query_project_rag(project_id, question, k)`
- **THEN** the callable contract and existing API path MUST remain available
- **AND** its internal retrieval MUST use trusted project scope and typed evidence.

#### Scenario: Existing MCP read tool

- **WHEN** an existing project MCP read tool is invoked with valid context
- **THEN** its tool name MUST remain stable
- **AND** its result MUST conform to a validated output schema
- **AND** unauthorized project data MUST remain inaccessible.
