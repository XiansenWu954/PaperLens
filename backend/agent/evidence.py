"""Typed evidence contracts (Task 3.1).

Full-text evidence is carried in an ``EvidenceEnvelope`` with a stable,
database-verifiable identity (chunk_id + content_hash + embedding_version);
the positional ``chunk_index`` is DISPLAY-ONLY and never an authenticity
basis. Metadata candidates use ``MetadataEvidence`` and are never disguised
as full-text envelopes.

The ``evidence_id`` is a DETERMINISTIC digest of the normalized
project/paper/chunk/content_hash/embedding_version representation (§20.1):
the same chunk version always yields the same id, and any content-hash or
embedding-version change yields a different id. Every producer (query/read/
compare) shares this single factory.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

ENVELOPE_REQUIRED_FIELDS = (
    "evidence_id", "project_id", "paper_id", "chunk_id", "content_hash",
    "embedding_version",
)


def make_evidence_id(
    project_id: int,
    paper_id: int,
    chunk_id: int,
    content_hash: str,
    embedding_version: str,
) -> str:
    """Deterministic evidence id for a specific chunk VERSION (§20.1/§21.1).

    Derived from the normalized representation of project/paper/chunk ids,
    content hash and embedding version via the FULL SHA-256 digest. Same input
    -> same id; a content-hash or embedding-version change -> different id.
    """
    canonical = "|".join([
        str(int(project_id)),
        str(int(paper_id)),
        str(int(chunk_id)),
        str(content_hash or ""),
        str(embedding_version or ""),
    ])
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return f"ev-{digest}"


def canonical_evidence_id(item: dict[str, Any]) -> str | None:
    """Recompute the canonical evidence id from an envelope's OWN fields.

    Returns None when the identity fields are missing or not positive
    integers (caller decides legacy vs malformed). The declared
    ``evidence_id`` is NEVER trusted — it must equal this recomputed value.
    """
    try:
        project_id = _positive_int(item.get("project_id"))
        paper_id = _positive_int(item.get("paper_id"))
        chunk_id = _positive_int(item.get("chunk_id"))
    except (TypeError, ValueError):
        return None
    if project_id is None or paper_id is None or chunk_id is None:
        return None
    content_hash = item.get("content_hash")
    embedding_version = item.get("embedding_version")
    if not isinstance(content_hash, str) or not isinstance(embedding_version, str):
        return None
    return make_evidence_id(
        project_id, paper_id, chunk_id, content_hash, embedding_version)


def _positive_int(value: Any) -> int | None:
    """Positive integer identity (bool is NOT an int for identity purposes)."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    if isinstance(value, str) and value.strip().isdigit():
        parsed = int(value)
        return parsed if parsed > 0 else None
    return None


def evidence_identity_key(item: dict[str, Any]) -> str:
    """Stable per-item resolution identity (§20.3).

    Resolution is keyed by the typed evidence_id when present; legacy items
    (no envelope) get a deterministic surrogate key so the same marker with
    multiple candidates is never first-wins.
    """
    eid = item.get("evidence_id")
    if eid:
        return f"ev:{eid}"
    marker = str(item.get("source_marker") or item.get("citation") or "").strip() or "none"
    payload = json.dumps(
        {k: v for k, v in item.items() if k != "__legacy_unresolved"},
        sort_keys=True,
        default=str,
    )
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12]
    return f"legacy:{marker}:{digest}"


@dataclass(frozen=True)
class ParsedEvidence:
    """Result of validating/parsing one evidence-shaped dict (§20.4).

    kind: "fulltext" (valid envelope) | "metadata" (valid metadata evidence)
          | "legacy" (migration-period structure, downgraded) | "malformed"
    """

    kind: str
    item: dict[str, Any]


