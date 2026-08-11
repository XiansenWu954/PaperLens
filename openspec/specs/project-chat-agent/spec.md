# Spec: project-chat-agent

## Purpose

Define persistent project chat, project-scoped retrieval and tool use,
evidence-bound answers, and streaming conversation behavior.

## Requirements

### Requirement: Project chat
Each project MUST support a persistent chat session.

#### Scenario: Ask project question
- **WHEN** POST `/api/projects/<id>/chat` with a message
- **THEN** a chat session/message is stored and an answer is returned or streamed.
- **AND** the assistant response prioritizes the answer itself before operational trace details.

#### Scenario: Restore project chat session
- **WHEN** the frontend opens a project workspace with existing chat sessions
- **THEN** it can load persisted sessions, select a session, and show stored user/assistant messages.

#### Scenario: Stream tool trace
- **WHEN** the chat Agent streams a run
- **THEN** the frontend keeps tool/process details available separately from assistant prose.
- **AND** run id, session id, detected intent, planned tools, tool calls, tool results, evidence count, added papers, graph updates, completion, and errors are available in a collapsed or secondary trace surface.

#### Scenario: Evidence and library artifacts
- **WHEN** a chat run returns evidence or adds papers
- **THEN** the frontend surfaces evidence titles and newly added/existing paper titles so the user can understand what changed in the project library.

#### Scenario: Continue project exploration with library expansion
- **WHEN** the user explicitly asks to add, import, include, or put more papers into the project library
- **THEN** the chat Agent routes through paper search, safe project add, and project RAG answer tools.
- **AND** the answer explains what was added or what evidence remains available without requiring the user to manually leave chat.

#### Scenario: Explore follow-up search directions without mutation
- **WHEN** the user asks for limitations, gaps, future work, or search directions without explicitly asking to add papers
- **THEN** the chat Agent first queries project RAG evidence and then searches external sources for candidate directions.
- **AND** the chat Agent MUST NOT add papers to the project library in this path.
- **AND** the stream includes searchable candidate papers separately from the final answer.

#### Scenario: Project RAG grounding
- **WHEN** the user asks a question about the current project papers
- **THEN** the Agent queries only the current project's non-excluded library evidence.
- **AND** the answer includes source markers from project evidence whenever evidence is available.

#### Scenario: Full-text PDF evidence
- **WHEN** project papers have PDF-ingested `Text` chunks
- **THEN** project chat answers MUST prefer full-text evidence over metadata fallback.
- **AND** metadata fallback is used only for project papers that genuinely have no vector chunks.
- **AND** the answer MUST distinguish full-text conclusions from metadata-only hypotheses.

#### Scenario: Citation graph explanation
- **WHEN** the user asks to explain or refresh the project Citation Map
- **THEN** the Agent MUST use `get_project_citation_graph`.
- **AND** the answer MUST include graph node count, edge count, representative node titles, and relationship basis from graph artifacts.
- **AND** the answer MUST NOT invent topical relationships that are not present in graph nodes, edges, or `referenced_works`.

#### Scenario: Live answer synthesis
- **WHEN** live chat LLM mode is enabled and DeepSeek is reachable
- **THEN** the final assistant answer is synthesized by the model from the tool context and recent session history.
- **AND** the answer MUST prioritize the user's requested result over process narration.
- **AND** the answer MUST avoid fake `pqac-*` keys and avoid unsupported claims.

#### Scenario: Live answer fallback
- **WHEN** the live model call fails or times out
- **THEN** the harness returns a deterministic fallback answer from tool context.
- **AND** the stream and logs mark the fallback status without exposing API keys, full prompts, or full paper text.

#### Scenario: Quality check event
- **WHEN** a chat Agent run completes or is blocked
- **THEN** the stream includes a `quality_check` event with evidence count, source marker count, tool error count, and a verdict.
- **AND** possible verdicts distinguish grounded answers, partial/tool-error answers, blocked destructive requests, missing source markers, and insufficient evidence.

### Requirement: Safe autonomous tools
Agent chat MAY search and add papers but MUST NOT autonomously delete project papers.

#### Scenario: Tool policy
- **WHEN** tool schemas are listed for chat Agent
- **THEN** no delete or clear-project tool is present.
