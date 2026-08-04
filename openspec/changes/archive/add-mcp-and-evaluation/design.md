# Design: add-mcp-and-evaluation

## 1. MCP server（mcp_server/）
用官方 `mcp` SDK（stdio transport）。把 PaperLens 工具层暴露为标准 MCP server，任何 MCP 客户端可发现并调用。

```
mcp_server/
├── __init__.py
├── server.py      # MCP server 定义（register tools）+ run 入口
└── tools.py       # MCP tool schema（search_papers/gather_evidence）+ 执行（复用 agent.tools）
```

```python
# server.py（缝合 mcp SDK 用法）
from mcp.server import Server
from mcp.server.stdio import stdio_server
import mcp.types as types

server = Server("paperlens")

@server.list_tools()
async def list_tools() -> list[types.Tool]:
    return [
        types.Tool(name="search_papers", description="...", inputSchema={...}),
        types.Tool(name="gather_evidence", description="...", inputSchema={...}),
    ]

@server.call_tool()
async def call_tool(name, arguments):
    result = await agent_execute_tool(name, arguments)  # 复用 agent.tools.execute_tool
    return [types.TextContent(type="text", text=result)]

async def main():
    async with stdio_server() as (read, write):
        await server.run(read, write, ...)

if __name__ == "__main__":
    import asyncio; asyncio.run(main())
```

## 2. MCP client（mcp_client/，验证双向）
一个轻量 client，连接本地 MCP server（in-process 或 stdio），列出工具 + 调用 search_papers，确认闭环。
```python
# client.py
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

async def demo():
    params = StdioServerParameters(command="python", args=["-m","mcp_server.server"])
    async with stdio_client(params) as (r,w):
        async with ClientSession(r,w) as session:
            await session.initialize()
            tools = await session.list_tools()
            result = await session.call_tool("search_papers", {"query":"transformer"})
            print(result)
```

## 3. 评测集（eval/）
```
eval/
├── dataset.py      # CS 评测集（10-15题，3类，含 gold paper titles/queries）
├── metrics.py      # Recall@k / faithfulness(LLM-judge) / coverage
├── variants.py     # baseline(朴素搜索) vs paperlens(图谱+RAG)
├── run_eval.py     # 跑对比，产报告
└── reports/        # 历次对比结果
```

### dataset.py（10-15题，3类）
```python
EVAL_ITEMS = [
    EvalItem(id="q01", type="factual", question="Transformer 的核心机制是什么？",
             gold_queries=["attention is all you need"],
             gold_titles={"attention is all you need"}),  # gold paper title 关键词
    EvalItem(id="q02", type="recent", question="2024 年 Mamba 架构有哪些代表工作？",
             gold_queries=["Mamba state space model"], gold_titles={"Mamba","Vision Mamba"}),
    EvalItem(id="q03", type="compare", question="RAG 和微调相比有什么优劣？",
             gold_queries=["retrieval augmented generation vs fine-tuning"], gold_titles={"RAG"}),
    ...  # 共 ~12 题
]
```
gold 用 title 关键词（而非精确 title）做模糊匹配，适应检索结果标题变体。

### metrics.py
- **Recall@k**：检索结果里命中 gold title 关键词的比例（k=10）。
- **faithfulness**：LLM-as-judge 给综述每条论断打"是否有引用支撑"（0/1），取均值。
- **coverage**：LLM-as-judge 评估综述覆盖 gold 主题的比例。

### variants.py
- `baseline_research(question)`：datasources.search 直出 → DeepSeek 摘要（无图谱无 RAG）。
- `paperlens_research(question)`：完整 agent（图谱+RAG）。
- 两者都用同一 DeepSeek 模型。

### run_eval.py
对每题跑两个 variant，算指标，产对比表（Recall@k 均值 / faithfulness / coverage）。**诚实记录**，不粉饰。

## 4. 验证项
- MCP server：`python -m mcp_server.server` 能起，client demo 能列工具 + 调用返回结果。
- 评测：`python -m eval.run_eval` 产出 baseline vs paperlens 对比表。

## 5. 测试
- mcp_server：tool schema 正确性、execute_tool 复用（mock）。
- eval/dataset：gold 匹配逻辑。
- eval/metrics：Recall@k 计算（mock 检索结果）、faithfulness/coverage（mock LLM judge）。
