"""Project fixture helpers for deterministic evaluation.

These utilities fix the state-pollution problem that made reused projects
accumulate runs/sessions/reports across eval calls (deepseek-live-evaluation
plan §3.2). They reset a project to a clean snapshot WITHOUT deleting the
project itself or the shared global Paper / rag.Text rows.

Three fixture project kinds are provided so eval cases start from a known
state instead of the mutable demo project:
    - evidence   : seeded with the standard demo papers (Mamba + Attention)
    - empty      : no papers at all (abstention / no-evidence tests)
    - isolation  : papers from a disjoint topic (GNN/vision) so cross-project
                   leakage can be detected
"""
from __future__ import annotations

from asgiref.sync import async_to_sync

from agent.project_tools import add_papers_to_project

from .models import (
    ChatMessage,
    ChatSession,
    PaperIngestionJob,
    PaperRelation,
    ProjectPaper,
    ProjectRun,
    ProjectRunEvent,
    ReportVersion,
    ResearchProject,
)

# Papers for the isolation fixture: a disjoint topic (graph neural networks /
# computer vision) so a sequence-models query retrieving them is real leakage.
ISOLATION_PAPERS = [
    {
        "source": "fixture",
        "source_id": "gcn-isolation",
        "title": "Semi-Supervised Classification with Graph Convolutional Networks",
        "abstract": "GCN scales semi-supervised classification on graph-structured data via localized spectral filters.",
        "year": 2017,
        "authors": ["Thomas Kipf", "Max Welling"],
        "venue": "ICLR",
        "citation_count": 30000,
        "arxiv_id": "1609.02907",
    },
    {
        "source": "fixture",
        "source_id": "graphsage-isolation",
        "title": "Inductive Representation Learning on Large Graphs",
        "abstract": "GraphSAGE learns node embeddings by sampling and aggregating neighbors for inductive tasks.",
        "year": 2017,
        "authors": ["William Hamilton", "Rex Ying", "Jure Leskovec"],
        "venue": "NeurIPS",
        "citation_count": 12000,
        "arxiv_id": "1706.02216",
    },
]


def reset_project_state(project_id: int) -> dict:
    """Clear all child state of a project, keeping the project + global papers.

    Deletes (in CASCADE-safe order): run events, runs, chat messages, chat
    sessions, ingestion jobs, paper relations, reports, project-paper links.
    The ResearchProject row and the shared papers.Paper / rag.Text chunks
    survive (they are deduped across projects). Returns a summary of what was
    cleared.

    This is the building block for snapshot-based fixtures: call it, then
    re-seed papers, to guarantee each eval case starts from a known state.
    """
    summary = {
        "project_id": project_id,
        "runs": ProjectRun.objects.filter(project_id=project_id).delete()[0],
        # ChatMessage cascade from ChatSession, but delete explicitly for clarity.
        "chat_messages": ChatMessage.objects.filter(session__project_id=project_id).delete()[0],
        "chat_sessions": ChatSession.objects.filter(project_id=project_id).delete()[0],
        "ingestion_jobs": PaperIngestionJob.objects.filter(project_id=project_id).delete()[0],
        "paper_relations": PaperRelation.objects.filter(project_id=project_id).delete()[0],
        "reports": ReportVersion.objects.filter(project_id=project_id).delete()[0],
        "project_papers": ProjectPaper.objects.filter(project_id=project_id).delete()[0],
    }
    # ProjectRunEvent cascades from ProjectRun; the ProjectRun delete above already
    # removed them, but a defensive direct clear handles any orphans.
    ProjectRunEvent.objects.filter(run__project_id=project_id).delete()
    return summary


def make_fixture_project(kind: str, *, title: str | None = None) -> ResearchProject:
    """Create (or reset) an independent fixture project of the given kind.

    kind:
        - "evidence"   : reset + seed with the standard demo papers
        - "empty"      : reset, no papers (abstention tests)
        - "isolation"  : reset + seed with the disjoint-topic isolation papers

    Each kind uses a fixed, descriptive title so repeated calls reuse the same
    project row and reset it (snapshot semantics). The project is always
    ``status="active"`` so it is RAG-queryable.
    """
    if kind not in {"evidence", "empty", "isolation"}:
        raise ValueError(f"unknown fixture kind: {kind!r}")

    default_title = {
        "evidence": "Fixture: evidence (Mamba + Attention)",
        "empty": "Fixture: empty (abstention)",
        "isolation": "Fixture: isolation (GNN)",
    }[kind]
    target_title = title or default_title

    project = ResearchProject.objects.filter(title=target_title).first()
    if project is None:
        project = ResearchProject.objects.create(
            title=target_title,
            description=f"Fixed snapshot fixture ({kind}) for deterministic eval.",
            status="active",
        )
    else:
        # Snapshot: clear accumulated runs/sessions/reports/links before re-seed.
        reset_project_state(project.id)
        project.status = "active"
        project.save(update_fields=["status", "updated_at"])

    if kind == "evidence":
        # Reuse the demo paper definitions (Mamba + Attention) but in this
        # isolated project so it never shares state with the demo workspace.
        from .demo import DEMO_PAPERS
        async_to_sync(add_papers_to_project)(project.id, DEMO_PAPERS, "fixture seed")
    elif kind == "isolation":
        async_to_sync(add_papers_to_project)(project.id, ISOLATION_PAPERS, "isolation seed")
    # "empty": no papers.

    return project
