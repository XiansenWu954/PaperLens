"""In-process MCP test client (Task 2.5, §18.2).

Calls tools through the REAL registered handler entry: an in-memory transport
pair drives the actual ``mcp.server.Server`` dispatcher, which invokes the
registered ``tools/call`` handler with a real ``ServerRequestContext`` and
typed ``CallToolRequestParams``. Client ``meta`` is passed exactly as a real
client would send it — it is an open map, and the tests assert it is never
treated as server-bound identity.
"""
from __future__ import annotations

import asyncio
from typing import Any

from mcp import ClientSession
from mcp.shared.memory import create_client_server_memory_streams
from mcp.types import CallToolResult

from mcp_server import server as mcp_server


def call_tool_via_client(
    name: str, arguments: dict[str, Any] | None = None, meta: dict | None = None
) -> CallToolResult:
    """One tools/call round trip through the real server dispatcher."""

    async def _run() -> CallToolResult:
        async with create_client_server_memory_streams() as (client, server):
            server_task = asyncio.create_task(
                mcp_server.server.run(
                    server[0],
                    server[1],
                    mcp_server.server.create_initialization_options(),
                )
            )
            try:
                async with ClientSession(client[0], client[1]) as session:
                    await session.initialize()
                    result = await session.call_tool(
                        name, arguments or {}, meta=meta
                    )
                    return result
            finally:
                server_task.cancel()
                try:
                    await server_task
                except asyncio.CancelledError:
                    pass

    return asyncio.run(_run())
