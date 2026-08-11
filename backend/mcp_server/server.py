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
from asgiref.sync import sync_to_async
from mcp.server import Server
from mcp.server.stdio import stdio_server

from agent.context import ToolExecutionContext, create_context
from agent.project_tools import PROJECT_AGENT_TOOLS, dumps_tool_result, execute_project_tool

logger = logging.getLogger(__name__)

server = Server("paperlens")

MCP_PROJECT_TOOL_NAMES = {
    "search_papers",
    "query_project_rag",
    "list_project_papers",
    "get_project_citation_graph",
}

# Declared output schemas (Task 2.5): the MCP adapter validates its structured
# results against these. search_papers schema is declared; its structured-result
# validation is exercised offline in tests only for the non-network tools.
_MCP_OUTPUT_SCHEMAS = {
    "search_papers": {
        "type": "object",
        "properties": {
            "papers": {"type": "array", "items": {"type": "object"}},
            "count": {"type": "integer"},
        },
        "required": ["papers", "count"],
    },
    "query_project_rag": {
        "type": "object",
        "properties": {
            "evidence": {"type": "array", "items": {"type": "object"}},
            "fallback": {"type": "string"},
        },
        "required": ["evidence", "fallback"],
    },
    "list_project_papers": {
        "type": "object",
        "properties": {
            "papers": {"type": "array", "items": {"type": "object"}},
            "count": {"type": "integer"},
        },
        "required": ["papers", "count"],
    },
    "get_project_citation_graph": {
        "type": "object",
        "properties": {
            "graph": {
                "type": "object",
                "properties": {
                    "nodes": {"type": "array", "items": {"type": "object"}},
                    "edges": {"type": "array", "items": {"type": "object"}},
                },
            }
        },
        "required": ["graph"],
    },
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
                outputSchema=_MCP_OUTPUT_SCHEMAS.get(fn["name"]),
            )
        )
    return tools


_TOOLS = [*_project_mcp_tools(), *LEGACY_TOOLS]


def _server_bound_project_id() -> tuple[int | None, bool]:
    """Server-bound project from process configuration (Task 2.5, §18.2/§19).

    The ONLY legitimate server-bound project sources are server configuration,
    lifespan, or authenticated session state. The current stdio single-user
    deployment reads it from ``PAPERLENS_MCP_PROJECT_ID``.

    Security note: the client-controlled request ``_meta`` is an OPEN MAP — a
    client can send arbitrary keys. The security property is therefore that
    this handler NEVER reads ``_meta`` as an identity at all.

    Validation (§19): the configured value must be a POSITIVE integer AND the
    project must exist (existence is checked by the caller). An invalid value
    fails closed with the unified selector error — it must never silently
    produce an empty-project success.
    """
    raw = os.environ.get("PAPERLENS_MCP_PROJECT_ID", "").strip()
    if not raw:
        return None, False
    if raw.isdigit() and int(raw) > 0:
        return int(raw), False
    logger.warning(
        "PAPERLENS_MCP_PROJECT_ID ignored: not a positive integer",
        extra={"event": "mcp_server_bound_config_invalid"},
    )
    return None, True


# Unified, stable selector error shape (§18.2): missing / invalid / nonexistent
# selectors are indistinguishable, so the single-user transport bootstrap leaks
# no existence or shape information.
_SELECTOR_ERROR = {
    "error": "project_not_found",
    "message": "项目不存在。",
}


