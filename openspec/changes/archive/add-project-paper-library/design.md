# add-project-paper-library Design

## SOTA/Concept Fit
- Tool-layer source aggregation follows gpt-researcher retriever style: multiple sources, normalized paper payloads, dedupe.
- Project scope is a product boundary around PaperQA2-style `Text` chunks.

## DBLP Role
- `DEFAULT_SOURCES` includes DBLP, OpenAlex, and ArXiv.
- DBLP contributes CS venue/author/key metadata.
- OpenAlex/S2 enrich citations and references; ArXiv contributes PDF.

## Data Safety
- Removing a paper from a project deletes only `ProjectPaper`.
- Global `Paper`, `Text`, and cache rows stay intact for reuse.
