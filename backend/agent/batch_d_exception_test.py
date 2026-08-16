"""P2-D-R2-04: opaque-exception sensitive scan through the REAL error
boundary. An exception with no sensitive keywords is injected through a
real producer (the workflow task path) and the scan covers checkpoint,
pending writes, events, logs, API and Celery results."""
from __future__ import annotations

import json
import logging
from unittest import mock

from django.db import connection
from django.test import Client
from django.utils import timezone

from agent.batch_c_red_tests import _active, _link, _paper, _proj, \
    BatchCRedTestBase


class BD08OpaqueExceptionScanTest(BatchCRedTestBase):

    def test_DW_BD08_OPAQUE_EXCEPTION_NO_LEAK(self):
        self.case_id = "DW-BD08-OPAQUE-EXCEPTION-NO-LEAK"
        self.contract = ("an opaque exception (no sensitive keywords) "
                         "injected through a REAL producer enters the "
                         "error boundary; its body never reaches "
                         "checkpoint/pending writes/events/logs/API/"
                         "Celery result (P2-D-R2-04)")
        if connection.vendor != "postgresql":
            self.skipTest("PostgreSQL required")
        from api.models import ProjectRun
        import agent.project_workflow as pw
        from api.tasks import run_research_expand_workflow_task

        OPAQUE_MSG = "Xk9#mQ2$vL8pZ4wN7"  # no keywords, no PII
        proj = _proj("BD08")
        paper = _paper("BD08 P", "bd08-p")
        _link(proj, paper)
        _active(paper)

        class _Cap(logging.Handler):
            def __init__(self):
                super().__init__()
                self.records = []
            def emit(self, record):
                self.records.append(record.getMessage())

        handler = _Cap()
        root = logging.getLogger()
        root.addHandler(handler)

        run = ProjectRun.objects.create(
            project=proj, kind="workflow", status="pending",
            question="bd08")

        async def _search(*a, **kw):
            return {"papers": [{"title": "BD08 P", "year": 2024,
                                "arxiv_id": paper.arxiv_id,
                                "pdf_url": paper.pdf_url}]}

        # REAL producer raising the opaque exception mid-graph
        async def _boom(*a, **kw):
            raise RuntimeError(OPAQUE_MSG)

        result = None
        try:
            with mock.patch.object(pw, "search_papers", _search), \
                 mock.patch.object(pw, "draft_report_section", _boom), \
                 mock.patch("api.tasks.ingest_paper_pdf_task.delay",
                            return_value=type("R", (), {"id": "f"})()):
                try:
                    result = run_research_expand_workflow_task.run(run.id)
                except Exception as exc:  # noqa: BLE001
                    result = {"raised": exc.__class__.__name__}
        finally:
            root.removeHandler(handler)

        run.refresh_from_db()
        self._checks.append({"surface": "error_boundary",
                             "sentinel": "reached",
                             "found": run.status != "error",
                             "note": run.status})
        self.assertEqual(run.status, "error",
                         "opaque exception did not reach error boundary (BD-08)")

        surfaces = {}
        ev_blob = " ".join(str(v) for v in
                           run.events.values_list("payload", flat=True))
        surfaces["events"] = OPAQUE_MSG in ev_blob
        ck_found = []
        with connection.cursor() as cur:
            for tbl in ("checkpoints", "checkpoint_blobs",
                        "checkpoint_writes"):
                cur.execute(f"SELECT * FROM {tbl}")
                for row in cur.fetchall():
                    blob = " ".join(str(v) for v in row
                                    if v is not None)
                    if OPAQUE_MSG in blob:
                        ck_found.append(tbl)
        surfaces["checkpoint"] = bool(ck_found)
        log_blob = " ".join(handler.records)
        surfaces["logs"] = OPAQUE_MSG in log_blob
        client = Client(HTTP_HOST="localhost")
        resp = client.get(f"/api/projects/{proj.id}/runs")
        api_blob = json.dumps(resp.json(), default=str)
        surfaces["api"] = OPAQUE_MSG in api_blob
        surfaces["celery_result"] = OPAQUE_MSG in json.dumps(
            result, default=str)
        # run.error_message stores only the class name + stable text
        surfaces["run_error_message"] = OPAQUE_MSG in (
            run.error_message or "")

        for surface, leaked in surfaces.items():
            self._checks.append({"surface": surface,
                                 "sentinel": "exception_body",
                                 "found": leaked, "note": ""})
        leaks = [k for k, v in surfaces.items() if v]
        self.assertEqual(leaks, [],
                         f"opaque exception body leaked (BD-08): {leaks}")
