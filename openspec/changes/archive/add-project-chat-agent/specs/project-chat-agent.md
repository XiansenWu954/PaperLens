# Spec delta: project-chat-agent

## ADDED Requirements

### Requirement: Project chat
Each project MUST support a persistent chat session.

#### Scenario: Ask project question
- **WHEN** POST `/api/projects/<id>/chat` with a message
- **THEN** a chat session/message is stored and an answer is returned or streamed.

### Requirement: Safe autonomous tools
Agent chat MAY search and add papers but MUST NOT autonomously delete project papers.

#### Scenario: Tool policy
- **WHEN** tool schemas are listed for chat Agent
- **THEN** no delete or clear-project tool is present.
