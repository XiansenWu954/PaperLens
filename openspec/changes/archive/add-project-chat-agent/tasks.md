# add-project-chat-agent Tasks

- [x] Add chat session/message models and migrations.
- [x] Add project Agent tool schemas and execution functions.
- [x] Add streaming chat endpoint with token/tool/evidence/done/error events.
- [x] Persist user/assistant messages and run events.
- [x] Add frontend session restore, run summary, tool trace, evidence, and added-paper visibility.
- [x] Add tests for RAG chat, search-add behavior, and no autonomous delete.
- [x] Verify with `python manage.py test api agent rag`.

Verification recorded on 2026-07-31:

- `python manage.py test papers api eval` -> 30 tests passed.
- `python manage.py test api realtime papers datasources rag citation agent mcp_server eval` -> 117 tests passed.
- `python manage.py evaluate_project_agent` -> passed with network search case intentionally skipped by default.

Additional verification recorded on 2026-07-31:

- `npm run build` -> passed after Agent Chat panel refactor.
- Browser check at `http://127.0.0.1:5176/projects/<demo>` -> session list, prompt chips, streamed run summary, tool events, evidence, added papers, and responsive layout verified.

Quality/usability correction recorded on 2026-08-01:

- Reduced Agent Chat visible chrome so assistant messages are answer-first, with session history and tool traces collapsed by default.
- Added compact chips for quality verdict, evidence sources, and newly added papers directly under the assistant answer.
- Added `quality_check` stream handling in the frontend.
- `wsl bash -lc "cd /mnt/d/aiproducts/PaperLens/backend && . .venv/bin/activate && python manage.py test agent.tests.ProjectIntentClassifierTest agent.tests.ProjectHarnessToolRoutingTest eval.tests.ProjectAgentHarnessEvalTest"` -> 18 tests passed.
- `wsl bash -lc "cd /mnt/d/aiproducts/PaperLens/backend && . .venv/bin/activate && python manage.py evaluate_intents"` -> 31/31 cases passed.
- `wsl bash -lc "cd /mnt/d/aiproducts/PaperLens/backend && . .venv/bin/activate && python manage.py evaluate_project_agent"` -> 7/7 cases passed, including quality verdicts; search/add is judged `grounded`.
- `npm run build` -> passed.
