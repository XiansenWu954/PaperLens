"""PaperLens MCP server.

The MCP surface intentionally exposes a small, auditable tool set for external
clients. Project tools are derived from the same Function Calling schemas used
by the in-app Agent harness so the two surfaces cannot silently drift.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sys

import mcp.types as types
from mcp.server import Server
from mcp.server.stdio import stdio_server

from agent.project_tools import PROJECT_AGENT_TOOLS, dumps_tool_result, execute_project_tool

logger = logging.getLogger(__name__)

server = Server("paperlens")

MCP_PROJECT_TOOL_NAMES = {
    "search_papers",
    "query_project_rag",
    "list_project_papers",
    "get_project_citation_graph",
}

LEGACY_TOOLS = [
    types.Tool(
        name="gather_evidence",
        description="Gather grounded evidence snippets from indexed papers for a research question.",
        inputSchema={
            "type": "object",
            "properties": {
                "question": {"type": "string", "description": "The research question to support with evidence."},
            },
            "required": ["question"],
        },
    ),
]


def _project_mcp_tools() -> list[types.Tool]:
    tools: list[types.Tool] = []
    for spec in PROJECT_AGENT_TOOLS:
        fn = spec["function"]
        if fn["name"] not in MCP_PROJECT_TOOL_NAMES:
            continue
        tools.append(
            types.Tool(
                name=fn["name"],
                description=fn["description"],
                inputSchema=fn["parameters"],
            )
        )
    return tools


_TOOLS = [*_project_mcp_tools(), *LEGACY_TOOLS]


async def _handle_list_tools(request, meta) -> types.ListToolsResult:
    return types.ListToolsResult(tools=_TOOLS)


async def _handle_call_tool(request, meta) -> types.CallToolResult:
    """Handle tools/call params from either dict or pydantic-style request data."""
    _ensure_django()
    from agent.tools import execute_tool

    params = getattr(request, "params", None) or {}
    if isinstance(params, dict):
        name = params.get("name", "")
        arguments = params.get("arguments") or {}
    else:
        name = getattr(params, "name", "") or ""
        arguments = getattr(params, "arguments", None) or {}
    try:
        if name in MCP_PROJECT_TOOL_NAMES:
            result = dumps_tool_result(await execute_project_tool(name, arguments))
        elif name == "gather_evidence":
            result = await execute_tool(name, arguments)
        else:
            result = json.dumps({"error": f"unknown MCP tool {name}"}, ensure_ascii=False)
        return types.CallToolResult(
            content=[types.TextContent(type="text", text=result)],
            isError=False,
        )
    except Exception as e:
        logger.exception("mcp call_tool failed name=%s", name)
        return types.CallToolResult(
            content=[types.TextContent(type="text", text=json.dumps({"error": str(e)}, ensure_ascii=False))],
            isError=True,
        )


server.add_request_handler("tools/list", types.PaginatedRequestParams, _handle_list_tools)
server.add_request_handler("tools/call", types.CallToolRequestParams, _handle_call_tool)

_django_ready = False


def _ensure_django():
    global _django_ready
    if _django_ready:
        return
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    import django
    django.setup()
    _django_ready = True


async def main():
    _ensure_django()
    logger.info("PaperLens MCP server started over stdio")
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main())
