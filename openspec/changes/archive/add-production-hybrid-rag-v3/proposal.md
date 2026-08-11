# Proposal: add-production-hybrid-rag-v3

## Summary
Upgrade PaperLens from a lightweight self-managed RAG demo into a resume-grade,
production-aligned Agent literature workbench. The change introduces
PostgreSQL + pgvector, Postgres full-text search, RRF hybrid retrieval, Qwen3
embedding configuration, Celery/Redis background ingestion, project PDF upload,
a bounded LangGraph workflow, and 30+ deterministic RAG quality metrics.

## Motivation
The V2 workbench demonstrates Agent concepts, but the original SQLite + JSON
embedding + NumPy retrieval path is not representative of mainstream RAG
systems. V3 keeps the existing user-facing API shape where possible while
making the core retrieval, ingestion, and evaluation chain credible for a
portfolio/resume project.

## Non-Goals
- No multi-user permission model.
- No Chroma/OpenSearch/Elasticsearch in this slice.
- No migration of old local SQLite demo data.
- No autonomous destructive Agent tools.
