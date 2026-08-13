"""Application models for research tasks and project workspaces."""
from __future__ import annotations

from django.db import models


class ResearchProject(models.Model):
    """A durable literature research workspace."""

    STATUS_CHOICES = [
        ("active", "Active"),
        ("archived", "Archived"),
    ]

    title = models.CharField(max_length=240)
    description = models.TextField(blank=True)
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default="active")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self) -> str:
        return f"ResearchProject({self.id}, {self.title[:40]})"


class ProjectPaper(models.Model):
    """Project membership for a globally deduplicated paper."""

    STATUS_CHOICES = [
        ("candidate", "Candidate"),
        ("included", "Included"),
        ("core", "Core"),
        ("excluded", "Excluded"),
    ]

    ADDED_BY_CHOICES = [
        ("user", "User"),
        ("agent", "Agent"),
        ("demo", "Demo"),
    ]

    project = models.ForeignKey(
        ResearchProject, related_name="project_papers", on_delete=models.CASCADE
    )
    paper = models.ForeignKey(
        "papers.Paper", related_name="project_links", on_delete=models.CASCADE
    )
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default="candidate")
    source_reason = models.TextField(blank=True)
    added_by = models.CharField(max_length=16, choices=ADDED_BY_CHOICES, default="user")
    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["project", "paper"], name="uniq_project_paper"
            )
        ]

    def __str__(self) -> str:
        return f"ProjectPaper(project={self.project_id}, paper={self.paper_id})"


class ProjectRun(models.Model):
    """Inspectable project-scoped run used by Agent harnesses."""

    STATUS_CHOICES = ResearchTask_STATUS_CHOICES = [
        ("pending", "Pending"),
        ("running", "Running"),
        ("done", "Done"),
        ("error", "Error"),
    ]

    KIND_CHOICES = [
        ("research", "Research"),
        ("chat", "Chat"),
        ("report", "Report"),
        ("ingestion", "Ingestion"),
        ("workflow", "Workflow"),
        ("demo", "Demo"),
    ]

    project = models.ForeignKey(
        ResearchProject, related_name="runs", on_delete=models.CASCADE
    )
    kind = models.CharField(max_length=16, choices=KIND_CHOICES, default="research")
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default="pending")
    question = models.TextField(blank=True)
    output = models.TextField(blank=True)
    error_message = models.TextField(blank=True)
    sources = models.JSONField(default=list, blank=True)
    citation_graph = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self) -> str:
        return f"ProjectRun({self.id}, project={self.project_id}, {self.kind})"


class ProjectRunEvent(models.Model):
    """Append-only run event for progress, tools, and errors."""

    run = models.ForeignKey(ProjectRun, related_name="events", on_delete=models.CASCADE)
    event_type = models.CharField(max_length=64, db_index=True)
    payload = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at", "id"]


class ChatSession(models.Model):
    """Persistent project chat session."""

    project = models.ForeignKey(
        ResearchProject, related_name="chat_sessions", on_delete=models.CASCADE
    )
    title = models.CharField(max_length=240, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self) -> str:
        return f"ChatSession({self.id}, project={self.project_id})"


class ChatMessage(models.Model):
    """User/assistant/tool message in a project chat."""

    ROLE_CHOICES = [
        ("user", "User"),
        ("assistant", "Assistant"),
        ("tool", "Tool"),
        ("system", "System"),
    ]

    session = models.ForeignKey(ChatSession, related_name="messages", on_delete=models.CASCADE)
    role = models.CharField(max_length=16, choices=ROLE_CHOICES)
    content = models.TextField()
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at", "id"]


class ReportVersion(models.Model):
    """Versioned report artifact for a project."""

    project = models.ForeignKey(
        ResearchProject, related_name="reports", on_delete=models.CASCADE
    )
    title = models.CharField(max_length=240, default="Research report")
    content = models.TextField()
    source = models.CharField(max_length=32, default="agent")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at", "-id"]


