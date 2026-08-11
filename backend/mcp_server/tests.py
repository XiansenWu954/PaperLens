import asyncio
import json
from unittest import mock

from django.test import TransactionTestCase

from agent.project_tools import PROJECT_AGENT_TOOLS
from mcp_server import server as mcp_server
from mcp_server.testing import call_tool_via_client


class ToolsSchemaTest(TransactionTestCase):
    def test_mcp_surface_exposes_safe_project_tools_and_legacy_evidence_tool(self):
        names = {tool.name for tool in mcp_server._TOOLS}
        self.assertEqual(
            names,
            {
                "search_papers",
                "query_project_rag",
                "list_project_papers",
                "get_project_citation_graph",
                "gather_evidence",
            },
        )

    def test_project_mcp_tools_derive_from_function_calling_contracts(self):
        project_contracts = {
            tool["function"]["name"]: tool["function"]
            for tool in PROJECT_AGENT_TOOLS
            if tool["function"]["name"] in mcp_server.MCP_PROJECT_TOOL_NAMES
        }
        self.assertEqual(set(project_contracts), mcp_server.MCP_PROJECT_TOOL_NAMES)

        for tool in mcp_server._TOOLS:
            if tool.name == "gather_evidence":
                continue
            function_contract = project_contracts[tool.name]
            self.assertEqual(tool.description, function_contract["description"])
            self.assertEqual(tool.input_schema, function_contract["parameters"])
            self.assertEqual(tool.input_schema["type"], "object")
            self.assertIn("required", tool.input_schema)
            self.assertIsNotNone(tool.output_schema,
                                 f"{tool.name} must declare an output schema")

    def test_mcp_surface_does_not_expose_write_or_report_internal_tools(self):
        names = {tool.name for tool in mcp_server._TOOLS}
        self.assertNotIn("add_papers_to_project", names)
        self.assertNotIn("draft_report_section", names)

    def test_all_project_function_calling_schemas_are_valid_objects(self):
        for tool in PROJECT_AGENT_TOOLS:
            with self.subTest(tool=tool["function"]["name"]):
                self.assertEqual(tool["type"], "function")
                function = tool["function"]
                self.assertTrue(function["name"])
                self.assertTrue(function["description"])
                self.assertEqual(function["parameters"]["type"], "object")
                self.assertIsInstance(function["parameters"].get("required", []), list)


class ListToolsHandlerTest(TransactionTestCase):
    def test_list_tools_returns_all_registered_tools(self):
        result = asyncio.run(mcp_server._handle_list_tools(None, None))
        names = {tool.name for tool in result.tools}
        self.assertEqual(names, {tool.name for tool in mcp_server._TOOLS})


