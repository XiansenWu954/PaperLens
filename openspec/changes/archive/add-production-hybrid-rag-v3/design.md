# Design: production hybrid RAG V3

## Agent Concepts

### Function Calling
Function Calling remains the boundary between the model and auditable actions.
Tools can search papers, add candidates, query project RAG, refresh graph data,
and draft report sections. Destructive actions such as deleting papers or
overwriting reports remain outside autonomous tool access.

### Prompt Engineering
Prompt contracts require project evidence first, exact source markers, no fake
pqac keys, weak-evidence disclosure, and compact academic answers. Qwen3
embedding query/document instructions are separate so retrieval intent is
explicit.

### Harness
The deterministic project chat harness remains the stable entrypoint and
evaluation baseline. Celery-backed background tasks add durable execution for
PDF ingestion and long research workflows. ProjectRunEvent records tool,
retrieval, ingestion, workflow, and failure events.

### MCP
The MCP surface should expose stable project tools only: search papers, query
project RAG, list project papers, and citation graph retrieval. Internal DB
mutation and destructive actions should not be MCP-exported.

### LangGraph
LangGraph is used only for the explicit long chain where it adds value:
plan expansion -> search sources -> add candidates -> enqueue ingestion ->
query hybrid RAG -> critic -> draft report -> persist report. Normal chat
routing stays deterministic.

## Retrieval Design
- PostgreSQL stores vectors through pgvector in the `rag.Text.embedding` column.
- SQLite remains a local-test fallback through the same model API.
- Dense retrieval uses vector cosine distance.
- Lexical retrieval uses Postgres full-text search over chunk search text.
- Reciprocal Rank Fusion combines dense and lexical candidates.
- DeepSeek RCS only scores fused candidates, reducing cost and noise.

## Ingestion Design
- PDF upload and pdf_url ingestion create `PaperIngestionJob`.
- Celery tasks parse PDFs, chunk text, compute embeddings, persist Text rows,
  update job status, and append ProjectRunEvent records.
- Chunks carry page ranges, section labels, char offsets, content hash,
  embedding model, embedding dim, embedding version, and indexed_at.

## Evaluation Design
- Default eval uses deterministic fixtures and fake lexical embeddings.
- Metrics: Recall@5, MRR, Context Precision, Citation Coverage, Faithfulness,
  Unsupported Claim Rate, and average retrieval latency.
- Live LLM evaluation remains optional and separate from deterministic CI gates.
