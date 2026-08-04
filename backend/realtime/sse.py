"""LangGraph astream → SSE 事件映射。

LangGraph 1.x 多 stream_mode 的 chunk 格式可能为 (mode, data) 元组或 {mode, ...} dict。
本模块做容错适配，统一输出 SSE 事件：
- step: 节点完成（updates）
- token: 逐字增量（messages）
- graph: 引用图谱 vis_data（citation_graph 节点的 updates）
"""
from __future__ import annotations

import json
import logging
from typing import Any, AsyncIterator
from urllib.parse import quote

logger = logging.getLogger(__name__)


def _sse(event: str, data: Any) -> bytes:
    """构造一个 SSE 帧。"""
    payload = json.dumps(data, ensure_ascii=False, default=str)
    return f"event: {event}\ndata: {payload}\n\n".encode("utf-8")


async def map_astream_to_sse(astream: AsyncIterator) -> AsyncIterator[bytes]:
    """消费 LangGraph astream，逐个产出 SSE 帧。

    astream 的 chunk 在多 stream_mode 下通常是 (stream_mode_name, chunk_data) 元组。
    本函数容错处理元组和 dict 两种形态。
    """
    async for raw in astream:
        # 解析 (mode, data) 或 {"mode":..., "data":...}
        if isinstance(raw, tuple) and len(raw) == 2:
            mode, data = raw
        elif isinstance(raw, dict):
            mode = raw.get("mode") or raw.get("type")
            data = raw.get("data", raw)
        else:
            # 单模式直接是 data，无法判定 mode，跳过
            continue

        if mode == "updates":
            async for frame in _handle_updates(data):
                yield frame
        elif mode == "messages":
            async for frame in _handle_messages(data):
                yield frame
        else:
            logger.debug("skip astream mode %s", mode, extra={"event": "sse_mode_skipped"})
        # 其他 mode（values/debug）暂不映射


async def _handle_updates(data: Any) -> AsyncIterator[bytes]:
    """updates 模式：data 是 {node_name: state_delta}。"""
    if not isinstance(data, dict):
        return
    for node, delta in data.items():
        if not isinstance(delta, dict):
            yield _sse("step", {"node": node})
            continue
        # citation_graph 节点产出 vis_data → 发 graph 事件
        if node == "citation_graph" and "citation_graph" in delta:
            cg = delta["citation_graph"]
            if isinstance(cg, dict) and cg.get("vis"):
                yield _sse("graph", cg["vis"])
            yield _sse("step", {"node": "citation_graph", "done": True})
        # synthesizer 产出 final_report → 发 step（综述通过 token 流式，这里只标记完成）
        elif node == "synthesizer" and "final_report" in delta:
            yield _sse("step", {"node": "synthesizer", "done": True})
        elif node == "planner" and "plan" in delta:
            yield _sse("step", {"node": "planner", "plan": delta["plan"], "done": True})
        elif node == "fan_out_researchers":
            notes_n = len(delta.get("notes", []))
            sources_n = len(delta.get("sources", []))
            yield _sse("step", {"node": "fan_out_researchers", "notes": notes_n, "sources": sources_n, "done": True})
        else:
            yield _sse("step", {"node": node})


async def _handle_messages(data: Any) -> AsyncIterator[bytes]:
    """messages 模式：data 是 (message, metadata) 或 AIMessageChunk。"""
    # 容错：可能是 (msg, meta) 元组，或直接是 message 对象
    msg = data[0] if isinstance(data, (tuple, list)) and data else data
    meta = data[1] if isinstance(data, (tuple, list)) and len(data) > 1 else {}

    content = getattr(msg, "content", None)
    if content is None and isinstance(msg, dict):
        content = msg.get("content")
    if isinstance(content, list):
        # 部分 provider 返回 [{"type":"text","text":"..."}]
        content = "".join(c.get("text", "") if isinstance(c, dict) else str(c) for c in content)
    if content:
        yield _sse("token", {"text": content})
