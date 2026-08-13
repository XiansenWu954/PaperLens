# add-project-workspace Design

## SOTA/Concept Fit
- Agent run state follows open_deep_research-style run trace concepts: every run has inputs, outputs, status, and step events.
- This is not an Agent concept by itself; it is the durable application substrate that lets Harness and UI show what happened.

## Model Shape
- `ResearchProject`: title, description, status, timestamps.
- `ProjectRun`: project-scoped run with kind/status/question/output fields.
- `ProjectRunEvent`: append-only event list for tool calls, steps, errors, and observable progress.
- `ReportVersion`: versioned project report content.

## Compatibility
- Existing `ResearchTask` remains and gains optional project linkage rather than being removed.
- Existing `/api/research` behavior remains usable.