async def _resolve_mcp_context(
    arguments: dict,
) -> tuple[ToolExecutionContext | None, dict | None]:
    """Transport-level project context resolution (Task 2.5, §18.2).

    SECURITY BOUNDARY (explicitly documented): PaperLens currently has NO
    multi-user authentication. Resolution order:
    1. A server-bound project (PAPERLENS_MCP_PROJECT_ID / session auth state)
       wins unconditionally: the value must be a positive integer pointing at
       an existing project, and a conflicting client selector is logged and
       never overrides it. Invalid server-bound config fails closed.
    2. Without a server-bound project, an explicit client selector is ONLY a
       single-user transport bootstrap: it is validated to reference an
       existing project, then a frozen ToolExecutionContext is created.
    3. Missing / invalid / nonexistent selectors all return the SAME stable
       error shape (no existence or reason disclosure).
    """
    bound, config_invalid = _server_bound_project_id()
    if config_invalid:
        return None, _SELECTOR_ERROR
    from api.models import ResearchProject

    if bound is not None:
        if selector := arguments.get("project_id"):
            try:
                if int(selector) != bound:
                    logger.warning(
                        "mcp selector ignored: server-bound context wins",
                        extra={
                            "event": "mcp_selector_conflict",
                            "bound_project_id": bound,
                            "selector_project_id": selector,
                        },
                    )
            except (TypeError, ValueError):
                logger.warning(
                    "mcp selector ignored: unparseable",
                    extra={"event": "mcp_selector_conflict", "bound_project_id": bound},
                )
        exists = await sync_to_async(
            ResearchProject.objects.filter(id=bound).exists
        )()
        if not exists:
            logger.warning(
                "server-bound project does not exist",
                extra={"event": "mcp_server_bound_missing", "bound_project_id": bound},
            )
            return None, _SELECTOR_ERROR
        return create_context(bound), None
    selector = arguments.get("project_id")
    if selector is None:
        return None, _SELECTOR_ERROR
    try:
        project_id = int(selector)
    except (TypeError, ValueError):
        return None, _SELECTOR_ERROR
    exists = await sync_to_async(
        ResearchProject.objects.filter(id=project_id).exists
    )()
    if not exists:
        return None, _SELECTOR_ERROR
    return create_context(project_id), None


async def _handle_list_tools(request, meta) -> types.ListToolsResult:
    return types.ListToolsResult(tools=_TOOLS)


async def _handle_call_tool(request, params) -> types.CallToolResult:
    """tools/call handler with the real SDK contract (Task 2.5, §18.2).

    The SDK dispatcher calls registered handlers as
    ``handler(ServerRequestContext, typed_params)`` where the second argument
    is the already-validated ``CallToolRequestParams``. The client-controlled
    ``params.meta`` is an OPEN MAP and can carry arbitrary keys; it is never
    consulted as an authorization identity — server-bound project identity
    comes only from process configuration (PAPERLENS_MCP_PROJECT_ID) or
    session auth state.
    """
    _ensure_django()
    from agent.tools import execute_tool

    name = getattr(params, "name", "") or ""
    arguments = getattr(params, "arguments", None) or {}
    try:
        if name in MCP_PROJECT_TOOL_NAMES:
            context, error = await _resolve_mcp_context(arguments)
            if error is not None:
                return types.CallToolResult(
                    content=[types.TextContent(type="text", text=dumps_tool_result(error))],
                    structured_content=error,
                    isError=True,
                )
            args = {k: v for k, v in arguments.items() if k != "project_id"}
            result_dict = await execute_project_tool(context, name, args)
            return types.CallToolResult(
                content=[types.TextContent(type="text", text=dumps_tool_result(result_dict))],
                structured_content=result_dict,
                isError=False,
            )
        elif name == "gather_evidence":
            result = await execute_tool(name, arguments)
            return types.CallToolResult(
                content=[types.TextContent(type="text", text=result)],
                isError=False,
            )
        else:
            # §32.3: unknown tool names become the stable code + digest —
            # the original name never enters the CallToolResult or logs.
            from agent.events import sanitize_tool_name

            code, digest = sanitize_tool_name(name)
            payload = {"error": code, "message": "未知工具，请检查工具名。"}
            if digest:
                payload["tool_hash"] = digest
            result = json.dumps(payload, ensure_ascii=False)
            return types.CallToolResult(
                content=[types.TextContent(type="text", text=result)],
                structured_content=payload,
                isError=True,
            )
    except Exception as e:
        # §31.1: stable public error code + fixed copy — never str(e).
        from agent.events import error_hash, safe_stack_frames

        logger.error(
            "mcp call_tool failed",
            extra={
                "event": "mcp_call_tool_failed",
                "tool_name": name,
                "error": e.__class__.__name__,
                "error_hash": error_hash(e),
                "stack_frames": safe_stack_frames(e),
            },
        )
        return types.CallToolResult(
            content=[types.TextContent(
                type="text",
                text=json.dumps({
                    "error": e.__class__.__name__,
                    "message": "工具调用失败，请稍后重试。",
                }, ensure_ascii=False))],
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
