# Spec delta: workbench-frontend-v2

## ADDED Requirements

### Requirement: Dashboard
The frontend MUST provide a dashboard listing projects and a demo entry point.

#### Scenario: Empty dashboard
- **WHEN** no projects exist
- **THEN** the user can create or seed a project.

### Requirement: Project workspace
The frontend MUST provide project paper, report, graph, and chat panels.

#### Scenario: Chat event visibility
- **WHEN** chat streams tool events
- **THEN** the Chat Panel shows tool activity separately from assistant text.
