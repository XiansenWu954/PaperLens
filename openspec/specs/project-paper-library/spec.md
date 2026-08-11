# Spec: project-paper-library

## Purpose

Define project membership, DBLP-first CS discovery, import and export, ingestion
state, and non-destructive paper-library management.

## Requirements

### Requirement: DBLP default source
Paper search MUST include DBLP by default for CS metadata.

#### Scenario: Default sources
- **WHEN** `datasources.registry.search(query)` is called without explicit sources
- **THEN** DBLP, OpenAlex, and ArXiv are included.

### Requirement: Project paper library
Projects MUST maintain their own paper membership without duplicating global papers.

#### Scenario: Remove project paper
- **WHEN** DELETE `/api/projects/<id>/papers/<paper_id>`
- **THEN** the paper is removed from the project only.
