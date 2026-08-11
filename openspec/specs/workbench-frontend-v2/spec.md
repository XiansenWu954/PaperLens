# Spec: workbench-frontend-v2

## Purpose

Define the project workbench UI for dashboard, evidence, reports, citation graph,
chat, and responsive task states.

## Requirements

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

#### Scenario: Live chat answer readability
- **WHEN** the assistant streams a live model answer with Markdown formatting
- **THEN** the Chat Panel renders escaped Markdown into readable headings, lists, emphasis, and inline code.
- **AND** raw operational summaries do not visually compete with the answer.

#### Scenario: Search result chips
- **WHEN** the Agent searches external papers from chat
- **THEN** candidate papers are shown as compact selectable/inspectable chips or rows separate from the final answer.

### Requirement: Interaction consistency
Workbench controls MUST follow common user expectations for navigation, forms, tabs, destructive actions, and mobile layout.

#### Scenario: Real navigation
- **WHEN** a header item looks like navigation
- **THEN** it is a real route link or is not rendered as navigation.

#### Scenario: Form submission
- **WHEN** the user submits a project/search/chat form
- **THEN** Enter/button behavior, disabled states, labels, and error recovery are consistent with the visible task.

#### Scenario: Tab semantics
- **WHEN** a workspace panel is selected through tabs or segmented controls
- **THEN** the active control exposes selected state and does not rely on color alone.

#### Scenario: Destructive project actions
- **WHEN** the user removes a paper from the project library
- **THEN** the frontend asks for explicit confirmation before emitting the removal action.

#### Scenario: Unsaved report drafts
- **WHEN** the user edits a report draft and attempts to switch context
- **THEN** the frontend warns before discarding unsaved content.

#### Scenario: Inspectors and traces
- **WHEN** the frontend shows tool traces, run events, or diagnostics
- **THEN** the default view prioritizes the user-facing answer/artifact and keeps diagnostics available in a secondary or collapsed surface.

#### Scenario: Desktop and mobile overflow check
- **WHEN** Dashboard or Project Workspace is viewed on desktop or mobile widths
- **THEN** the main workbench, Agent Chat, Evidence Board, Report Studio, and diagnostics do not create horizontal overflow.

### Requirement: Report Studio artifact workflow
The frontend MUST let users manage report versions as project artifacts instead of showing only the newest generated text.

#### Scenario: Select report version
- **WHEN** multiple report versions exist
- **THEN** the user can select a version and read its rendered Markdown content.

#### Scenario: Save manual report version
- **WHEN** the user creates or edits a report draft and saves it
- **THEN** the frontend calls the existing project reports API and selects the newly saved version.

#### Scenario: Audit report evidence anchors
- **WHEN** a report is displayed or edited
- **THEN** the frontend shows a lightweight evidence audit covering project-paper title hits, `pqac-*` citation markers, explicit source markers, and active project papers not covered by the report.

#### Scenario: Responsive report workflow
- **WHEN** the Project Workspace is viewed at mobile width
- **THEN** Report Studio controls, version list, audit panel, editor, and reader do not create horizontal overflow.
