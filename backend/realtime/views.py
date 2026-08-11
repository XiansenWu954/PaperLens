"""SSE 流式端点：实时推送 agent 工作过程。

GET /api/research/<id>/stream
- 桥接 LangGraph astream（updates/messages）→ SSE 事件
- 累积 updates delta 成最终状态，agent 完成后持久化到 ResearchTask
- 关键头：text/event-stream + Cache-Control:no-cache + X-Accel-Buffering:no
"""
from __future__ import annotations

import asyncio
import logging
import time

from asgiref.sync import sync_to_async
from django.http import StreamingHttpResponse

from api.models import ResearchTask
from config.logging_context import (
    reset_request_id,
    reset_task_id,
    set_request_id,
    set_task_id,
)
from realtime.sse import _sse, map_astream_to_sse

logger = logging.getLogger(__name__)


def _merge_delta(state: dict, node: str, delta: dict) -> None:
    """把一个 updates delta 合并进累积状态（list 追加，标量覆盖）。"""
    if not isinstance(delta, dict):
        return
    for k, v in delta.items():
        if isinstance(v, list) and isinstance(state.get(k), list):
            state[k] = state[k] + v
        else:
            state[k] = v


async def research_stream(request, task_id: int):
    """SSE 端点：运行 agent 并流式推送。"""
    request_id = getattr(request, "paperlens_request_id", "")
    task_token = set_task_id(task_id)
    try:
        task = await sync_to_async(ResearchTask.objects.get)(id=task_id)
    except ResearchTask.DoesNotExist:
        logger.info(
            "SSE task not found",
            extra={"event": "sse_task_not_found", "task_id": task_id, "status": 404},
        )
        resp = StreamingHttpResponse(
            iter([_sse("error", {"message": "任务不存在"})]), content_type="text/event-stream"
        )
        resp["Cache-Control"] = "no-cache"
        resp["X-Accel-Buffering"] = "no"
        resp["X-Request-ID"] = request_id
        reset_task_id(task_token)
        return resp

    task.status = "running"
    await task.asave()
    logger.info(
        "SSE connected",
        extra={"event": "sse_connected", "task_id": task.id, "task_status": task.status},
    )

    async def event_stream():
        stream_request_token = set_request_id(request_id)
        stream_task_token = set_task_id(task.id)
        started = time.perf_counter()
        try:
            yield b": connected\n\n"
            final_state: dict = {"question": task.question}
            from agent.config import DEFAULT_CONFIG
            from agent.graph import build_graph

            logger.info(
                "agent stream started",
                extra={"event": "agent_stream_started", "task_id": task.id},
            )
            graph = build_graph(DEFAULT_CONFIG)
            astream = graph.astream(
                {"question": task.question},
                stream_mode=["updates", "messages"],
            )
            # 同时流式输出 + 累积 updates delta
            async for mode, data in astream:
                if mode == "updates" and isinstance(data, dict):
                    for node, delta in data.items():
                        _merge_delta(final_state, node, delta)
                # 映射到 SSE
                async for frame in map_astream_to_sse(iter_async([(mode, data)])):
                    yield frame
            await _persist(task, final_state)
            logger.info(
                "agent stream completed",
                extra={
                    "event": "agent_stream_completed",
                    "task_id": task.id,
                    "duration_ms": round((time.perf_counter() - started) * 1000, 2),
                    "sources": len(final_state.get("sources", [])),
                    "notes": len(final_state.get("notes", [])),
                },
            )
            yield _sse("done", {"task_id": task.id})
        except asyncio.CancelledError:
            logger.info(
                "SSE client disconnected",
                extra={
                    "event": "sse_client_disconnected",
                    "task_id": task_id,
                    "duration_ms": round((time.perf_counter() - started) * 1000, 2),
                },
            )
            raise
        except Exception as e:
            # §30.3: no logger.exception (raw exception message).
            from agent.events import error_hash

            logger.error(
                "agent stream failed",
                extra={
                    "event": "agent_stream_failed",
                    "task_id": task_id,
                    "duration_ms": round((time.perf_counter() - started) * 1000, 2),
                    "error": e.__class__.__name__,
                    "error_hash": error_hash(e),
                },
            )
            # §32.1: ResearchTask.error_message stores the stable code only —
            # the raw exception message is never persisted.
            await _mark_error(task, f"{e.__class__.__name__}: research task failed")
            # §31.1: the SSE error surface carries the stable code + fixed
            # copy — the raw exception message is never serialized.
            yield _sse("error", {
                "message": "服务暂时不可用，请稍后重试。",
                "error": e.__class__.__name__,
            })
        finally:
            reset_task_id(stream_task_token)
            reset_request_id(stream_request_token)

    resp = StreamingHttpResponse(event_stream(), content_type="text/event-stream")
    resp["Cache-Control"] = "no-cache"
    resp["X-Accel-Buffering"] = "no"
    resp["Connection"] = "keep-alive"
    resp["X-Request-ID"] = request_id
    reset_task_id(task_token)
    return resp


async def iter_async(items):
    """把一个同步列表包成 async iterator。"""
    for it in items:
        yield it


async def _persist(task: ResearchTask, state: dict) -> None:
    """把 agent 最终状态存入 ResearchTask。"""
    task.status = "done"
    task.final_report = state.get("final_report", "")
    task.citation_graph = (state.get("citation_graph") or {}).get("vis", {})
    task.sources = state.get("sources", [])
    task.notes = state.get("notes", [])
    await task.asave()
    logger.info(
        "research task persisted",
        extra={
            "event": "research_persisted",
            "task_id": task.id,
            "report_chars": len(task.final_report),
            "graph_nodes": len((task.citation_graph or {}).get("nodes", [])),
            "sources": len(task.sources or []),
            "notes": len(task.notes or []),
            "task_status": task.status,
        },
    )


async def _mark_error(task: ResearchTask, msg: str) -> None:
    task.status = "error"
    task.error_message = msg[:1000]
    await task.asave()
    logger.info(
        "research task marked error",
        extra={
            "event": "research_marked_error",
            "task_id": task.id,
            "task_status": task.status,
        },
    )
