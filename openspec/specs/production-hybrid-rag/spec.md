# Spec: production-hybrid-rag

## Purpose

PaperLens supports a V3 production-aligned project RAG path:

- PostgreSQL + pgvector vector storage for demo/deployment.
- SQLite-compatible fallback for local unit tests.
- Configured embedding provider (BGE-M3 default) with index version tracking.
- Postgres FTS + dense vector search + RRF fusion.
- Celery-backed PDF ingestion jobs with page/section/offset metadata.
- Explicit LangGraph workflow for long research expansion.
- Deterministic 30+ RAG quality evaluation metrics.

The originating change has been completed and archived under
`openspec/changes/archive/add-production-hybrid-rag-v3`.

## Requirements

### Requirement: Configured Embedding Provider And Index Version

The current default embedding provider MUST be defined by one canonical environment/settings
contract and every indexed chunk MUST record enough metadata to prevent incompatible vectors
from being mixed.

#### Scenario: Current default provider

- **WHEN** the backend starts without a test override
- **THEN** the effective provider and model MUST match `.env.example`, Django settings, health
  output, and current architecture documentation.

#### Scenario: Deterministic test provider

- **WHEN** the normal offline test suite runs
- **THEN** it MUST use a deterministic fake embedding provider
- **AND** real BGE-M3 loading MUST require an explicit real-model test flag.

#### Scenario: Embedding provider change

- **WHEN** the default embedding model, dimension, or encoding behavior changes
- **THEN** a new embedding version MUST be created
- **AND** incompatible old and new vectors MUST NOT be queried as one index
- **AND** a measured reindex plan and rollback path MUST be approved through OpenSpec.
