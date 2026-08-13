# Spec: production-hybrid-rag

## ADDED Requirements

### Requirement: PostgreSQL pgvector RAG Index
PaperLens MUST support PostgreSQL + pgvector as the production/demo RAG index.

#### Scenario: Store chunk vectors
- **GIVEN** a parsed paper chunk
- **WHEN** ingestion persists the chunk on PostgreSQL
- **THEN** `rag.Text.embedding` MUST be stored as `vector(1024)`
- **AND** the row MUST include embedding model, dimension, version, content hash,
  page range, section, char offsets, search text, and indexed timestamp.

### Requirement: Hybrid Retrieval
Project RAG MUST retrieve with dense vectors and lexical search, then fuse
candidate rankings with Reciprocal Rank Fusion.

#### Scenario: Project-scoped hybrid query
- **GIVEN** a project with indexed papers
- **WHEN** `query_project_rag(project_id, question, k)` is called
- **THEN** dense candidates, lexical candidates, fused candidates, selected
  evidence count, model/version, and retrieval duration MUST be logged.
- **AND** the public API contract MUST remain compatible with V2.

### Requirement: Qwen3 Embedding Provider
The default production-aligned embedding provider MUST be Qwen3 local embedding.

#### Scenario: Embed document chunks
- **WHEN** document chunks are embedded
- **THEN** the provider MUST use the document instruction prefix.
- **AND** query embedding MUST use the query instruction prefix.
- **AND** tests MUST be able to swap in a deterministic fake provider.

### Requirement: PDF Ingestion Jobs
PDF upload and pdf_url ingestion MUST run through a tracked background job.

#### Scenario: Upload PDF
- **GIVEN** a project paper
- **WHEN** a user posts multipart field `file` to `/pdf-upload`
- **THEN** the system MUST save the PDF under media storage, create a
  `PaperIngestionJob`, enqueue a Celery task, and return job status.

### Requirement: LangGraph Research Expansion Workflow
Long research expansion MUST be an explicit workflow, not the default chat path.

#### Scenario: Start research expansion
- **WHEN** a user posts a question to `/workflows/research-expand`
- **THEN** a ProjectRun MUST be created and nodes MUST append workflow events:
  plan, search, add, enqueue ingestion, hybrid RAG, critic, draft report, persist.

### Requirement: RAG Quality Metrics
Hybrid RAG MUST have a deterministic 30+ case evaluation command.

#### Scenario: Run quality eval
- **WHEN** `python manage.py evaluate_rag_quality --write-report` is run
- **THEN** the report MUST include at least 30 cases and metrics for Recall@5,
  MRR, Context Precision, Citation Coverage, Faithfulness, Unsupported Claim
  Rate, and average retrieval latency.
