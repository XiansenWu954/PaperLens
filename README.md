# PaperLens

PaperLens is a research workspace for computer science papers. It helps organize
papers into projects, index uploaded PDFs, search project evidence, inspect
citation relationships, and draft report versions from traceable sources.

The application is built with Django/DRF, Server-Sent Events, Vue 3, PostgreSQL,
pgvector, Redis, Celery, and a lightweight tool orchestration layer.

## Features

- Project workspaces for papers, runs, chat sessions, reports, and run events.
- Paper discovery through DBLP, OpenAlex, arXiv, and Semantic Scholar.
- Project paper library with add, remove, status, and ingestion metadata.
- PDF upload and ingestion with page, section, and character-offset metadata.
- Hybrid retrieval using pgvector dense search, PostgreSQL full-text search, and
  Reciprocal Rank Fusion.
- Project chat powered by a **ReAct agent loop**: the LLM autonomously decides
  which tools to call, in what order, and how many times (read evidence, search,
  add papers, compare, read sections, build citation map, draft reports, export
  BibTeX) — while a deterministic safety layer blocks destructive operations and
  verifies every citation.
- Citation map for project-level paper relationships, embedded inline in chat.
- Report studio with report versions and source-marker checks.
- Background jobs for ingestion and longer research workflows.
- Structured backend logs with request IDs, task IDs, run events, durations, and
  error fields.
- MCP-compatible tool surface for selected project read/query operations.

## Architecture

```mermaid
flowchart LR
  User["User"] --> UI["Vue workbench"]
  UI --> API["Django REST/SSE API"]
  API --> Harness["Run harness"]
  Harness --> Tools["Project tools"]
  Tools --> Search["Paper sources"]
  Tools --> RAG["Hybrid retrieval"]
  Tools --> Graph["Citation graph"]
  Tools --> Reports["Report versions"]
  API --> Jobs["Celery + Redis"]
  Jobs --> Ingest["PDF parse, chunk, embed"]
  Ingest --> DB["PostgreSQL + pgvector + FTS"]
  RAG --> DB
  Harness --> Events["Run events + logs"]
  Events --> UI
```

## Repository Layout

```text
backend/     Django apps, retrieval, ingestion, evaluation, MCP server
frontend/    Vue 3 workbench
openspec/    Architecture and change specifications
docker-compose.yml
.env.example
```

## Requirements

- Python 3.11+
- Node.js 22+
- Docker Desktop, for the Postgres/Redis demo stack
- A DeepSeek-compatible API key for live model calls

A DeepSeek API key is **required** for Agent Chat answers, report section
drafting, and RAG reranking. Without a key the service still starts and can
serve pure retrieval/paper-library operations, but any LLM-dependent call
returns a clear "未配置 DEEPSEEK_API_KEY" message instead of crashing.

Local tests run without a model key by using the fake embedding provider and
deterministic fixtures.

## Quick Start

Create local configuration:

```powershell
Copy-Item .env.example .env
```

Start the Docker stack:

```powershell
docker compose up --build -d
```

Seed a demo project:

```powershell
docker compose exec backend python manage.py seed_demo_project
```

Open the app:

- Frontend: `http://127.0.0.1:5173`
- Backend API: `http://127.0.0.1:8000/api/projects`

## Local Development

Backend:

```powershell
cd backend
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python manage.py migrate
python manage.py seed_demo_project
python manage.py runserver 0.0.0.0:8000
```

Frontend:

```powershell
cd frontend
npm install
npm run dev -- --host 0.0.0.0
```

## Configuration

Important settings are defined in `.env.example`. Copy it to `.env` and fill in
your own values:

```text
DJANGO_SECRET_KEY=change-me-for-non-local-use
DATABASE_URL=postgres://paperlens:paperlens@postgres:5432/paperlens
CELERY_BROKER_URL=redis://redis:6379/0
CELERY_RESULT_BACKEND=redis://redis:6379/1
DEEPSEEK_API_KEY=
DEEPSEEK_MODEL=deepseek-v4-flash
PAPERLENS_PROJECT_CHAT_LIVE_LLM=1
PAPERLENS_EMBEDDING_PROVIDER=qwen3-local
PAPERLENS_EMBEDDING_MODEL=Qwen/Qwen3-Embedding-0.6B
PAPERLENS_EMBEDDING_DIM=1024
PAPERLENS_RAG_DENSE_K=20
PAPERLENS_RAG_LEXICAL_K=20
PAPERLENS_RAG_FINAL_K=8
VITE_API_BASE_URL=http://localhost:8000
```

