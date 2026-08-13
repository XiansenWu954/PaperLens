"""IngestionService — scoped job get-or-create + global build claim/reuse
(Tasks 4.1) and the short activation transaction (Task 4.3).

Key derivation:
- request-key = ``{project_id}:{paper_id}:{source_identity}`` — scoped to ONE
  project/paper; concurrent identical requests converge on ONE job.
- build-key  = ``{paper_id}:{source_identity}`` — GLOBAL across projects;
  two project jobs for the same paper+PDF share ONE non-null index version.

The build version is claimed (get-or-create on the deterministic identity)
at enqueue time so redelivery and cross-project reuse always attach to the
same version; the version only becomes ``active`` inside the short activation
transaction that locks the paper, verifies persisted chunk counts, supersedes
the previous active row and activates exactly one new version without ever
deleting rollback data.
"""
from __future__ import annotations

import hashlib
import logging
import time

from django.db import IntegrityError, transaction
from django.utils import timezone

from api.models import PaperIngestionJob
from rag.models import PaperIndexVersion, Text

logger = logging.getLogger(__name__)


def _digest(value: str, length: int = 64) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:length]


class IngestionService:
    """Deterministic ingestion coordination (request/build identity, job
    convergence, version claim, activation)."""

    PARSER_IDENTITY = "ingestion-service-v1"
    CHUNK_CONFIG_HASH = "ingestion-service-v1"

    # -- identity derivation (Tasks 4.1) ------------------------------------

    def request_key(self, project_id: int, paper_id: int, source_identity: str) -> str:
        return f"{project_id}:{paper_id}:{source_identity}"

    def build_key(self, paper_id: int, source_identity: str) -> str:
        return f"{paper_id}:{source_identity}"

    # -- scoped job get-or-create (Tasks 4.1) --------------------------------

    def get_or_create_job(
        self,
        project,
        paper,
        *,
        idempotency_key: str,
        file_name: str = "",
        file_hash: str = "",
        file_path: str = "",
        source_kind: str = "",
        source_url: str = "",
        file_size: int = 0,
    ) -> tuple[PaperIngestionJob, bool]:
        """Scoped get-or-create on ``(project, paper, idempotency_key)``.

        Concurrent identical requests race on the partial unique constraint;
        the loser retries the GET (one job, never two builds).
        """
        defaults = {
            "file_name": file_name,
            "file_hash": file_hash,
            "file_path": file_path,
            "source_kind": source_kind,
            "source_url": source_url,
            "file_size": file_size,
        }
        for _ in range(3):
            try:
                return PaperIngestionJob.objects.get_or_create(
                    project=project,
                    paper=paper,
                    idempotency_key=idempotency_key,
                    defaults=defaults,
                )
            except IntegrityError:
                continue
        job = PaperIngestionJob.objects.get(
            project=project, paper=paper, idempotency_key=idempotency_key)
        return job, False

    # -- global build claim / reuse (Tasks 4.1) ------------------------------

    def claim_build(self, job: PaperIngestionJob, source_identity: str) -> PaperIndexVersion:
        """Get-or-create the GLOBAL build version for (paper, source).

        The version identity is (paper, source_sha256, pipeline_signature) —
        deterministic and model-free — so every project job attached to this
        build (and every redelivery) references the SAME non-null version.
        """
        from rag.embedding import embedding_metadata

        meta = embedding_metadata()
        # pipeline_signature is CharField(64) — keep the build identity short
        # and deterministic (hash of the build key).
        pipeline = f"ingest-build:{_digest(self.build_key(job.paper_id, source_identity), 24)}"
        src_sha = _digest(source_identity)
        for _ in range(3):
            try:
                version, _created = PaperIndexVersion.objects.get_or_create(
                    paper=job.paper,
                    source_sha256=src_sha,
                    pipeline_signature=pipeline,
                    defaults={
                        "status": "building",
                        "parser_identity": self.PARSER_IDENTITY,
                        "chunk_config_hash": self.CHUNK_CONFIG_HASH,
                        "embedding_model": str(meta["embedding_model"]),
                        "embedding_version": str(meta["embedding_version"]),
                        "embedding_dim": int(meta["embedding_dim"]),
                    },
                )
                break
            except IntegrityError:
                continue
        else:  # pragma: no cover - 3 races is effectively impossible
            version = PaperIndexVersion.objects.get(
                paper=job.paper, source_sha256=src_sha,
                pipeline_signature=pipeline)
        if job.index_version_id != version.id:
            job.index_version = version
            job.save(update_fields=["index_version", "updated_at"])
        return version

    # -- short activation transaction (Task 4.3) -----------------------------

    def activate(
        self, paper_id: int, version_id: int, expected_chunks: int
    ) -> PaperIndexVersion:
        """Lock paper + version, verify persisted chunks, supersede the old
        active row and activate exactly ONE new version.

        Runs inside a short transaction with row locks; ONLY ``building ->
        active`` is allowed (ING-D-CX-01) — an already-active version is never
        re-activated here; reuse of an active build goes through the explicit
        no-op path in the caller. Any verification failure raises a STABLE
        message (never raw values) and the previous active version stays
        untouched (rollback boundary — old index keeps serving).
        """
        with transaction.atomic():
            from papers.models import Paper

            paper_row = Paper.objects.select_for_update().get(id=paper_id)
            version = PaperIndexVersion.objects.select_for_update().get(
                id=version_id)
            if version.status != "building":
                raise RuntimeError("version_not_building")
            persisted = Text.objects.filter(index_version=version).count()
            if persisted != expected_chunks:
                raise RuntimeError("chunk_count_mismatch")
            PaperIndexVersion.objects.filter(
                paper=paper_row, status="active"
            ).update(status="superseded")
            version.status = "active"
            version.chunk_count = expected_chunks
            version.activated_at = timezone.now()
            version.failed_at = None
            version.error_code = ""
            version.save(update_fields=[
                "status", "chunk_count", "activated_at", "failed_at",
                "error_code", "updated_at"])
        logger.info(
            "index version activated",
            extra={
                "event": "index_version_activated",
                "paper_id": paper_id,
                "version_id": version_id,
                "chunk_count": expected_chunks,
                "status": "done",
            },
        )
        return version