class CallToolHandlerTest(TransactionTestCase):
    """Runs every call through the REAL registered handler entry via an
    in-process MCP client (Task 2.5, §18.2): memory transport + ClientSession,
    so the SDK dispatcher invokes the handler with a real ServerRequestContext
    and typed CallToolRequestParams."""

    def setUp(self):
        from api.models import ResearchProject
        self.project = ResearchProject.objects.create(title="MCP test project")

    def test_call_project_search_papers_routes_to_project_tool_executor(self):
        async def fake_execute(context, name, args):
            return {"papers": [{"title": args["query"]}], "count": 1}

        with mock.patch("mcp_server.server.execute_project_tool", fake_execute):
            result = call_tool_via_client(
                "search_papers",
                {"project_id": self.project.id, "query": "RAG"},
            )

        self.assertFalse(result.is_error)
        payload = json.loads(result.content[0].text)
        self.assertEqual(payload["count"], 1)
        self.assertEqual(payload["papers"][0]["title"], "RAG")

    def test_call_project_rag_routes_to_project_tool_executor(self):
        async def fake_execute(context, name, args):
            return {"evidence": [{"paper_id": 1, "summary": args["question"]}], "fallback": ""}

        with mock.patch("mcp_server.server.execute_project_tool", fake_execute):
            result = call_tool_via_client(
                "query_project_rag",
                {"project_id": self.project.id, "question": "why"},
            )

        payload = json.loads(result.content[0].text)
        self.assertFalse(result.is_error)
        self.assertEqual(payload["evidence"][0]["summary"], "why")

    def test_call_project_library_and_graph_support_object_params(self):
        calls = []

        async def fake_execute(context, name, args):
            calls.append((name, args))
            if name == "list_project_papers":
                return {"papers": [{"title": "Paper A"}], "count": 1}
            return {"graph": {"nodes": [{"id": 1}], "edges": []}}

        with mock.patch("mcp_server.server.execute_project_tool", fake_execute):
            papers = call_tool_via_client(
                "list_project_papers", {"project_id": self.project.id})
            graph = call_tool_via_client(
                "get_project_citation_graph", {"project_id": self.project.id})

        self.assertEqual([name for name, _ in calls], ["list_project_papers", "get_project_citation_graph"])
        self.assertEqual(json.loads(papers.content[0].text)["count"], 1)
        self.assertEqual(len(json.loads(graph.content[0].text)["graph"]["nodes"]), 1)

    def test_call_legacy_gather_evidence_routes_to_legacy_executor(self):
        async def fake_execute(name, args):
            return json.dumps({"tool": name, "question": args["question"]})

        with mock.patch("agent.tools.execute_tool", fake_execute):
            result = call_tool_via_client("gather_evidence", {"question": "q"})

        self.assertFalse(result.is_error)
        payload = json.loads(result.content[0].text)
        self.assertEqual(payload["tool"], "gather_evidence")

    def test_unknown_tool_returns_json_error_without_dispatch(self):
        with mock.patch("mcp_server.server.execute_project_tool") as project_execute:
            result = call_tool_via_client("nonexistent", {})

        project_execute.assert_not_called()
        # §32.3: stable unknown_tool + digest — the original name never leaks.
        self.assertTrue(result.is_error)
        payload = json.loads(result.content[0].text)
        self.assertEqual(payload["error"], "unknown_tool")
        self.assertNotIn("nonexistent", result.content[0].text)
        self.assertIn("tool_hash", payload)

    def test_tool_exception_returns_error_result(self):
        async def fake_execute(context, name, args):
            raise RuntimeError("boom")

        with mock.patch("mcp_server.server.execute_project_tool", fake_execute):
            result = call_tool_via_client(
                "query_project_rag", {"project_id": self.project.id, "question": "q"})

        self.assertTrue(result.is_error)
        # §31.1: the error surface carries the stable code + fixed copy —
        # never the raw exception message.
        payload = json.loads(result.content[0].text)
        self.assertEqual(payload["error"], "RuntimeError")
        self.assertNotIn("boom", result.content[0].text)

    def test_selector_for_nonexistent_project_returns_scoped_error(self):
        """Bootstrap validation: any invalid selector returns the SAME stable
        scoped error without dispatching the tool (§18.2)."""
        with mock.patch("mcp_server.server.execute_project_tool") as project_execute:
            missing = call_tool_via_client("list_project_papers", {})
            nonexistent = call_tool_via_client("list_project_papers", {"project_id": 99999999})
            invalid = call_tool_via_client("list_project_papers", {"project_id": "abc"})

        project_execute.assert_not_called()
        for result in (missing, nonexistent, invalid):
            self.assertTrue(result.is_error)
        payloads = [json.loads(r.content[0].text) for r in (missing, nonexistent, invalid)]
        self.assertEqual(payloads[0], payloads[1])
        self.assertEqual(payloads[1], payloads[2])
        self.assertEqual(payloads[0]["error"], "project_not_found")

    def test_server_bound_project_wins_over_selector(self):
        """§18.2: PAPERLENS_MCP_PROJECT_ID is the server-bound project; a client
        selector B must not override it."""
        import os
        from agent.context import ToolExecutionContext

        calls = []

        async def fake_execute(context, name, args):
            calls.append((context, name, dict(args)))
            return {"papers": [], "count": 0}

        with mock.patch.dict(os.environ,
                             {"PAPERLENS_MCP_PROJECT_ID": str(self.project.id)}), \
             mock.patch("mcp_server.server.execute_project_tool", fake_execute):
            result = call_tool_via_client(
                "list_project_papers", {"project_id": 424242})

        self.assertFalse(result.is_error)
        self.assertTrue(calls)
        context = calls[0][0]
        self.assertIsInstance(context, ToolExecutionContext)
        self.assertEqual(context.project_id, self.project.id)
        self.assertNotIn("project_id", calls[0][2])
