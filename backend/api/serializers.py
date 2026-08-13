"""API serializers."""
from __future__ import annotations

from rest_framework import serializers

from .models import (
    ChatMessage,
    ChatSession,
    PaperIngestionJob,
    ProjectPaper,
    ProjectRun,
    ProjectRunEvent,
    ReportVersion,
    ResearchProject,
    ResearchTask,
)


class CreateResearchTaskSerializer(serializers.Serializer):
    question = serializers.CharField(max_length=2000)
    project_id = serializers.IntegerField(required=False)


class ResearchTaskSerializer(serializers.ModelSerializer):
    class Meta:
        model = ResearchTask
        fields = [
            "id",
            "project",
            "question",
            "status",
            "final_report",
            "citation_graph",
            "sources",
            "error_message",
            "created_at",
            "updated_at",
        ]
        read_only_fields = [
            "id",
            "status",
            "final_report",
            "citation_graph",
            "sources",
            "error_message",
            "created_at",
            "updated_at",
        ]


class ResearchProjectSerializer(serializers.ModelSerializer):
    paper_count = serializers.IntegerField(read_only=True)
    run_count = serializers.IntegerField(read_only=True)
    latest_report_id = serializers.IntegerField(read_only=True, allow_null=True)

    class Meta:
        model = ResearchProject
        fields = [
            "id",
            "title",
            "description",
            "status",
            "paper_count",
            "run_count",
            "latest_report_id",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "created_at", "updated_at", "paper_count", "run_count", "latest_report_id"]


class ProjectPaperSerializer(serializers.ModelSerializer):
    paper_id = serializers.IntegerField(source="paper.id", read_only=True)
    title = serializers.CharField(source="paper.title", read_only=True)
    abstract = serializers.CharField(source="paper.abstract", read_only=True)
    year = serializers.IntegerField(source="paper.year", read_only=True)
    citation_count = serializers.IntegerField(source="paper.citation_count", read_only=True)
    doi = serializers.CharField(source="paper.doi", read_only=True)
    arxiv_id = serializers.CharField(source="paper.arxiv_id", read_only=True)
    openalex_id = serializers.CharField(source="paper.openalex_id", read_only=True)
    pdf_url = serializers.CharField(source="paper.pdf_url", read_only=True)
    venue = serializers.SerializerMethodField()
    chunk_count = serializers.SerializerMethodField()
    ingestion_status = serializers.SerializerMethodField()
    latest_ingestion_job_id = serializers.SerializerMethodField()
    latest_ingestion_error = serializers.SerializerMethodField()
    embedding_model = serializers.SerializerMethodField()
    indexed_at = serializers.SerializerMethodField()
    fulltext_ready = serializers.SerializerMethodField()
    latest_job_retryable = serializers.SerializerMethodField()

    class Meta:
        model = ProjectPaper
        fields = [
            "id",
            "paper_id",
            "title",
            "abstract",
            "year",
            "venue",
            "citation_count",
            "doi",
            "arxiv_id",
            "openalex_id",
            "pdf_url",
            "status",
            "source_reason",
            "added_by",
            "notes",
            "ingestion_status",
            "latest_ingestion_job_id",
            "latest_ingestion_error",
            "embedding_model",
            "indexed_at",
            "chunk_count",
            "fulltext_ready",
            "latest_job_retryable",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "created_at", "updated_at", "added_by"]

    def get_venue(self, obj: ProjectPaper) -> str:
        return obj.paper.venue.name if obj.paper.venue_id else ""

    def get_chunk_count(self, obj: ProjectPaper) -> int:
        value = getattr(obj, "chunk_count", None)
        if value is not None:
            return int(value)
        return obj.paper.chunks.count()

    def get_ingestion_status(self, obj: ProjectPaper) -> str:
        job = self._latest_job(obj)
        if job:
            return job.status
        return "embedded" if self.get_chunk_count(obj) > 0 else "pending"

    def get_latest_ingestion_job_id(self, obj: ProjectPaper) -> int | None:
        job = self._latest_job(obj)
        return job.id if job else None

    def get_latest_ingestion_error(self, obj: ProjectPaper) -> str:
        job = self._latest_job(obj)
        if job and job.status == "failed" and job.error_message:
            return job.error_message
        return ""

    def get_embedding_model(self, obj: ProjectPaper) -> str:
        chunk = obj.paper.chunks.order_by("-indexed_at", "-id").first()
        return chunk.embedding_model if chunk else ""

    def get_indexed_at(self, obj: ProjectPaper) -> str | None:
        chunk = obj.paper.chunks.order_by("-indexed_at", "-id").first()
        return chunk.indexed_at.isoformat() if chunk and chunk.indexed_at else None

    def _latest_job(self, obj: ProjectPaper) -> PaperIngestionJob | None:
        return PaperIngestionJob.objects.filter(
            project=obj.project,
            paper=obj.paper,
        ).order_by("-created_at", "-id").first()

    def get_fulltext_ready(self, obj: ProjectPaper) -> bool:
        """Tasks 5.5: true ONLY when the paper has a live ACTIVE index version
        WITH chunks — never from a URL or membership alone."""
        from rag.models import PaperIndexVersion

        return PaperIndexVersion.objects.filter(
            paper=obj.paper, status="active",
            chunk_count__gt=0,
        ).exists()

    def get_latest_job_retryable(self, obj: ProjectPaper) -> bool:
        job = self._latest_job(obj)
        return bool(job and job.status == "failed" and job.retryable)


