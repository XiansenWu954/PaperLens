# Spec delta: production-hybrid-rag

## REMOVED Requirements

### Requirement: Qwen3 Embedding Provider

**Reason:** The archived V3 change recorded Qwen3 as the original default, but the current
runtime and `.env.example` use BGE-M3. Keeping the old requirement in current specs creates
an implementation/documentation conflict.

**Migration:** Replace model-specific default wording with the configured provider and index
versioning requirement below. Archived change history remains unchanged.

## ADDED Requirements

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
