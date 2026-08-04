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
- Project chat that can query indexed evidence, search for additional papers,
  update the project library, refresh citation data, and draft report sections.
- Citation map for project-level paper relationships.
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

Local tests can run without a model key by using the fake embedding provider and
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

Important settings are defined in `.env.example`:

```text
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

Do not commit `.env`, API keys, raw paper dumps, local logs, or generated live
evaluation reports.

## API Overview

Compatibility endpoints:

- `POST /api/research`
- `GET /api/research/<id>`
- `GET /api/research/<id>/stream`

Project endpoints:

- `GET|POST /api/projects`
- `GET|PATCH|DELETE /api/projects/<id>`
- `GET|POST /api/projects/<id>/papers`
- `POST /api/projects/<id>/papers/search-add`
- `POST /api/projects/<id>/papers/<paper_id>/pdf-upload`
- `POST /api/projects/<id>/papers/<paper_id>/ingest`
- `GET /api/projects/<id>/ingestion-jobs`
- `GET|POST /api/projects/<id>/chat`
- `GET /api/projects/<id>/chat/<session_id>/stream`
- `GET|POST /api/projects/<id>/reports`
- `GET /api/projects/<id>/citation-graph`
- `POST /api/projects/<id>/workflows/research-expand`

## Quality Checks

Backend:

```powershell
cd backend
python manage.py check
python manage.py test api realtime papers datasources rag citation agent mcp_server eval --noinput
python manage.py evaluate_intents
python manage.py evaluate_agent_quality --write-report
python manage.py evaluate_rag_quality --write-report
python manage.py evaluate_pdf_rag --write-report
```

Frontend:

```powershell
cd frontend
npm run build
```

Recent local results:

| Check | Result |
|---|---:|
| Django system check | passed |
| Backend test suite | 169 tests passed |
| Intent evaluation | 33/33 passed |
| Agent quality evaluation | score 1.0 |
| RAG quality evaluation | 32/32 passed |
| PDF ingestion/RAG evaluation | 3/3 passed |
| Frontend production build | passed |
| Docker backend check | passed |

Latest deterministic RAG metrics:

| Metric | Value |
|---|---:|
| Recall@5 | 1.0 |
| MRR | 0.977 |
| Context Precision | 0.6207 |
| Citation Coverage | 1.0 |
| Faithfulness | 1.0 |
| Unsupported Claim Rate | 0.0 |

## Notes

- SQLite remains available as a local fallback for tests and small development
  runs. The Docker stack uses PostgreSQL with pgvector.
- The default embedding model is Qwen3-Embedding-0.6B. CPU inference works, but
  cold start can be slow; keep the model cache warm for demos.
- If Docker Hub image pulls fail on a local network, pull the equivalent Python
  and Node base images from another trusted registry and tag them locally before
  rebuilding.
- Destructive project actions, such as deleting papers or clearing a project,
  are not exposed through autonomous tool calls.

## License

MIT