class ProjectRunEventSerializer(serializers.ModelSerializer):
    class Meta:
        model = ProjectRunEvent
        fields = ["id", "event_type", "payload", "created_at"]


class ProjectRunSerializer(serializers.ModelSerializer):
    events = ProjectRunEventSerializer(many=True, read_only=True)

    class Meta:
        model = ProjectRun
        fields = [
            "id",
            "project",
            "kind",
            "status",
            "question",
            "output",
            "error_message",
            "sources",
            "citation_graph",
            "events",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "created_at", "updated_at", "events"]


class ChatMessageSerializer(serializers.ModelSerializer):
    class Meta:
        model = ChatMessage
        fields = ["id", "role", "content", "metadata", "created_at"]
        read_only_fields = ["id", "created_at"]


class ChatSessionSerializer(serializers.ModelSerializer):
    messages = ChatMessageSerializer(many=True, read_only=True)

    class Meta:
        model = ChatSession
        fields = ["id", "project", "title", "messages", "created_at", "updated_at"]
        read_only_fields = ["id", "project", "messages", "created_at", "updated_at"]


class ReportVersionSerializer(serializers.ModelSerializer):
    class Meta:
        model = ReportVersion
        fields = ["id", "project", "title", "content", "source", "created_at"]
        read_only_fields = ["id", "project", "created_at"]


class PaperIngestionJobSerializer(serializers.ModelSerializer):
    paper_title = serializers.CharField(source="paper.title", read_only=True)
    fulltext_ready = serializers.SerializerMethodField()

    class Meta:
        model = PaperIngestionJob
        fields = [
            "id",
            "project",
            "paper",
            "paper_title",
            "status",
            "source_kind",
            "file_name",
            "file_hash",
            "chunk_count",
            "error_code",
            "error_message",
            "retryable",
            "celery_task_id",
            "fulltext_ready",
            "created_at",
            "updated_at",
        ]
        read_only_fields = fields

    def get_fulltext_ready(self, obj) -> bool:
        """Tasks 5.1 / Tasks5-CX-05: true ONLY when the paper has a live
        ACTIVE index version WITH chunks (chunk_count > 0). building /
        superseded / failed / active-with-zero-chunks are all false."""
        if obj.index_version_id:
            return (
                obj.index_version.status == "active"
                and obj.index_version.chunk_count > 0
            )
        from rag.models import PaperIndexVersion

        return PaperIndexVersion.objects.filter(
            paper_id=obj.paper_id, status="active",
            chunk_count__gt=0,
        ).exists()
