# add-project-paper-library Proposal

Add a project-scoped paper library with DBLP as a default CS source. Users can add, remove from project, and mark papers while the global paper store remains deduplicated.

## Why
- The product needs a durable project knowledge base for RAG chat and report generation.
- DBLP is a resume-relevant CS metadata source and must be a default source, not a hidden fallback.

## Scope
- Project-paper links, status tags, search-add API, project-scoped RAG paper IDs.
- No destructive deletion of global `Paper` or `Text` records when removing from a project.
