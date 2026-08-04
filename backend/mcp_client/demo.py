"""PaperLens MCP Client（验证双向闭环）。

连接本地 mcp_server.server（stdio），列出工具 + 调用 search_papers，确认闭环。
用法：python -m mcp_client.demo
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

logger = logging.getLogger(__name__)


async def run_demo(query: str = "transformer attention") -> dict:
    """连接 server → 列工具 → 调用 search_papers，返回 {tools, result}。"""
    # server 入口：python -m mcp_server.server（在工作目录 backend/ 下）
    backend_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "mcp_server.server"],
        cwd=backend_dir,
        env={**os.environ, "PYTHONPATH": backend_dir},
    )

    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            # 1. 列工具
            tools_resp = await session.list_tools()
            tool_names = [t.name for t in tools_resp.tools]
            logger.info("发现工具: %s", tool_names)

            # 2. 调用 search_papers
            result = await session.call_tool("search_papers", {"query": query, "max_results": 3})
            text = result.content[0].text if result.content else ""
            logger.info("search_papers 返回 %d 字符", len(text))
            return {"tools": tool_names, "result_text": text}


def main():
    logging.basicConfig(level=logging.INFO, format="%(name)s %(levelname)s %(message)s")
    query = sys.argv[1] if len(sys.argv) > 1 else "transformer attention"
    print("=" * 50)
    print(f"MCP 双向验证（query={query}）")
    print("=" * 50)
    info = asyncio.run(run_demo(query))
    print(f"\n发现工具: {info['tools']}")
    print(f"返回结果前 200 字: {info['result_text'][:200]}")
    ok = len(info["tools"]) >= 2 and len(info["result_text"]) > 0
    print(f"\n验证: 工具≥2={len(info['tools'])>=2}, 有结果={len(info['result_text'])>0} -> {'通过 ✓' if ok else '失败 ✗'}")


if __name__ == "__main__":
    main()
