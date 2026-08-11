"""RAG data model for project-scoped paper passages and evidence."""
from __future__ import annotations

from django.db import models

from .fields import HybridVectorField


class Text(models.Model):
    """A paper passage indexed for dense and lexical retrieval."""

    paper = models.ForeignKey(
        "papers.Paper", related_name="chunks", on_delete=models.CASCADE
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
                fields=["paper", "chunk_index"], name="uniq_text_paper_chunk"
            )
        ]
        indexes = [
            models.Index(fields=["paper", "chunk_index"], name="rag_text_paper_i_c9e3bb_idx"),
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
