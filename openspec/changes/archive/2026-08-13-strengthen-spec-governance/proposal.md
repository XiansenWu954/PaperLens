## Why

PaperLens has accumulated useful hardening work, but some important constraints have remained only
in chat handoffs or test reports while older specs still imply broader framework ownership. Without
a durable traceability and conflict-resolution contract, repeated fixes can improve local tests yet
quietly move the product away from its approved V3 architecture.

## What Changes

- Make Codex, DS, and GLM ownership and handoff authority explicit.
- Require every material finding to be classified into spec, design, task, or internal evidence.
- Add a requirement-to-code-to-test traceability record and a phase-level drift verdict.
- Define conflict precedence and block silent reinterpretation of specs to make tests pass.
- Clarify that LangGraph owns durable research workflows, not normal project chat.
- Replace hardcoded model-cost behavior with configuration and measured quality/latency decisions.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `project-constitution`: Add role ownership, traceability, drift review, and conflict resolution.
- `agent-orchestration`: Restrict LangGraph to explicit durable research workflows and make model
  reasoning behavior configurable rather than a permanent architecture requirement.

## Impact

This is a governance and specification-alignment change. It updates OpenSpec, collaboration
instructions, and review practice without changing REST/SSE interfaces, production code, models,
retrieval, prompts, or framework dependencies. Existing implementation changes remain owned by
their original capability changes.
