"""RAG data model for project-scoped paper passages and evidence."""
from __future__ import annotations

from django.db import models

from .fields import HybridVectorField


class PaperIndexVersion(models.Model):
    """Immutable complete paper index version (Tasks 2.1-2.2).

    One row per (paper, source_sha256, pipeline_signature); at most one
    ``active`` version per paper (partial unique constraint). Chunks of a
    building version are invisible to retrieval; a version becomes active
    only after a successful short activation transaction.
    """

    STATUS_CHOICES = [
        ("building", "Building"),
        ("active", "Active"),
        ("superseded", "Superseded"),
        ("failed", "Failed"),
    ]

    paper = models.ForeignKey(
        "papers.Paper", related_name="index_versions", on_delete=models.CASCADE
    )
    status = models.CharField(max_length=16, choices=STATUS_CHOICES,
                              default="building")
    # immutable build identity
    source_sha256 = models.CharField(max_length=64)
    pipeline_signature = models.CharField(max_length=64)
    # parser / chunk configuration identity
    parser_identity = models.CharField(max_length=160, blank=True, default="")
    chunk_config_hash = models.CharField(max_length=64, blank=True, default="")
    # embedding identity
    embedding_model = models.CharField(max_length=160, blank=True)
    embedding_version = models.CharField(max_length=240, blank=True)
    embedding_dim = models.IntegerField(default=1024)
    # observed cardinality
    chunk_count = models.IntegerField(default=0)
    # safe failure fields (never raw exception text)
    error_code = models.CharField(max_length=64, blank=True, default="")
    error_hash = models.CharField(max_length=32, blank=True, default="")
    # lifecycle timestamps
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    activated_at = models.DateTimeField(null=True, blank=True)
    failed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        constraints = [
            models.UniqueConstraint(
                fields=["paper", "source_sha256", "pipeline_signature"],
                name="uniq_paper_index_version_identity",
            ),
            # at most ONE active version per paper (partial unique)
            models.UniqueConstraint(
                fields=["paper"],
                condition=models.Q(status="active"),
                name="uniq_paper_index_version_one_active",
            ),
        ]
        indexes = [
            models.Index(fields=["paper", "status"],
                         name="rag_piv_paper_st_idx"),
            models.Index(fields=["embedding_model", "embedding_version"],
                         name="rag_piv_embeddi_idx"),
        ]

    def __str__(self) -> str:
        return (f"PaperIndexVersion({self.id}, paper={self.paper_id}, "
                f"{self.status})")


class Text(models.Model):
    """A paper passage indexed for dense and lexical retrieval.

    Chunks belong to an immutable ``index_version``; uniqueness is
    (index_version, chunk_index). ``paper`` and embedding fields are kept as
    compatibility fields (the active-compatible version remains the retrieval
    boundary).
    """

    paper = models.ForeignKey(
        "papers.Paper", related_name="chunks", on_delete=models.CASCADE
    )
    index_version = models.ForeignKey(
        PaperIndexVersion, related_name="chunks", on_delete=models.CASCADE
    )
    docname = models.CharField(max_length=256)
    chunk_index = models.IntegerField(default=0)
    content = models.TextField()
    embedding = HybridVectorField(default=list, dimensions=1024)
    embedding_model = models.CharField(max_length=160, blank=True)
    embedding_dim = models.IntegerField(default=1024)
    embedding_version = models.CharField(max_length=240, blank=True)
    content_hash = models.CharField(max_length=64, blank=True, db_index=True)
    page_start = models.IntegerField(null=True, blank=True)
    page_end = models.IntegerField(null=True, blank=True)
    section = models.CharField(max_length=240, blank=True)
    char_start = models.IntegerField(null=True, blank=True)
    char_end = models.IntegerField(null=True, blank=True)
    search_vector = models.TextField(blank=True)
    # BGE-M3 sparse 词级权重 {token_id_str: weight}，用于 sparse lexical 检索（替代/增强 PG FTS）
    sparse_weights = models.JSONField(default=dict, blank=True)
    citation_key = models.CharField(max_length=32, db_index=True)
    indexed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["index_version", "chunk_index"],
                name="uniq_text_index_version_chunk",
            )
        ]
        indexes = [
            models.Index(fields=["paper", "chunk_index"], name="rag_text_paper_i_c9e3bb_idx"),
            models.Index(fields=["index_version"], name="rag_text_index_v_d7c215_idx"),
            models.Index(fields=["content_hash"], name="rag_text_content_5b09bb_idx"),
            models.Index(fields=["embedding_model", "embedding_version"], name="rag_text_embeddi_f9c262_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.docname} (chunk={self.chunk_index})"


class Evidence(models.Model):
    """LLM relevance summary for one indexed chunk."""

    text = models.ForeignKey(Text, related_name="evidence", on_delete=models.CASCADE)
    question = models.TextField()
    summary = models.TextField()
    score = models.IntegerField(default=0)
    citation_key = models.CharField(max_length=32, db_index=True)

    def __str__(self) -> str:
        return f"Evidence(score={self.score}) for {self.text.docname}"
