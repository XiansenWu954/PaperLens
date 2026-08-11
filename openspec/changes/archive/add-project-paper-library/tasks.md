# add-project-paper-library Tasks

- [x] Add `ProjectPaper` model and migration.
- [x] Make DBLP part of default search sources.
- [x] Add endpoints for listing, adding, search-adding, marking, and removing project papers.
- [x] Add project paper ID helper for RAG filtering.
- [x] Add tests for DBLP default source and project paper lifecycle.
- [x] Verify with `python manage.py test api datasources`.

Verification recorded on 2026-07-31:

- `python manage.py test papers api eval` -> 30 tests passed.
- `python manage.py test api realtime papers datasources rag citation agent mcp_server eval` -> 117 tests passed.
