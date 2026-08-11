"""Demo project seed data for the resume showcase path."""
from __future__ import annotations

from asgiref.sync import async_to_sync

from agent.project_tools import add_papers_to_project

from .models import ReportVersion, ResearchProject
from .serializers import ResearchProjectSerializer


DEMO_PROJECT_TITLE = "Mamba and efficient sequence models"

DEMO_PAPERS = [
    {
        "source": "demo",
        "source_id": "mamba-demo",
        "title": "Mamba: Linear-Time Sequence Modeling with Selective State Spaces",
        "abstract": "Mamba introduces selective state space models for efficient long sequence modeling.",
        "year": 2023,
        "authors": ["Albert Gu", "Tri Dao"],
        "venue": "arXiv",
        "citation_count": 1200,
        "arxiv_id": "2312.00752",
        "referenced_works": ["W1", "W2", "W3"],
    },
    {
        "source": "demo",
        "source_id": "transformer-demo",
        "title": "Attention Is All You Need",
        "abstract": "The Transformer architecture replaces recurrence with self-attention.",
        "year": 2017,
        "authors": ["Ashish Vaswani"],
        "venue": "NeurIPS",
        "citation_count": 100000,
        "arxiv_id": "1706.03762",
        "referenced_works": ["W2", "W3", "W4"],
    },
]


def seed_demo_project(
    title: str | None = None,
    *,
    reuse: bool = True,
    status: str = "active",
    reset: bool = False,
) -> dict:
    """Create or refresh a self-contained demo workspace without network calls.

    ``reset=True`` (with ``reuse=True``) clears accumulated runs/sessions/reports
    before re-seeding, so each eval case starts from a known snapshot instead of
    inheriting state from a prior run. Default ``False`` preserves all existing
    callers. See ``api.fixtures.reset_project_state`` for what is cleared.
    """

    target_title = title or DEMO_PROJECT_TITLE
    project = None
    if reuse:
        project = (
            ResearchProject.objects.filter(title=target_title)
            .order_by("-updated_at", "-id")
            .first()
        )
    if project is None:
        project = ResearchProject.objects.create(
            title=target_title,
            description="Demo project for PaperLens Agent resume showcase.",
            status=status,
        )
    else:
        if reset:
            from .fixtures import reset_project_state
            reset_project_state(project.id)
        project.description = "Demo project for PaperLens Agent resume showcase."
        project.status = status
        project.save(update_fields=["description", "status", "updated_at"])

    if reuse and title is None:
        ResearchProject.objects.filter(title=DEMO_PROJECT_TITLE).exclude(id=project.id).update(status="archived")

    add_result = async_to_sync(add_papers_to_project)(
        project.id,
        DEMO_PAPERS,
        "Demo seed paper",
    )
    ReportVersion.objects.get_or_create(
        project=project,
        title="Demo research brief",
        defaults={
            "content": (
                "# Demo research brief\n\n"
                "This seeded project demonstrates project papers, Agent Chat, "
                "RAG scope, citation graph, and report versions."
            ),
            "source": "demo",
        },
    )
    return {
        "project": ResearchProjectSerializer(project).data,
        **add_result,
    }
