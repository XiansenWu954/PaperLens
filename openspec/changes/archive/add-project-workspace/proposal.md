# add-project-workspace Proposal

PaperLens V2 must become a project-oriented research workspace instead of a single-run demo. This change adds the durable project/run/report/event backbone used by the library, chat agent, frontend, and demo harness.

## Why
- Resume-grade Agent projects need durable state, inspectable runs, and repeatable demos.
- A project boundary lets RAG, citation graph, reports, and chat share the same paper collection.

## Scope
- Add project, run, run-event, and report-version concepts.
- Keep existing `/api/research` compatibility.
- Do not add multi-user permissions or background workers in this change.
