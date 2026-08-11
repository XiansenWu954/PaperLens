"""Database-driven CitationResolver (Task 3.3, §20.3).

Reference resolution is a current-database-state + project-scope question. A
citation resolves ONLY when ALL of the following hold (batch-verified in one
round-trip):

- the envelope project identity is present, well-formed and EXACTLY equal to
  the trusted context project;
- the paper is a current NON-excluded membership of that project;
- the chunk primary key exists and belongs to that paper;
- the content hash matches;
- the envelope-declared embedding version matches the DATABASE chunk version;
- that version/model matches the CURRENT active index version.

Resolution identity is the typed ``evidence_id`` (marker is only a display /
binding index), so multiple candidates sharing one marker are all retained —
never first-wins. Malformed per-item identities fail closed individually and
never abort the batch.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from api.models import ProjectPaper
from rag.models import Text

from .evidence import canonical_evidence_id, evidence_identity_key

# reason codes
RESOLVED = "resolved"
MISSING_PROJECT_IDENTITY = "missing_project_identity"
MALFORMED_IDENTITY = "malformed_identity"
EVIDENCE_ID_MISMATCH = "evidence_id_mismatch"
DUPLICATE_IDENTITY_CONFLICT = "duplicate_identity_conflict"
PROJECT_MISMATCH = "project_mismatch"
NOT_MEMBER = "not_member"
CHUNK_MISSING = "chunk_missing"
CHUNK_PAPER_MISMATCH = "chunk_paper_mismatch"
HASH_MISMATCH = "hash_mismatch"
ENVELOPE_VERSION_MISMATCH = "envelope_version_mismatch"
VERSION_MISMATCH = "version_mismatch"
LEGACY_UNRESOLVED = "legacy_unresolved"
METADATA = "metadata"


@dataclass(frozen=True)
class CitationResolution:
    marker: str
    reference_resolved: bool
    reason_code: str


def _marker(item: dict[str, Any]) -> str:
    for key in ("source_marker", "citation", "title", "docname"):
        marker = str(item.get(key) or "").strip()
        if marker:
            return marker
    return ""


def _active_embedding_meta() -> dict[str, Any]:
    from rag.embedding import embedding_metadata

    return embedding_metadata()


class CitationResolver:
    """Batch resolver: one chunk query + one membership query per call."""

    def __init__(self, project_id: int) -> None:
        self.project_id = int(project_id)

    def resolve(
        self,
        evidence_items: list[dict[str, Any]],
        active_meta: dict[str, Any] | None = None,
    ) -> dict[str, CitationResolution]:
        """Resolve every evidence item against the current database.

        Returns {evidence_identity_key: CitationResolution} — identity is the
        typed evidence_id, so same-marker candidates are all preserved. The
        DECLARED evidence_id is NEVER trusted: it is recomputed canonically
        and compared (§21.1). If the same declared identity maps to DIFFERENT
        canonical payloads, the whole identity group fails closed with
        `duplicate_identity_conflict` — independent of input order (no
        first-wins). Items without a usable marker are ignored.
        """
        active_meta = active_meta or _active_embedding_meta()
        # group every candidate by its resolution identity (declared
        # evidence_id); DIFFERENT canonical payloads sharing one identity are
        # detected BEFORE any resolution so the outcome never depends on input
        # order (§21.1).
        groups: dict[str, list[dict[str, Any]]] = {}
        for item in evidence_items:
            if not _marker(item):
                continue
            key = evidence_identity_key(item)
            groups.setdefault(key, []).append(item)

        chunk_ids: list[int] = []
        for group in groups.values():
            for item in group:
                cid = item.get("chunk_id")
                if cid is not None:
                    try:
                        chunk_ids.append(int(cid))
                    except (TypeError, ValueError):
                        pass  # malformed chunk id -> resolved individually below

        chunks: dict[int, Text] = {}
        if chunk_ids:
            chunks = {t.id: t for t in Text.objects.filter(id__in=chunk_ids)}

        paper_ids: set[int] = set()
        for group in groups.values():
            for item in group:
                pid = item.get("paper_id")
                if pid is not None:
                    try:
                        paper_ids.add(int(pid))
                    except (TypeError, ValueError):
                        pass
        memberships: set[int] = set()
        if paper_ids:
            memberships = set(
                ProjectPaper.objects.filter(
                    project_id=self.project_id, paper_id__in=paper_ids
                )
                .exclude(status="excluded")
                .values_list("paper_id", flat=True)
            )

        resolutions: dict[str, CitationResolution] = {}
        for key, group in groups.items():
            canonicals = {
                canonical_evidence_id(item) or f"none:{_marker(item)}"
                for item in group
            }
            if len(canonicals) > 1:
                resolutions[key] = CitationResolution(
                    marker=_marker(group[0]),
                    reference_resolved=False,
                    reason_code=DUPLICATE_IDENTITY_CONFLICT,
                )
                continue
            resolutions[key] = self._resolve_one(
                group[0], chunks, memberships, active_meta)
        return resolutions

    def _resolve_one(
        self,
        item: dict[str, Any],
        chunks: dict[int, Text],
        memberships: set[int],
        active_meta: dict[str, Any],
    ) -> CitationResolution:
        marker = _marker(item)
        if (item.get("evidence_type") == "metadata"
                and not item.get("__legacy_unresolved")):
            return CitationResolution(marker, False, METADATA)

        chunk_id = item.get("chunk_id")
        content_hash = item.get("content_hash")
        if chunk_id is None or not content_hash:
            # legacy / non-envelope item: never auto-upgrade to resolved
            return CitationResolution(marker, False, LEGACY_UNRESOLVED)

        # envelope project identity is REQUIRED and must equal the context.
        item_project = item.get("project_id")
        if item_project is None or item_project == "":
            return CitationResolution(marker, False, MISSING_PROJECT_IDENTITY)
        try:
            item_project_i = int(item_project)
        except (TypeError, ValueError):
            return CitationResolution(marker, False, MALFORMED_IDENTITY)
        if item_project_i != self.project_id:
            return CitationResolution(marker, False, PROJECT_MISMATCH)

        # malformed chunk/paper ids fail closed individually, never the batch.
        try:
            chunk_id_i = int(chunk_id)
            paper_id_i = int(item.get("paper_id"))
        except (TypeError, ValueError):
            return CitationResolution(marker, False, MALFORMED_IDENTITY)

        # §21.1/§22: evidence_id is REQUIRED non-empty — a full envelope
        # without any declared id must fail closed, not silently pass. The
        # DECLARED id must then equal the recomputed canonical id; the
        # resolver never trusts an envelope-supplied identity.
        declared_eid = item.get("evidence_id")
        if not isinstance(declared_eid, str) or not declared_eid:
            return CitationResolution(marker, False, EVIDENCE_ID_MISMATCH)
        canonical = canonical_evidence_id(item)
        if canonical is None or canonical != declared_eid:
            return CitationResolution(marker, False, EVIDENCE_ID_MISMATCH)

        chunk = chunks.get(chunk_id_i)
        if chunk is None:
            return CitationResolution(marker, False, CHUNK_MISSING)
        if chunk.paper_id != paper_id_i:
            return CitationResolution(marker, False, CHUNK_PAPER_MISMATCH)
        if paper_id_i not in memberships:
            return CitationResolution(marker, False, NOT_MEMBER)
        if chunk.content_hash != str(content_hash):
            return CitationResolution(marker, False, HASH_MISMATCH)

        # envelope-declared version must match the DATABASE chunk version…
        envelope_version = item.get("embedding_version")
        if envelope_version is None or str(envelope_version) != str(chunk.embedding_version):
            return CitationResolution(marker, False, ENVELOPE_VERSION_MISMATCH)
        # …and the chunk must be on the CURRENT active index version.
        if (
            chunk.embedding_version != active_meta["embedding_version"]
            or chunk.embedding_model != active_meta["embedding_model"]
        ):
            return CitationResolution(marker, False, VERSION_MISMATCH)

        return CitationResolution(marker, True, RESOLVED)
