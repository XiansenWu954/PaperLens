# add-agent-harness-and-mcp Tasks

- [x] Add execution harness module for project chat.
- [x] Add structured project chat run lifecycle logs for started/completed/failed states.
- [x] Add evaluation harness command/data.
- [x] Add demo seed command.
- [x] Extend MCP server with project-level tools.
- [x] Add tests for harness tool policy and MCP tool listing.
- [x] Verify with `python manage.py test agent mcp_server eval`.

Verification recorded on 2026-07-31:

- `python manage.py evaluate_project_agent` -> passed.
- `python manage.py seed_demo_project --title "PaperLens Demo"` -> created demo project.
- `python manage.py test api realtime papers datasources rag citation agent mcp_server eval` -> 117 tests passed.

Additional verification recorded on 2026-07-31:

- `wsl bash -lc "cd /mnt/d/aiproducts/PaperLens/backend && . .venv/bin/activate && python manage.py test agent.tests.ProjectHarnessToolRoutingTest"` -> 4 tests passed, including lifecycle log sanitization and failed-run logging.

Quality/usability correction recorded on 2026-08-01:

- Added per-tool timeout handling in `ProjectAgentHarness` so stalled external search/RAG calls return structured partial results instead of freezing the chat stream.
- Added `quality_check` harness event and evaluation scoring for answer grounding/source markers/tool errors.
- Aligned quality checking with evaluator source-marker rules: citation, title, and docname all count as valid grounding markers.
- Added an intent golden case for "continue/expand the paper scope" so project chat can broaden research from the chat window.
- Fixed two defects found during implementation:
  - blocked destructive requests previously reached answer-quality logic without initialized context.
  - harness timeout logging used reserved logging field `message`; it now logs the timeout text as `error_message`.
- `wsl bash -lc "cd /mnt/d/aiproducts/PaperLens/backend && . .venv/bin/activate && python manage.py test agent.tests.ProjectIntentClassifierTest agent.tests.ProjectHarnessToolRoutingTest eval.tests.ProjectAgentHarnessEvalTest"` -> 18 tests passed.
- `wsl bash -lc "cd /mnt/d/aiproducts/PaperLens/backend && . .venv/bin/activate && python manage.py check"` -> passed.
- `wsl bash -lc "cd /mnt/d/aiproducts/PaperLens/backend && . .venv/bin/activate && python manage.py test api realtime papers datasources rag citation agent mcp_server eval"` -> 152 tests passed.
- `wsl bash -lc "cd /mnt/d/aiproducts/PaperLens/backend && . .venv/bin/activate && python manage.py evaluate_project_agent"` -> 7/7 cases passed.

Aggregate quality metrics recorded on 2026-08-01:

- Added `python manage.py evaluate_agent_quality` for one-command Agent core scoring across intent routing, Function Calling, RAG grounding, prompt contracts, execution harness events/recovery, MCP schema drift, and DBLP default data-source policy.
- Added `--write-report` JSON artifact output under `backend/eval/reports/`.
- Added structured `eval.agent_quality` logs for `agent_quality_evaluation_started` and `agent_quality_evaluation_completed`.
- Added tests for aggregate resume metrics and evaluator lifecycle log fields.
- Fixed MCP schema compatibility in the evaluator by reading `input_schema`.
- Added `expected_tools` to project Agent harness diagnostics for auditable tool trajectories.
- `wsl bash -lc "cd /mnt/d/aiproducts/PaperLens/backend && . .venv/bin/activate && python manage.py test eval.tests.AgentQualityEvalTest eval.tests.ProjectAgentHarnessEvalTest"` -> 5 tests passed.
- `wsl bash -lc "cd /mnt/d/aiproducts/PaperLens/backend && . .venv/bin/activate && python manage.py evaluate_agent_quality --write-report"` -> passed, score 1.0, wrote `backend/eval/reports/agent_quality_20260801_101212.json`.
