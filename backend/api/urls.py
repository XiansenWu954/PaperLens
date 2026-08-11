"""API REST 路由（SSE 流式端点在 realtime.urls）。"""
from django.urls import include, path

from . import views

urlpatterns = [
    path("projects", views.projects, name="projects"),
    path("projects/demo-seed", views.demo_seed, name="demo_seed"),
    path("projects/<int:project_id>", views.project_detail, name="project_detail"),
    path("projects/<int:project_id>/papers", views.project_papers, name="project_papers"),
    path("projects/<int:project_id>/papers/import", views.project_papers_import, name="project_papers_import"),
    path("projects/<int:project_id>/papers/export.bib", views.project_papers_export_bib, name="project_papers_export_bib"),
    path("projects/<int:project_id>/papers/export.ris", views.project_papers_export_ris, name="project_papers_export_ris"),
    path("projects/<int:project_id>/papers/search-add", views.project_search_add, name="project_search_add"),
    path("projects/<int:project_id>/papers/<int:paper_id>", views.project_paper_detail, name="project_paper_detail"),
    path("projects/<int:project_id>/papers/<int:paper_id>/pdf-upload", views.project_paper_pdf_upload, name="project_paper_pdf_upload"),
    path("projects/<int:project_id>/papers/<int:paper_id>/ingest", views.project_paper_ingest, name="project_paper_ingest"),
    path("projects/<int:project_id>/ingestion-jobs", views.project_ingestion_jobs, name="project_ingestion_jobs"),
    path("projects/<int:project_id>/citation-graph", views.project_citation_graph, name="project_citation_graph"),
    path("projects/<int:project_id>/papers/<int:paper_a_id>/path/<int:paper_b_id>", views.project_paper_connection_path, name="project_paper_connection_path"),
    path("projects/<int:project_id>/paper-relations", views.project_paper_relations, name="project_paper_relations"),
    path("projects/<int:project_id>/runs", views.project_runs, name="project_runs"),
    path("projects/<int:project_id>/workflows/research-expand", views.project_research_expand_workflow, name="project_research_expand_workflow"),
    path("projects/<int:project_id>/chat", views.project_chat, name="project_chat"),
    path("projects/<int:project_id>/chat/<int:session_id>/stream", views.project_chat_stream, name="project_chat_stream"),
    path("projects/<int:project_id>/reports", views.project_reports, name="project_reports"),
    path("research", views.create_research, name="create_research"),
    path("research/<int:task_id>", views.get_research, name="get_research"),
    path("research/", include("realtime.urls")),
]
