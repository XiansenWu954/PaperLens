# add-agent-harness-and-mcp Design

## Concept Boundaries
- Execution Harness: run orchestration, tool policy, events, logging, and response streaming.
- Evaluation Harness: fixed cases for retrieval quality, citation faithfulness, and tool correctness.
- Demo Harness: creates a project with sample papers/report/chat for resume demos.
- MCP: external access to stable PaperLens tools only.

## MCP Tools
- `search_papers`
- `query_project_rag`
- `list_project_papers`
- `get_project_citation_graph`
