"""api 测试：ResearchTask CRUD + REST 端点。"""
from unittest import mock

from django.test import TestCase
from django.core.files.uploadedfile import SimpleUploadedFile
from rest_framework.test import APIClient

from api.models import ResearchTask
from api.models import ChatMessage, PaperIngestionJob, PaperRelation, ProjectPaper, ProjectRun, ProjectRunEvent, ReportVersion, ResearchProject
from papers.models import Paper


class ResearchTaskModelTest(TestCase):
    def test_create_default(self):
        t = ResearchTask.objects.create(question="test")
        self.assertEqual(t.status, "pending")
        self.assertEqual(t.final_report, "")
        self.assertEqual(t.sources, [])
        self.assertEqual(t.citation_graph, {})

    def test_update_status(self):
        t = ResearchTask.objects.create(question="t")
        t.status = "done"
        t.final_report = "# 综述"
        t.sources = [{"title": "p"}]
        t.save()
        t.refresh_from_db()
        self.assertEqual(t.status, "done")
        self.assertEqual(len(t.sources), 1)


class CreateResearchEndpointTest(TestCase):
    def setUp(self):
        self.client = APIClient()

    def test_create_task(self):
        r = self.client.post("/api/research", {"question": "Mamba"}, format="json")
        self.assertEqual(r.status_code, 201)
        self.assertIn("task_id", r.data)
        self.assertIn("X-Request-ID", r.headers)
        self.assertEqual(r.data["question"], "Mamba")
        self.assertTrue(ResearchTask.objects.filter(id=r.data["task_id"]).exists())

    def test_create_empty_question(self):
        r = self.client.post("/api/research", {"question": "   "}, format="json")
        self.assertEqual(r.status_code, 400)

    def test_create_missing_question(self):
        r = self.client.post("/api/research", {}, format="json")
        self.assertEqual(r.status_code, 400)


