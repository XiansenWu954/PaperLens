"""ProjectScopeResolver (Task 2.3): the ONLY entry point for project-scoped
paper/chunk/graph/report queries.

Semantics (design §3):
- paper_ids(None)      -> full current project evidence scope (non-excluded memberships)
- paper_ids([])        -> EXPLICIT empty scope -> [] (never falls back to the library)
- provided ids         -> intersected with the evidence scope (foreign / excluded /
                          unlinked / nonexistent ids simply drop out, no existence signal)
- library_memberships  -> inventory scope: ALL memberships of the current project
                          (excluded included, with status), never foreign/unlinked
- project_paper(pid)   -> scoped lookup; returns None for foreign/excluded/unlinked/
                          nonexistent (caller uses the same not-found result shape)
- chunks(paper_ids)    -> Text rows limited to the scoped papers; active_only keeps
                          rows with an indexed embedding version (version-aware
                          matching semantics land with Task 3.3)
"""
from __future__ import annotations

from typing import Any

from django.db.models import QuerySet

from api.models import ProjectPaper
from papers.models import Paper
from rag.models import Text


def _active_embedding_meta() -> dict:
    from rag.embedding import embedding_metadata

    return embedding_metadata()


class ProjectScopeResolver:
    def __init__(self, project_id: int) -> None:
        self.project_id = int(project_id)

    # ------------------------------------------------------------------
    # scope building
    # ------------------------------------------------------------------

    def _membership_qs(self, include_excluded: bool) -> QuerySet:
        qs = ProjectPaper.objects.select_related("paper", "paper__venue").filter(
            project_id=self.project_id
        )
        if not include_excluded:
            qs = qs.exclude(status="excluded")
        return qs

    def _evidence_paper_ids(self, paper_ids: list[int] | None) -> list[int]:
        """Resolve a requested paper subset against the evidence scope.

        None -> the full current project evidence scope.
        []   -> explicit empty scope (fail closed).
        ids  -> intersection with the evidence scope.
        """
        if paper_ids is None:
            return self.paper_ids()
        if paper_ids == []:
            return []
        allowed = set(self.paper_ids())
        return [pid for pid in paper_ids if pid in allowed]

    # ------------------------------------------------------------------
    # paper-level queries
    # ------------------------------------------------------------------

    def paper_ids(self, include_excluded: bool = False) -> list[int]:
        return list(
            self._membership_qs(include_excluded).values_list("paper_id", flat=True)
        )

    def papers(self, include_excluded: bool = False) -> list[Paper]:
        return [row.paper for row in self._membership_qs(include_excluded)]

    def library_memberships(self) -> list[ProjectPaper]:
        """Inventory scope: all current-project memberships (excluded included,
        with explicit status). Foreign/unlinked never appear."""
        return list(
            self._membership_qs(include_excluded=True).order_by(
                "-paper__citation_count", "paper__title"
            )
        )

    def project_paper(
        self, paper_id: int, include_excluded: bool = False
    ) -> Paper | None:
        """Scoped paper lookup. None for foreign / excluded / unlinked /
        nonexistent ids — indistinguishable not-found semantics."""
        row = self._membership_qs(include_excluded).filter(paper_id=paper_id).first()
        return row.paper if row else None

    def graph_papers(self) -> list[Paper]:
        return self.papers(include_excluded=False)

    # ------------------------------------------------------------------
    # chunk-level queries
    # ------------------------------------------------------------------

    def chunks(
        self, paper_ids: list[int] | None = None, active_only: bool = True
    ) -> QuerySet[Text]:
        """Chunks restricted to the resolved evidence scope.

        paper_ids=None -> full evidence scope; [] -> empty queryset (fail closed);
        otherwise the subset is intersected with the evidence scope.
        active_only keeps rows whose embedding MODEL + VERSION match the CURRENT
        active index (embedding_metadata); stale chunks never reach evidence
        producers (§20.2 — the filter lives here, not only at resolution time).
        """
        ids = self._evidence_paper_ids(paper_ids)
        if not ids:
            return Text.objects.none()
        qs = Text.objects.filter(paper_id__in=ids)
        if active_only:
            meta = _active_embedding_meta()
            qs = qs.filter(
                embedding_model=meta["embedding_model"],
                embedding_version=meta["embedding_version"],
            )
        return qs

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------

    def to_audit(self) -> dict[str, Any]:
        return {"project_id": self.project_id}