Key configuration notes:

- `DEEPSEEK_API_KEY` — your DeepSeek API key. Required for LLM features (Agent
  Chat, report drafting, RAG reranking). Without it the service starts but LLM
  calls return a friendly configuration hint.
- `DJANGO_SECRET_KEY` — set a strong random value for any non-local deployment.
- `PAPERLENS_EMBEDDING_PROVIDER=fake` — use the deterministic hash embedding for
  offline tests and no-network demos (no model download needed).

You can check configuration readiness at any time:

```text
GET http://127.0.0.1:8000/
# -> {"status":"ok","service":"PaperLens","version":"0.3.0","config":{"deepseek_key_configured":true,"embedding_provider":"bge-m3","database":"postgres"}}
```

Do not commit `.env`, API keys, raw paper dumps, local logs, or generated live
evaluation reports. If an API key is ever leaked, rotate it immediately in the
provider dashboard.

## API Overview

Compatibility endpoints:

- `POST /api/research`
- `GET /api/research/<id>`
- `GET /api/research/<id>/stream`

Project endpoints:

- `GET|POST /api/projects`
- `GET|PATCH|DELETE /api/projects/<id>`
- `GET|POST /api/projects/<id>/papers`
- `POST /api/projects/<id>/papers/import` (BibTeX/RIS import)
- `GET /api/projects/<id>/papers/export.bib` / `.ris` (export)
- `POST /api/projects/<id>/papers/search-add`
- `POST /api/projects/<id>/papers/<paper_id>/pdf-upload`
- `POST /api/projects/<id>/papers/<paper_id>/ingest`
- `GET /api/projects/<id>/ingestion-jobs`
- `GET|POST /api/projects/<id>/chat`
- `GET /api/projects/<id>/chat/<session_id>/stream`
- `GET|POST /api/projects/<id>/reports`
- `GET /api/projects/<id>/citation-graph`
- `GET /api/projects/<id>/papers/<paper_a>/path/<paper_b>` (connection path)
- `GET|POST /api/projects/<id>/paper-relations` (citation context labels)
- `POST /api/projects/<id>/workflows/research-expand`

## Quality Checks

Backend deterministic checks (fast, mocked):

```powershell
cd backend
python manage.py check
python manage.py test api realtime papers datasources rag citation agent mcp_server eval --noinput
python manage.py evaluate_intents
python manage.py evaluate_agent_quality --write-report
python manage.py evaluate_rag_quality --write-report
python manage.py evaluate_pdf_rag --write-report
```

Real-model upgrade quality checks (BGE-M3 + DeepSeek, minutes):

```powershell
python manage.py evaluate_upgrade_quality --write-report
```

Interactive end-to-end evaluation (requires running backend + DeepSeek key):

```powershell
python manage.py evaluate_interactive --write-report
```

Frontend:

```powershell
cd frontend
npm run test
npm run build
```

One-command quick gate (writes a run-record evidence bundle with per-step
output, a manifest, and a summary):

```powershell
cd backend
python manage.py run_quality_gate --frontend
```

Recent local results:

| Check | Result |
|---|---:|
| Django system check | passed |
| Docker PostgreSQL backend suite | 224 tests passed |
| Stage B scope/evidence security gate | 169/169 cases passed |
| Frontend Vitest suite | 54 tests passed |
| Frontend production build | passed |
| OpenSpec strict validation | 14/14 current specs passed |

These numbers are the reproducible Stage B baseline. They validate project
scope, evidence identity, citation resolution, capability policy, event
redaction, and interface compatibility. They do not claim current live-model
answer quality. Real BGE-M3 retrieval and DeepSeek Agent quality are evaluated
separately before a release metric is published.

## Notes

- SQLite remains available as a local fallback for tests and small development
  runs. The Docker stack uses PostgreSQL with pgvector.
- The default embedding model is BAAI/bge-m3. CPU inference works, but cold
  start can be slow; keep the model cache warm for demos.
- If Docker Hub image pulls fail on a local network, pull the equivalent Python
  and Node base images from another trusted registry and tag them locally before
  rebuilding.
- Destructive project actions, such as deleting papers or clearing a project,
  are not exposed through autonomous tool calls.

## License

MIT
