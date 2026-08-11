# add-project-chat-agent Design

## SOTA/Concept Fit
- Function Calling is used only for auditable tools: project RAG, search papers, add papers, refresh graph, draft report section.
- Prompt Engineering is separated into intent/router, chat responder, and report writer prompts with strict citation constraints.
- The first implementation may use deterministic harness routing for tests and demos; LLM routing can replace it later without changing tools.

## Safety
- Destructive actions are not exposed as autonomous tools.
- Chat answers must prefer project evidence and cite available `pqac-*` keys when evidence exists.