class GetResearchEndpointTest(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.task = ResearchTask.objects.create(question="q", status="done", final_report="# R")

    def test_get_existing(self):
        r = self.client.get(f"/api/research/{self.task.id}")
        self.assertEqual(r.status_code, 200)
        self.assertIn("X-Request-ID", r.headers)
        self.assertEqual(r.data["status"], "done")
        self.assertEqual(r.data["final_report"], "# R")

    def test_get_not_found(self):
        r = self.client.get("/api/research/99999")
        self.assertEqual(r.status_code, 404)


class ProjectWorkspaceEndpointTest(TestCase):
    def setUp(self):
        self.client = APIClient()

    def test_project_crud(self):
        r = self.client.post("/api/projects", {"title": "Mamba", "description": "SSM"}, format="json")
        self.assertEqual(r.status_code, 201)
        project_id = r.data["id"]
        self.assertEqual(r.data["paper_count"], 0)

        r2 = self.client.patch(f"/api/projects/{project_id}", {"description": "Updated"}, format="json")
        self.assertEqual(r2.status_code, 200)
        self.assertEqual(r2.data["description"], "Updated")

        r3 = self.client.get("/api/projects")
        self.assertEqual(r3.status_code, 200)
        self.assertEqual(len(r3.data), 1)

        d = self.client.delete(f"/api/projects/{project_id}")
        self.assertEqual(d.status_code, 204)
        self.assertEqual(self.client.get("/api/projects").data, [])
        archived = self.client.get("/api/projects?include_archived=1")
        self.assertEqual(len(archived.data), 1)
        self.assertEqual(archived.data[0]["status"], "archived")

    def test_report_version(self):
        project = ResearchProject.objects.create(title="Report")
        with self.assertLogs("api.views", level="INFO") as captured:
            r = self.client.post(
                f"/api/projects/{project.id}/reports",
                {"title": "v1", "content": "# R", "source": "user"},
                format="json",
            )
        self.assertEqual(r.status_code, 201)
        self.assertEqual(ReportVersion.objects.filter(project=project).count(), 1)
        saved = next(record for record in captured.records if record.event == "project_report_saved")
        self.assertEqual(saved.project_id, project.id)
        self.assertEqual(saved.report_source, "user")
        self.assertEqual(saved.content_chars, 3)
        self.assertNotIn("# R", saved.getMessage())

    def test_report_list_logs_count_without_content(self):
        project = ResearchProject.objects.create(title="Report list")
        ReportVersion.objects.create(project=project, title="v1", content="# Sensitive report")
        with self.assertLogs("api.views", level="INFO") as captured:
            r = self.client.get(f"/api/projects/{project.id}/reports")
        self.assertEqual(r.status_code, 200)
        listed = next(record for record in captured.records if record.event == "project_reports_listed")
        self.assertEqual(listed.project_id, project.id)
        self.assertEqual(listed.report_count, 1)
        self.assertNotIn("Sensitive report", listed.getMessage())


class ProjectPaperEndpointTest(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.project = ResearchProject.objects.create(title="Library")
        self.paper = Paper.objects.create(title="Attention Is All You Need", year=2017, citation_count=10)

    def test_add_and_remove_project_paper_keeps_global_paper(self):
        r = self.client.post(
            f"/api/projects/{self.project.id}/papers",
            {"paper_id": self.paper.id, "status": "core"},
            format="json",
        )
        self.assertEqual(r.status_code, 201)
        self.assertEqual(ProjectPaper.objects.count(), 1)

        d = self.client.delete(f"/api/projects/{self.project.id}/papers/{self.paper.id}")
        self.assertEqual(d.status_code, 204)
        self.assertEqual(ProjectPaper.objects.count(), 0)
        self.assertTrue(Paper.objects.filter(id=self.paper.id).exists())

    def test_import_bibtex_creates_project_papers(self):
        bib = (
            "@article{k1,\n title={Paper One}, author={Doe, John}, year={2024}, doi={10.1/p1}\n}\n"
            "@article{k2,\n title={Paper Two}, author={Roe, Jane}, year={2023}\n}\n"
        )
        r = self.client.post(
            f"/api/projects/{self.project.id}/papers/import",
            {"format": "bibtex", "text": bib},
            format="json",
        )
        self.assertEqual(r.status_code, 201)
        self.assertEqual(r.data["count"], 2)
        self.assertEqual(ProjectPaper.objects.filter(project=self.project).count(), 2)
        titles = {pp.paper.title for pp in ProjectPaper.objects.filter(project=self.project)}
        self.assertEqual(titles, {"Paper One", "Paper Two"})

    def test_import_bibtex_idempotent(self):
        bib = "@article{k1,\n title={Dup}, year={2024}, doi={10.1/dup}\n}\n"
        self.client.post(f"/api/projects/{self.project.id}/papers/import", {"format": "bibtex", "text": bib}, format="json")
        r2 = self.client.post(f"/api/projects/{self.project.id}/papers/import", {"format": "bibtex", "text": bib}, format="json")
        self.assertEqual(r2.status_code, 201)
        self.assertEqual(ProjectPaper.objects.filter(project=self.project).count(), 1)

    def test_export_bibtex_and_ris(self):
        self.client.post(
            f"/api/projects/{self.project.id}/papers",
            {"paper_id": self.paper.id, "status": "included"},
            format="json",
        )
        rb = self.client.get(f"/api/projects/{self.project.id}/papers/export.bib")
        self.assertEqual(rb.status_code, 200)
        self.assertIn("Attention Is All You Need", rb.content.decode())
        self.assertIn("@", rb.content.decode())
        rr = self.client.get(f"/api/projects/{self.project.id}/papers/export.ris")
        self.assertEqual(rr.status_code, 200)
        self.assertIn("TY  - JOUR", rr.content.decode())

    def test_demo_seed(self):
        r = self.client.post("/api/projects/demo-seed", {}, format="json")
        self.assertEqual(r.status_code, 201)
        self.assertGreaterEqual(r.data["count"], 2)

        again = self.client.post("/api/projects/demo-seed", {}, format="json")
        self.assertEqual(again.status_code, 201)
        self.assertEqual(again.data["project"]["id"], r.data["project"]["id"])
        self.assertEqual(ResearchProject.objects.filter(title=r.data["project"]["title"], status="active").count(), 1)

    def test_search_add_endpoint_uses_project_library_tool(self):
        async def fake_search(query, max_results=5):
            return [
                {
                    "title": "Fixture Paper",
                    "abstract": "Fixture abstract",
                    "year": 2026,
                    "source": "fixture",
                    "source_id": "fixture-search-add",
                    "arxiv_id": "2601.00001",
                }
            ]

        with mock.patch("datasources.registry.search", fake_search):
            r = self.client.post(
                f"/api/projects/{self.project.id}/papers/search-add",
                {"query": "fixture agent paper", "max_results": 1},
                format="json",
            )
        self.assertEqual(r.status_code, 201)
        self.assertEqual(r.data["count"], 1)
        self.assertEqual(ProjectPaper.objects.filter(project=self.project).count(), 1)

    def test_excluded_project_paper_is_removed_from_citation_graph_scope(self):
        peer = Paper.objects.create(
            title="Transformer",
            year=2017,
            citation_count=100,
            referenced_works=["W1", "W2"],
        )
        self.paper.referenced_works = ["W1", "W2"]
        self.paper.save(update_fields=["referenced_works"])
        ProjectPaper.objects.create(project=self.project, paper=self.paper, status="included")
        ProjectPaper.objects.create(project=self.project, paper=peer, status="included")

        before = self.client.get(f"/api/projects/{self.project.id}/citation-graph")
        self.assertEqual(before.status_code, 200)
        self.assertEqual(len(before.data["nodes"]), 2)

        patch = self.client.patch(
            f"/api/projects/{self.project.id}/papers/{peer.id}",
            {"status": "excluded"},
            format="json",
        )
        self.assertEqual(patch.status_code, 200)

        after = self.client.get(f"/api/projects/{self.project.id}/citation-graph")
        self.assertEqual(after.status_code, 200)
        self.assertEqual(after.data["nodes"], [])

    def test_pdf_upload_endpoint_creates_eager_ingestion_job(self):
        ProjectPaper.objects.create(project=self.project, paper=self.paper, status="included")

        async def fake_ingest_pdf_bytes(paper, pdf_bytes, *, index_version=None, **kwargs):
            # Tasks 4.3: the activation transaction verifies persisted chunks
            # against the reported count — the mock must persist real rows.
            from asgiref.sync import sync_to_async
            from rag.embedding import embedding_metadata
            from rag.models import Text

            meta = embedding_metadata()

            def _write():
                for i in range(2):
                    Text.objects.create(
                        paper=paper, index_version=index_version,
                        docname=f"fake chunk{i}", chunk_index=i, content="fake",
                        embedding=[0.0] * 1024,
                        embedding_model=str(meta["embedding_model"]),
                        embedding_dim=int(meta["embedding_dim"]),
                        embedding_version=str(meta["embedding_version"]),
                        content_hash=f"h{i}", citation_key=f"pqac-f{i}")
                return 2

            return await sync_to_async(_write)()

        pdf = SimpleUploadedFile("paper.pdf", b"%PDF-1.4 fake", content_type="application/pdf")
        with mock.patch("api.tasks.ingest_pdf_bytes", fake_ingest_pdf_bytes):
            r = self.client.post(
                f"/api/projects/{self.project.id}/papers/{self.paper.id}/pdf-upload",
                {"file": pdf},
                format="multipart",
            )

        self.assertEqual(r.status_code, 201)
        self.assertEqual(r.data["status"], "embedded")
        self.assertEqual(r.data["chunk_count"], 2)
        job = PaperIngestionJob.objects.get(id=r.data["id"])
        self.assertEqual(job.project, self.project)
        self.assertEqual(job.paper, self.paper)
        self.assertEqual(job.status, "embedded")
        event_types = list(ProjectRunEvent.objects.filter(run__project=self.project).values_list("event_type", flat=True))
        self.assertIn("ingestion_started", event_types)
        self.assertIn("ingestion_completed", event_types)

    def test_pdf_url_ingest_requires_project_paper_scope(self):
        r = self.client.post(
            f"/api/projects/{self.project.id}/papers/{self.paper.id}/ingest",
            {},
            format="json",
        )
        self.assertEqual(r.status_code, 404)

    def test_workflow_endpoint_queues_project_run(self):
        async def fake_run(project_id, question, run_id):
            return {"report_id": 42, "search_results": [{"title": "Paper A"}]}

        with mock.patch("agent.project_workflow.run_project_research_expand", fake_run):
            r = self.client.post(
                f"/api/projects/{self.project.id}/workflows/research-expand",
                {"question": "扩大检索 Mamba 并生成报告"},
                format="json",
            )

        self.assertEqual(r.status_code, 201)
        run = ProjectRun.objects.get(id=r.data["id"])
        self.assertEqual(run.kind, "workflow")
        self.assertIn(run.status, {"done", "running", "pending"})
        event_types = list(ProjectRunEvent.objects.filter(run=run).values_list("event_type", flat=True))
        self.assertIn("workflow_queued", event_types)


class ProjectChatEndpointTest(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.project = ResearchProject.objects.create(title="Chat")
        paper = Paper.objects.create(title="Mamba", abstract="Selective state spaces", year=2023)
        ProjectPaper.objects.create(project=self.project, paper=paper, status="included")

    def test_chat_answers_from_project_library(self):
        r = self.client.post(
            f"/api/projects/{self.project.id}/chat",
            {"message": "Mamba 有什么特点？"},
            format="json",
        )
        self.assertEqual(r.status_code, 201)
        self.assertIn("answer", r.data)
        self.assertGreaterEqual(ChatMessage.objects.filter(session__project=self.project).count(), 2)
        event_types = list(
            ProjectRunEvent.objects.filter(run__project=self.project)
            .values_list("event_type", flat=True)
        )
        self.assertIn("intent_detected", event_types)
        self.assertIn("tool_call", event_types)
        self.assertIn("evidence", event_types)
        self.assertIn("done", event_types)

    def test_chat_blocks_destructive_project_intent(self):
        r = self.client.post(
            f"/api/projects/{self.project.id}/chat",
            {"message": "清空项目并删除所有论文"},
            format="json",
        )
        self.assertEqual(r.status_code, 201)
        self.assertIn("不会自主执行", r.data["answer"])
        event_types = list(
            ProjectRunEvent.objects.filter(run__project=self.project)
            .values_list("event_type", flat=True)
        )
        self.assertIn("intent_detected", event_types)
        self.assertNotIn("tool_call", event_types)

    def test_chat_graph_intent_returns_graph_event(self):
        r = self.client.post(
            f"/api/projects/{self.project.id}/chat",
            {"message": "刷新引用关系图谱"},
            format="json",
        )
        self.assertEqual(r.status_code, 201)
        event_types = list(
            ProjectRunEvent.objects.filter(run__project=self.project)
            .values_list("event_type", flat=True)
        )
        self.assertIn("graph", event_types)

    def test_project_runs_endpoint_returns_persisted_events(self):
        self.client.post(
            f"/api/projects/{self.project.id}/chat",
            {"message": "Mamba 有什么特点？"},
            format="json",
        )
        r = self.client.get(f"/api/projects/{self.project.id}/runs")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(len(r.data), 1)
        event_types = [event["event_type"] for event in r.data[0]["events"]]
        self.assertIn("intent_detected", event_types)
        self.assertIn("tool_call", event_types)


class PaperRelationEndpointTest(TestCase):
    """引用语境标注（支持/反对/提及）。"""

    def setUp(self):
        self.client = APIClient()
        self.project = ResearchProject.objects.create(title="Relations")
        # A 引用 B（openalex id 匹配）
        self.paper_a = Paper.objects.create(
            title="Paper A builds on B", year=2024, openalex_id="W111",
            abstract="We extend the method of Paper B.", referenced_works=["W222"],
        )
        self.paper_b = Paper.objects.create(
            title="Paper B base method", year=2020, openalex_id="W222",
            abstract="A foundational method for sequence modeling.",
        )
        ProjectPaper.objects.create(project=self.project, paper=self.paper_a, status="included")
        ProjectPaper.objects.create(project=self.project, paper=self.paper_b, status="included")

    def test_post_analyzes_relations_with_mocked_llm(self):
        fake = mock.MagicMock()
        fake.complete.return_value = {
            "content": '{"label": "supporting", "reason": "A extends B method"}',
        }
        with mock.patch("llm.deepseek.DeepSeekClient", return_value=fake):
            r = self.client.post(f"/api/projects/{self.project.id}/paper-relations", {}, format="json")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.data["analyzed"], 1)
        rel = PaperRelation.objects.get(project=self.project)
        self.assertEqual(rel.label, "supporting")
        self.assertIn("extends", rel.context)

    def test_get_returns_analyzed_relations(self):
        PaperRelation.objects.create(
            project=self.project, citing_paper=self.paper_a, cited_paper=self.paper_b,
            label="mentioning", context="brief mention", confidence=0.5,
        )
        r = self.client.get(f"/api/projects/{self.project.id}/paper-relations")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(len(r.data), 1)
        self.assertEqual(r.data[0]["label"], "mentioning")
        self.assertEqual(r.data[0]["citing_title"], "Paper A builds on B")


class FixtureResetTest(TestCase):
    """Snapshot fixtures: reset_project_state + make_fixture_project (§3.2)."""

    def test_reset_project_state_clears_runs_sessions_reports_links(self):
        from api.fixtures import reset_project_state
        from api.models import ChatSession

        project = ResearchProject.objects.create(title="Reset target", status="active")
        # Pollute it with the child rows a harness run would create.
        run = ProjectRun.objects.create(project=project, kind="chat", status="done", question="q")
        ProjectRunEvent.objects.create(run=run, event_type="tool_call", payload={"name": "rag"})
        session = ChatSession.objects.create(project=project)
        ChatMessage.objects.create(session=session, role="user", content="hi")
        ReportVersion.objects.create(project=project, title="r", content="c", source="agent")
        PaperRelation.objects.create(
            project=project, citing_paper=self.paper_a if hasattr(self, "paper_a") else None,
            cited_paper=None, label="mentioning", context="x", confidence=0.1,
        ) if hasattr(self, "paper_a") else None

        cleared = reset_project_state(project.id)
        # All child rows gone; project itself survives.
        self.assertEqual(ProjectRun.objects.filter(project_id=project.id).count(), 0)
        self.assertEqual(ProjectRunEvent.objects.filter(run__project_id=project.id).count(), 0)
        self.assertEqual(ChatSession.objects.filter(project_id=project.id).count(), 0)
        self.assertEqual(ChatMessage.objects.filter(session__project_id=project.id).count(), 0)
        self.assertEqual(ReportVersion.objects.filter(project_id=project.id).count(), 0)
        self.assertTrue(ResearchProject.objects.filter(id=project.id).exists())

    def test_make_fixture_project_creates_each_kind_and_is_idempotent(self):
        from api.fixtures import make_fixture_project

        evidence = make_fixture_project("evidence")
        empty = make_fixture_project("empty")
        isolation = make_fixture_project("isolation")

        # evidence has demo papers; empty has none; isolation has the GNN papers.
        self.assertGreaterEqual(ProjectPaper.objects.filter(project=evidence).count(), 2)
        self.assertEqual(ProjectPaper.objects.filter(project=empty).count(), 0)
        self.assertGreaterEqual(ProjectPaper.objects.filter(project=isolation).count(), 2)

        # Calling again reuses the same project row and resets (snapshot).
        evidence_again = make_fixture_project("evidence")
        self.assertEqual(evidence_again.id, evidence.id)
        self.assertGreaterEqual(ProjectPaper.objects.filter(project=evidence).count(), 2)