class PaperRelation(models.Model):
    """两篇论文之间的引用语境关系（对齐 Scite 的 smart citations）。

    label 标注 A 引用 B 时的语境：supporting（支持）/ contradicting（反对）/ mentioning（提及）。
    由 LLM 基于 referenced_works + 摘要做轻量分类，结果缓存。
    """

    LABEL_CHOICES = [
        ("supporting", "Supporting"),
        ("contradicting", "Contradicting"),
        ("mentioning", "Mentioning"),
        ("unanalyzed", "Unanalyzed"),
    ]

    project = models.ForeignKey(
        ResearchProject, related_name="paper_relations", on_delete=models.CASCADE
    )
    citing_paper = models.ForeignKey(
        "papers.Paper", related_name="outgoing_relations", on_delete=models.CASCADE
    )
    cited_paper = models.ForeignKey(
        "papers.Paper", related_name="incoming_relations", on_delete=models.CASCADE
    )
    label = models.CharField(max_length=16, choices=LABEL_CHOICES, default="unanalyzed")
    context = models.TextField(blank=True)
    confidence = models.FloatField(default=0.0)
    analyzed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["project", "citing_paper", "cited_paper"],
                name="uniq_paper_relation",
            )
        ]

    def __str__(self) -> str:
        return f"PaperRelation({self.citing_paper_id}->{self.cited_paper_id}, {self.label})"


class PaperIngestionJob(models.Model):
    """Background PDF ingestion job for one project paper.

    Tasks 2.3: extended lifecycle states, idempotency/source identity,
    attempt/file metrics, safe error fields, optional global index-version
    reference. Existing fields and rows stay compatible.
    """

    STATUS_CHOICES = [
        ("pending", "Pending"),
        ("downloading", "Downloading"),
        ("parsing", "Parsing"),
        ("embedding", "Embedding"),
        ("committing", "Committing"),
        ("embedded", "Embedded"),
        ("failed", "Failed"),
    ]

    project = models.ForeignKey(
        ResearchProject, related_name="ingestion_jobs", on_delete=models.CASCADE
    )
    paper = models.ForeignKey(
        "papers.Paper", related_name="ingestion_jobs", on_delete=models.CASCADE
    )
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default="pending")
    # optional global build reference (Tasks 2.3); PROTECT so a referenced
    # version cannot be deleted out from under the job (audit chain intact)
    index_version = models.ForeignKey(
        "rag.PaperIndexVersion", related_name="ingestion_jobs",
        null=True, blank=True, on_delete=models.PROTECT,
    )
    # idempotency / source identity
    idempotency_key = models.CharField(max_length=128, blank=True)
    source_kind = models.CharField(max_length=16, blank=True,
                                   default="")
    # attempt / file metrics (attempts start at 0, incremented on execution)
    attempt_count = models.IntegerField(default=0)
    file_size = models.BigIntegerField(default=0)
    # safe error classification (never raw exception text)
    error_code = models.CharField(max_length=64, blank=True, default="")
    retryable = models.BooleanField(default=False)
    file_name = models.CharField(max_length=255, blank=True)
    file_hash = models.CharField(max_length=64, blank=True, db_index=True)
    file_path = models.CharField(max_length=512, blank=True)
    source_url = models.URLField(max_length=512, blank=True)
    chunk_count = models.IntegerField(default=0)
    error_message = models.TextField(blank=True)
    celery_task_id = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        constraints = [
            models.UniqueConstraint(
                fields=["project", "paper", "idempotency_key"],
                name="uniq_ingestion_project_paper_key",
                condition=~models.Q(idempotency_key=""),
            ),
        ]
        indexes = [
            models.Index(fields=["project", "paper", "status"], name="api_paperin_project_b19d8d_idx"),
            models.Index(fields=["file_hash"], name="api_paperin_file_ha_75a3f1_idx"),
        ]

    def __str__(self) -> str:
        return f"PaperIngestionJob({self.id}, paper={self.paper_id}, {self.status})"


class ResearchTask(models.Model):
    """一次研究任务（输入问题 → 综述 + 引用图谱 + 来源）。"""

    STATUS_CHOICES = [
        ("pending", "Pending"),
        ("running", "Running"),
        ("done", "Done"),
        ("error", "Error"),
    ]

    question = models.TextField()
    project = models.ForeignKey(
        ResearchProject,
        null=True,
        blank=True,
        related_name="legacy_tasks",
        on_delete=models.SET_NULL,
    )
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default="pending")
    final_report = models.TextField(blank=True)
    citation_graph = models.JSONField(default=dict, blank=True)  # vis_data
    sources = models.JSONField(default=list, blank=True)
    notes = models.JSONField(default=list, blank=True)
    error_message = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self) -> str:
        return f"ResearchTask({self.id}, {self.status}, {self.question[:40]})"
