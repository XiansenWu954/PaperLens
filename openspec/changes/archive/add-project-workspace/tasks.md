# add-project-workspace Tasks

- [x] Add project/run/event/report models and migrations.
- [x] Add serializers and REST endpoints for project CRUD and reports.
- [x] Preserve `/api/research` compatibility.
- [x] Add tests for project CRUD, run event persistence, and report versions.
- [x] Add structured report list/save lifecycle logs and tests that full report content is not logged.
- [x] Verify with `python manage.py test api`.

Verification recorded on 2026-07-31:

- `python manage.py check` -> passed.
- `python manage.py test api realtime papers datasources rag citation agent mcp_server eval` -> 117 tests passed.

Additional verification recorded on 2026-07-31:

- `wsl bash -lc "cd /mnt/d/aiproducts/PaperLens/backend && . .venv/bin/activate && python manage.py check"` -> passed.
- `wsl bash -lc "cd /mnt/d/aiproducts/PaperLens/backend && . .venv/bin/activate && python manage.py test api realtime papers datasources rag citation agent mcp_server eval"` -> 147 tests passed.
