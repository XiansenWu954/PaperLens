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

    # -- identity derivation (Tasks 4.1 / P2-C-R3-02) -----------------------

    def canonical_url_identity(self, source_url: str) -> str:
        """THE canonical URL source identity (P2-C-R3-02): the sha256 digest
        of the raw URL. Every entry point (URL API, Agent auto-queue,
        durable workflow, worker) MUST derive request keys, safe file names
        and global build claims from this single contract — never from a
        re-hashed digest (that would mint duplicate build versions)."""
        return _digest(source_url)

    def url_request_key(self, project_id: int, paper_id: int,
                        source_url: str) -> str:
        return self.request_key(project_id, paper_id,
                                self.canonical_url_identity(source_url))

    def url_file_name(self, paper_id: int, source_url: str) -> str:
        # SAFE digest name — never derived from the raw URL path/query.
        identity = self.canonical_url_identity(source_url)
        return f"paper-{paper_id}-{identity[:8]}.pdf"

    def enqueue_url_job(
        self, *, project, paper, source_url: str, source_kind: str,
    ) -> tuple[PaperIngestionJob, bool, PaperIndexVersion]:
        """Single canonical entry for URL-sourced ingestion (P2-C-R3-02).

        Used by the URL API, Agent auto-queue and the durable workflow so
        all three converge on ONE scoped project job and ONE global build:
          - request key   = {project}:{paper}:{sha256(url)[:64]}
          - safe filename = paper-{paper}-{sha8}.pdf
          - build claim   = claim_build(job, RAW url) — claim_build digests
            internally, exactly like the worker's
            ``job.file_hash or job.source_url`` fallback for URL jobs and
            the original Phase 1 URL API behaviour (compatibility: existing
            Phase 1 builds are reused, never duplicated).
        """
        job, created = self.get_or_create_job(
            project, paper,
            idempotency_key=self.url_request_key(
                project.id, paper.id, source_url),
            source_kind=source_kind,
            source_url=source_url,
            file_name=self.url_file_name(paper.id, source_url),
        )
        version = self.claim_build(job, source_url)
        return job, created, version

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

    def claim_build(self, job: PaperIngestionJob, source_identity: str,
                    *, expected_execution: tuple[int, str] | None = None,
                    ) -> PaperIndexVersion:
        """Get-or-create the GLOBAL build version for (paper, source).

        The version identity is (paper, source_sha256, pipeline_signature) —
        deterministic and model-free — so every project job attached to this
        build (and every redelivery) references the SAME non-null version.

        P2-GLM-01-CX-02 fencing: when ``expected_execution`` (job_id,
        token) is supplied, the lease fence, the version get-or-create AND
        the job→version attachment all happen inside ONE transaction that
        holds the job row lock from ``assert_execution_owner`` (token +
        non-terminal + unexpired lease). No check-then-write gap remains:
        an expired/stale worker raises before any version row is created
        or attached.
        """
        from rag.embedding import embedding_metadata

        meta = embedding_metadata()
        # pipeline_signature is CharField(64) — keep the build identity short
        # and deterministic (hash of the build key).
        pipeline = f"ingest-build:{_digest(self.build_key(job.paper_id, source_identity), 24)}"
        src_sha = _digest(source_identity)

        def _get_or_create() -> PaperIndexVersion:
            # get_or_create uses its own inner savepoint, so the retry
            # loop is safe inside the caller's fence transaction
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
                    return version
                except IntegrityError:
                    continue
            return PaperIndexVersion.objects.get(  # pragma: no cover
                paper=job.paper, source_sha256=src_sha,
                pipeline_signature=pipeline)

        def _attach(version: PaperIndexVersion) -> None:
            if job.index_version_id != version.id:
                job.index_version = version
                job.save(update_fields=["index_version", "updated_at"])

        if expected_execution is not None:
            from api.ingestion_execution import assert_execution_owner
            with transaction.atomic():
                assert_execution_owner(*expected_execution)
                version = _get_or_create()
                _attach(version)
            return version

        version = _get_or_create()
        _attach(version)
        return version

    # -- short activation transaction (Task 4.3) -----------------------------

    def activate(
        self, paper_id: int, version_id: int, expected_chunks: int,
        *, expected_execution: tuple[int, str] | None = None,
    ) -> PaperIndexVersion:
        """Lock paper + version, verify persisted chunks, supersede the old
        active row and activate exactly ONE new version.

        Runs inside a short transaction with row locks; ONLY ``building ->
        active`` is allowed (ING-D-CX-01) — an already-active version is never
        re-activated here; reuse of an active build goes through the explicit
        no-op path in the caller. Any verification failure raises a STABLE
        message (never raw values) and the previous active version stays
        untouched (rollback boundary — old index keeps serving).

        P2-GLM-01 fencing: with ``expected_execution`` (job_id, token) the
        same short transaction first locks and verifies the execution lease
        row — a stale worker raises ExecutionLeaseLost BEFORE any index
        mutation, with no race window between check and commit.
        """
        with transaction.atomic():
            from papers.models import Paper

            if expected_execution is not None:
                from api.ingestion_execution import assert_execution_owner
                assert_execution_owner(*expected_execution)

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