def parse_evidence_item(item: Any, trusted_project_id: int | None = None) -> ParsedEvidence:
    """Validate an evidence-shaped dict through the shared parser/factory.

    Type rules (§21.2):
    - project/paper/chunk ids must be POSITIVE integers (bool is not an int);
    - content_hash / embedding_version / evidence_id must be non-empty strings;
    - the declared evidence_id must equal the RECOMPUTED canonical id
      (§21.1) — otherwise malformed;
    - metadata must carry a valid project identity; when a trusted project is
      provided it must match (foreign metadata is downgraded to legacy).

    Migration-period structures with MISSING fields may be downgraded to
    ``legacy`` (marked ``__legacy_unresolved``); structures with WRONG TYPES or
    a wrong canonical id are ``malformed`` and never counted as evidence.
    """
    if not isinstance(item, dict):
        return ParsedEvidence("malformed", {})
    etype = item.get("evidence_type")

    def _legacy(original: dict[str, Any]) -> ParsedEvidence:
        legacy = dict(original)
        legacy.setdefault("evidence_type", "fulltext" if etype == "fulltext" else "unknown")
        legacy["__legacy_unresolved"] = True
        return ParsedEvidence("legacy", legacy)

    def _has_wrong_types(required: tuple[str, ...]) -> bool:
        """Any required field PRESENT but with the wrong type/value class."""
        for f in required:
            value = item.get(f)
            if value in (None, ""):
                continue  # missing -> legacy territory
            if f in ("project_id", "paper_id", "chunk_id"):
                if _positive_int(value) is None:
                    return True
            else:  # string fields
                if not isinstance(value, str):
                    return True
        return False

    if etype == "fulltext":
        required = ("evidence_id", "project_id", "paper_id", "chunk_id",
                    "content_hash", "embedding_version")
        if _has_wrong_types(required):
            return ParsedEvidence("malformed", dict(item))
        missing = [f for f in required if item.get(f) in (None, "")]
        if missing:
            if item.get("paper_id") is not None or item.get("source_marker") or item.get("citation"):
                return _legacy(item)
            return ParsedEvidence("malformed", dict(item))
        # all fields present and typed: verify the canonical evidence id
        canonical = canonical_evidence_id(item)
        if canonical is None or canonical != str(item.get("evidence_id")):
            return ParsedEvidence("malformed", dict(item))
        return ParsedEvidence("fulltext", dict(item))

    if etype == "metadata":
        if _has_wrong_types(("project_id", "paper_id")):
            return ParsedEvidence("malformed", dict(item))
        project_id = _positive_int(item.get("project_id"))
        paper_id = _positive_int(item.get("paper_id"))
        has_marker = bool(item.get("source_marker") or item.get("citation"))
        if project_id is None:
            # missing project identity: migration-era metadata → legacy
            if paper_id is not None or has_marker:
                return _legacy(item)
            return ParsedEvidence("malformed", dict(item))
        if trusted_project_id is not None and project_id != int(trusted_project_id):
            # foreign project metadata: never counts as metadata retrieval
            return _legacy(item)
        if paper_id is None:
            if has_marker:
                return _legacy(item)
            return ParsedEvidence("malformed", dict(item))
        if not (isinstance(item.get("title"), str) and item.get("title")) and not has_marker:
            return ParsedEvidence("malformed", dict(item))
        return ParsedEvidence("metadata", dict(item))

    # no evidence_type at all: migration-period legacy structure
    if item.get("paper_id") is not None or item.get("source_marker") or item.get("citation"):
        return _legacy(item)
    return ParsedEvidence("malformed", dict(item))


@dataclass(frozen=True)
class EvidenceEnvelope:
    """Full-text evidence produced by RAG / section read / comparison.

    Fields:
    - evidence_id: stable id derived from the chunk identity (same chunk
      version -> same id).
    - project_id / paper_id / chunk_id: database-stable identities.
    - content_hash: hash of the chunk content (authenticity check).
    - excerpt: model/UI context only; truth comes from chunk_id+content_hash.
    - page/section: display metadata.
    - retrieval_sources / retrieval_scores: where this evidence came from.
    - embedding_version: index version the chunk belongs to.
    - chunk_index: DISPLAY ONLY — never used as an authenticity basis.
    """

    evidence_id: str
    project_id: int
    paper_id: int
    chunk_id: int
    content_hash: str
    excerpt: str
    page_start: int | None = None
    page_end: int | None = None
    section: str = ""
    retrieval_sources: tuple[str, ...] = ()
    retrieval_scores: dict[str, float] = field(default_factory=dict)
    embedding_version: str = ""
    # display-only / compatibility fields
    chunk_index: int | None = None
    title: str = ""
    summary: str = ""
    citation: str = ""
    source_marker: str = ""
    score: float = 0.0
    docname: str = ""
    fallback: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "evidence_id": self.evidence_id,
            "project_id": self.project_id,
            "paper_id": self.paper_id,
            "chunk_id": self.chunk_id,
            "content_hash": self.content_hash,
            "excerpt": self.excerpt,
            "page_start": self.page_start,
            "page_end": self.page_end,
            "section": self.section,
            "retrieval_sources": list(self.retrieval_sources),
            "retrieval_scores": self.retrieval_scores,
            "embedding_version": self.embedding_version,
            "evidence_type": "fulltext",
            # display-only / compatibility
            "chunk_index": self.chunk_index,
            "title": self.title,
            "summary": self.summary,
            "citation": self.citation,
            "source_marker": self.source_marker,
            "score": self.score,
            "docname": self.docname,
            "fallback": self.fallback,
        }


@dataclass(frozen=True)
class MetadataEvidence:
    """Metadata-only candidate (abstract/title/venue). Never full-text."""

    project_id: int
    paper_id: int
    title: str
    summary: str
    citation: str
    source_marker: str
    score: float = 0.0
    evidence_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "evidence_id": self.evidence_id or f"md-{self.project_id}-{self.paper_id}",
            "project_id": self.project_id,
            "paper_id": self.paper_id,
            "title": self.title,
            "summary": self.summary,
            "citation": self.citation,
            "source_marker": self.source_marker,
            "score": self.score,
            "evidence_type": "metadata",
        }
