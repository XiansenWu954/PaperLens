"""PaperLens 论文本地库存模型。

把抓取到的论文持久化到本地 SQLite，作为"本地库存"——同一论文绝不重复抓取，
既是防限流，也满足本地库约束。引用图谱所需 referenced_works 直接落 JSONField。
"""
from __future__ import annotations

from django.db import IntegrityError
from django.db import models


class Venue(models.Model):
    """会议/期刊（CS 元数据，来自 OpenAlex/DBLP）。"""

    name = models.CharField(max_length=256, db_index=True)
    venue_type = models.CharField(max_length=64, blank=True)  # journal/conference/etc
    raw = models.JSONField(default=dict, blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["name"], name="uniq_venue_name")]

    def __str__(self) -> str:
        return self.name


class Author(models.Model):
    """作者（CS 元数据）。"""

    name = models.CharField(max_length=256, db_index=True)
    openalex_id = models.CharField(max_length=64, null=True, blank=True, db_index=True)
    affiliation = models.CharField(max_length=256, blank=True)
    raw = models.JSONField(default=dict, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["openalex_id"], name="uniq_author_openalex")
        ]

    def __str__(self) -> str:
        return self.name


class Paper(models.Model):
    """论文（本地库存核心）。

    统一标识：优先 doi，其次 arxiv_id，最后 openalex_id。
    referenced_works 存该论文引用的其他 openalex_id 列表——引用图谱的 bibliographic
    coupling 数据源（地基验证：OpenAlex 该字段返回完整列表，样本 95 条）。
    """

    # 标识符（多源归一）
    openalex_id = models.CharField(max_length=64, null=True, blank=True, db_index=True)
    s2_id = models.CharField(max_length=64, null=True, blank=True, db_index=True)
    doi = models.CharField(max_length=128, null=True, blank=True, db_index=True)
    arxiv_id = models.CharField(max_length=32, null=True, blank=True, db_index=True)

    # 内容
    title = models.TextField()
    abstract = models.TextField(blank=True)
    year = models.IntegerField(null=True, blank=True)

    # 关系
    authors = models.ManyToManyField(Author, blank=True, related_name="papers")
    venue = models.ForeignKey(Venue, null=True, blank=True, on_delete=models.SET_NULL)

    # 引用图谱数据（★护城河）
    citation_count = models.IntegerField(default=0)
    referenced_works = models.JSONField(default=list, blank=True)
    pdf_url = models.URLField(max_length=512, null=True, blank=True)

    # 来源原数据（兜底）
    raw = models.JSONField(default=dict, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["doi"], name="uniq_paper_doi"),
            models.UniqueConstraint(fields=["arxiv_id"], name="uniq_paper_arxiv"),
            models.UniqueConstraint(fields=["openalex_id"], name="uniq_paper_openalex"),
        ]

    def __str__(self) -> str:
        return f"{self.title[:60]} ({self.year})"


def _norm(value: str | None) -> str | None:
    return value.strip().lower() if value and value.strip() else None


def upsert_paper(data: dict) -> Paper:
    """按 doi / arxiv_id / openalex_id upsert 一篇论文。

    data 为归一化后的字典（datasources 统一结构）。命中已存在则更新字段，
    不存在则新建。返回 Paper 实例。
    """
    doi = _norm(data.get("doi"))
    arxiv_id = _norm(data.get("arxiv_id"))
    openalex_id = data.get("openalex_id")

    paper: Paper | None = None
    if doi:
        paper = Paper.objects.filter(doi=doi).first()
    if not paper and arxiv_id:
        paper = Paper.objects.filter(arxiv_id=arxiv_id).first()
    if not paper and openalex_id:
        paper = Paper.objects.filter(openalex_id=openalex_id).first()

    fields = {
        "title": data.get("title", "")[:10000],
        "abstract": data.get("abstract", "") or "",
        "year": data.get("year"),
        "citation_count": data.get("citation_count", 0) or 0,
        "referenced_works": data.get("referenced_works") or [],
        "pdf_url": data.get("pdf_url"),
        "raw": data.get("raw", {}),
    }
    if doi:
        fields["doi"] = doi
    if arxiv_id:
        fields["arxiv_id"] = arxiv_id
    if openalex_id:
        fields["openalex_id"] = openalex_id

    if not paper:
        try:
            paper = Paper.objects.create(**fields)
        except IntegrityError:
            paper = None
            if doi:
                paper = Paper.objects.filter(doi=doi).first()
            if not paper and arxiv_id:
                paper = Paper.objects.filter(arxiv_id=arxiv_id).first()
            if not paper and openalex_id:
                paper = Paper.objects.filter(openalex_id=openalex_id).first()
            if not paper:
                raise

    for k, v in fields.items():
        setattr(paper, k, v)
    paper.save()
    return paper
