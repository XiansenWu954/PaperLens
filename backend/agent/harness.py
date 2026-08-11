"""Project Agent execution harness.

The harness owns tool routing, events, logging, and stream shape. It keeps
views thin and makes Function Calling/Prompt Engineering boundaries explicit.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import time
from collections.abc import AsyncIterator
from typing import Any

from asgiref.sync import sync_to_async

from .context import create_context
from .events import redact_text
from .intent import ProjectIntent, classify_project_intent
from .prompts import LIVE_PROJECT_CHAT_SYSTEM
from .project_tools import execute_project_tool

logger = logging.getLogger(__name__)

# §21.4: citation binding uses ONLY explicit [cite:<marker>] tokens — plain
# substring matches in natural language never bind a citation.
_CITE_TOKEN_RE = re.compile(r"\[cite:\s*([^\]]+?)\s*\]", re.IGNORECASE)


def extract_citation_markers(answer: str) -> list[str]:
    """Extract every explicit [cite:<marker>] token, trimmed and lowercased."""
    if not answer:
        return []
    return [token.strip().lower() for token in _CITE_TOKEN_RE.findall(answer)]


class ProjectAgentHarness:
    """Deterministic first-pass harness with replaceable LLM router."""

    def __init__(
        self,
        project_id: int,
        session_id: int | None = None,
        tool_executor=None,
        tool_timeout_seconds: float = 15.0,
        use_llm: bool | None = None,
        llm_client_factory=None,
        llm_timeout_seconds: float = 30.0,
        raw_answer_callback=None,
    ) -> None:
        self.project_id = project_id
        self.session_id = session_id
        self.tool_executor = tool_executor or execute_project_tool
        self.tool_timeout_seconds = tool_timeout_seconds
        # Task 2.1: frozen server-created identity. stream() refreshes it with
        # the run/session ids once the run exists; standalone _quality_check
        # calls use the project-only context.
        self._context = create_context(project_id)
        # Tasks 5.x (§28.1): the raw model answer is delivered ONLY to this
        # explicit eval hook — it is never streamed, persisted or logged.
        self.raw_answer_callback = raw_answer_callback
        # Honor the settings-level PROJECT_CHAT_LIVE_LLM (which is forced off in
        # test mode) rather than reading the raw env — otherwise a .env with
        # PAPERLENS_PROJECT_CHAT_LIVE_LLM=1 makes deterministic tests burn real quota.
        from django.conf import settings as _settings
        self.use_llm = use_llm if use_llm is not None else bool(getattr(_settings, "PROJECT_CHAT_LIVE_LLM", False))
        self.llm_client_factory = llm_client_factory
        self.llm_timeout_seconds = llm_timeout_seconds

    async def run(self, message: str) -> dict[str, Any]:
        events: list[dict[str, Any]] = []
        answer_parts: list[str] = []
        async for event in self.stream(message):
            events.append(event)
            if event["event"] == "token":
                answer_parts.append(event["data"]["text"])
        return {"answer": "".join(answer_parts), "events": events}

    async def stream(self, message: str) -> AsyncIterator[dict[str, Any]]:
        from api.models import ChatMessage, ChatSession, ProjectRun, ProjectRunEvent, ResearchProject

        project = await sync_to_async(ResearchProject.objects.get)(id=self.project_id)
        if self.session_id:
            session = await sync_to_async(ChatSession.objects.get)(id=self.session_id, project=project)
        else:
            session = await sync_to_async(ChatSession.objects.create)(
                project=project, title=message[:80]
            )
            self.session_id = session.id

        run = await sync_to_async(ProjectRun.objects.create)(
            project=project, kind="chat", status="running", question=message
        )
        # Task 2.1: frozen server-created execution context for every tool call.
        self._context = create_context(
            self.project_id,
            run_id=run.id,
            session_id=session.id,
            actor="user",
        )
        started = time.perf_counter()
        history = await sync_to_async(self._load_recent_history)(session.id)
        await sync_to_async(ChatMessage.objects.create)(
            session=session, role="user", content=message
        )

        logger.info(
            "project chat harness started",
            extra={
                "event": "project_agent_run_started",
                "project_id": self.project_id,
                "run_id": run.id,
                "session_id": session.id,
                # §30.3: never log the user message verbatim — only length/hash.
                "message_chars": len(message),
                "message_hash": hashlib.sha256(message.encode("utf-8")).hexdigest()[:12],
                "status": "running",
            },
        )

        async def emit(event_type: str, payload: dict[str, Any],
                       persist: bool | None = None) -> dict[str, Any]:
            # Tasks 5.x (§30.1): the unified EventPublisher serializes every
            # payload (recursive schema) and persists ProjectRunEvent — raw
            # contexts never reach SSE / API response / DB events.
            from .event_publisher import EventPublisher

            publisher = EventPublisher(
                run=run,
                session_id=self._context.session_id,
                request_id=self._context.request_id,
            )
            return await sync_to_async(publisher.publish)(
                event_type, payload, persist=persist)

        yield await emit("harness_started", {"session_id": session.id, "run_id": run.id})

        try:
            intent = classify_project_intent(message, self.project_id)
            logger.info(
                "project agent intent detected",
                extra={
                    "event": "project_agent_intent_detected",
                    "project_id": self.project_id,
                    "intent": intent.name,
                    "planned_tools": intent.tool_names,
                },
            )
            yield await emit(
                "intent_detected",
                {
                    "intent": intent.name,
                    "rationale": intent.rationale,
                    "blocked": intent.blocked,
                    "planned_tools": intent.tool_names,
                },
            )
            context = {}
            if intent.blocked:
                answer = self._compose_blocked_answer(intent)
                context["answer_mode"] = "blocked"
            elif self.use_llm:
                # ReAct 模式:LLM 自主决策工具(主路径)
                async for loop_event in self._run_chat_agent_loop(message, history, emit):
                    if loop_event["event"] == "final_answer_raw":
                        answer = loop_event["data"]["answer"]
                        context = loop_event["data"]["context"]
                        context["answer_mode"] = "react"
                        # Tasks 5.x (§28.1): the raw model answer is delivered
                        # ONLY to the explicit eval hook — it must never be
                        # streamed/persisted before or after the safety gate.
                        if self.raw_answer_callback is not None:
                            self.raw_answer_callback(answer)
                    elif loop_event["event"] in {"tool_call", "tool_result", "search_results", "evidence", "paper_added", "graph", "agent_mode", "llm_call", "llm_result", "tool_scope_violation"}:
                        yield await emit(loop_event["event"], loop_event["data"])
            else:
                async for tool_event in self._execute_plan(intent, context):
                    if tool_event["event"] in {"tool_call", "tool_result", "search_results", "evidence", "paper_added", "graph"}:
                        yield await emit(tool_event["event"], tool_event["data"])
                    else:
                        yield tool_event
                answer = self._compose_answer(message, intent, context)
                context.setdefault("answer_mode", "deterministic")

            # B3 (deepseek-live-evaluation-plan §3.4): keep the model's raw answer
            # separate from the postprocessed one. _ensure_source_markers only
            # appends a "证据依据" block as a UX fallback; it must NOT be allowed
            # to make an uncited model answer look grounded. `model_cited` records
            # whether the model itself cited any evidence marker.
            raw_model_answer = answer

            # Task 4.x (capability evidence policy): the minimum evidence is
            # decided by the STRUCTURED capability contract derived from the
            # intent — never by answer keywords/length or called tools. When the
            # policy is not met the answer FAILS CLOSED to a standard abstention
            # and no citations / "证据依据" block may be appended afterwards.
            from .capability import capability_for_intent

            contract = capability_for_intent(intent)
            quality = await self._quality_check(answer, intent, context)
            safety_replaced = False
            action_failed = False
            if (contract.requires_resolved_bound_fulltext
                    and quality.get("answer_mode") == "abstained"
                    and not intent.blocked):
                answer = self._compose_abstention(quality, intent, context)
                safety_replaced = True
                quality = await self._quality_check(answer, intent, context)
            elif quality.get("answer_mode") == "action_failed" and not intent.blocked:
                # §24.3: a tool error must not be presented as success.
                answer = self._compose_action_failure(quality)
                action_failed = True
                quality = await self._quality_check(answer, intent, context)
            else:
                answer = self._ensure_source_markers(answer, context)
                quality = await self._quality_check(answer, intent, context)
            quality["raw_model_answer_chars"] = len(raw_model_answer)
            quality["model_cited"] = self._answer_has_any_marker(raw_model_answer, context)
            quality["postprocessed_added_markers"] = len(answer) > len(raw_model_answer)
            quality["raw_model_answer"] = raw_model_answer  # E1: expose raw for runner
            quality["safety_replaced"] = safety_replaced  # fail-closed flag
            quality["action_failed"] = action_failed
            yield await emit("quality_check", quality)

            # §30.2: the supported final answer is user-visible product content,
            # but secret patterns are redacted BEFORE token events, ChatMessage
            # assistant content, run.output and API responses.
            answer = redact_text(answer)

            for chunk in self._chunks(answer):
                # §30.1: token events go through the public serializer too —
                # schema + correlation ids + redaction; they are not persisted.
                yield await emit("token", {"text": chunk}, persist=False)

            await sync_to_async(ChatMessage.objects.create)(
                session=session,
                role="assistant",
                content=answer,
                metadata={"run_id": run.id},
            )
            run.status = "done"
            run.output = answer
            await sync_to_async(run.save)()
            logger.info(
                "project chat harness completed",
                extra={
                    "event": "project_agent_run_completed",
                    "project_id": self.project_id,
                    "run_id": run.id,
                    "session_id": session.id,
                    "intent": intent.name,
                    "tool_count": len(intent.tool_names),
                    "answer_chars": len(answer),
                    "duration_ms": round((time.perf_counter() - started) * 1000, 2),
                    "status": "done",
                },
            )
            yield await emit("done", {"session_id": session.id, "run_id": run.id})
        except Exception as exc:
            # §30.3: never format the raw exception message into logs/events/
            # run.error_message — record exception type, safe stack frames, a
            # digest and correlation ids.
            from .events import error_hash, safe_stack_frames

            logger.error(
                "project chat harness failed",
                extra={
                    "event": "project_agent_run_failed",
                    "project_id": self.project_id,
                    "run_id": run.id,
                    "session_id": session.id,
                    "message_chars": len(message),
                    "message_hash": hashlib.sha256(message.encode("utf-8")).hexdigest()[:12],
                    "duration_ms": round((time.perf_counter() - started) * 1000, 2),
                    "status": "error",
                    "error": exc.__class__.__name__,
                    "error_hash": error_hash(exc),
                    "stack_frames": safe_stack_frames(exc),
                },
            )
            run.status = "error"
            run.error_message = f"{exc.__class__.__name__}: chat execution failed"
            await sync_to_async(run.save)()
            # §31.1: the SSE/API error surface carries ONLY the stable error
            # code + fixed user copy — the raw exception message is never
            # serialized (no regex-based redaction).
            yield await emit("error", {
                "error": exc.__class__.__name__,
                "error_hash": error_hash(exc),
            })

    async def _run_chat_agent_loop(self, message: str, history: list | None, emit) -> AsyncIterator[dict[str, Any]]:
        """运行 ReAct 模式 ChatAgentLoop，透传其事件。"""
        from .chat_loop import ChatAgentLoop

        loop = ChatAgentLoop(
            self.project_id,
            context=self._context,
            tool_executor=self.tool_executor,
        )
        async for event in loop.run(message, history):
            yield event

    async def _execute_plan(self, intent: ProjectIntent, context: dict[str, Any]) -> AsyncIterator[dict[str, Any]]:
        from .project_tools import strip_auth_fields
        from .validation import validate_tool_arguments

        for step in intent.tool_plan:
            args = strip_auth_fields(dict(step.arguments))
            if step.name == "add_papers_to_project":
                args["papers"] = context.get("search_papers", {}).get("papers", [])
            yield {
                "event": "tool_call",
                "data": {"name": step.name, "arguments": self._safe_args(args), "summary": step.summary},
            }
            logger.info(
                "project agent tool call",
                extra={
                    "event": "project_agent_tool_call",
                    "project_id": self.project_id,
                    "tool_name": step.name,
                },
            )
            # §25.1: production schema validation before execution; invalid
            # args never run and never enter __args_<tool>.
            validated_args, validation_error = validate_tool_arguments(step.name, args)
            if validation_error is not None:
                result = {
                    "error": validation_error["error"],
                    "message": validation_error["message"],
                    "field": validation_error.get("field", ""),
                }
                context[step.name] = result
                summary = self._tool_summary(step.name, result)
                yield {"event": "tool_result", "data": {"name": step.name, **summary}}
                logger.warning(
                    "project agent tool validation failed",
                    extra={
                        "event": "project_agent_tool_validation_failed",
                        "project_id": self.project_id,
                        "tool_name": step.name,
                        "error": validation_error["error"],
                    },
                )
                continue
            args = validated_args
            try:
                result = await asyncio.wait_for(
                    self.tool_executor(self._context, step.name, args),
                    timeout=self.tool_timeout_seconds,
                )
            except TimeoutError:
                result = {
                    "error": "tool_timeout",
                    "message": f"{step.name} exceeded {self.tool_timeout_seconds:g}s",
                    "recoverable": True,
                }
                logger.warning(
                    "project agent tool timed out",
                    extra={
                        "event": "project_agent_tool_timeout",
                        "project_id": self.project_id,
                        "tool_name": step.name,
                        "duration_ms": int(self.tool_timeout_seconds * 1000),
                        "status": "timeout",
                    },
                )
            context[step.name] = result
            # §24.2: keep the deterministic plan's validated arguments so
            # capability policy obligations come from the actual request.
            context.setdefault(f"__args_{step.name}", []).append(dict(args))
            summary = self._tool_summary(step.name, result)
            logger.info(
                "project agent tool result",
                extra={
                    "event": "project_agent_tool_result",
                    "project_id": self.project_id,
                    "tool_name": step.name,
                    **summary,
                },
            )
            yield {"event": "tool_result", "data": {"name": step.name, **summary}}
            if step.name == "query_project_rag":
                yield {"event": "evidence", "data": result}
            elif step.name == "search_papers":
                yield {"event": "search_results", "data": self._search_results_payload(result)}
            elif step.name == "add_papers_to_project":
                yield {"event": "paper_added", "data": result}
            elif step.name == "get_project_citation_graph":
                yield {"event": "graph", "data": result.get("graph", {"nodes": [], "edges": []})}

    def _load_recent_history(self, session_id: int) -> list[dict[str, str]]:
        from api.models import ChatMessage

        rows = list(
            ChatMessage.objects.filter(session_id=session_id)
            .order_by("-created_at", "-id")
            .values("role", "content")[:8]
        )
        rows.reverse()
        return [
            {
                "role": str(row["role"]),
                "content": str(row["content"] or "")[:1200],
            }
            for row in rows
        ]

    async def _compose_live_answer(
        self,
        message: str,
        intent: ProjectIntent,
        context: dict[str, Any],
        history: list[dict[str, str]],
        draft_answer: str,
    ) -> tuple[str, dict[str, Any]]:
        started = time.perf_counter()
        try:
            answer, usage = await asyncio.wait_for(
                sync_to_async(
                    self._call_live_answer_model,
                    thread_sensitive=False,
                )(message, intent, context, history, draft_answer),
                timeout=self.llm_timeout_seconds,
            )
            context["answer_mode"] = "live_llm"
            context["llm_usage"] = usage
            return answer, {
                "status": "ok",
                "provider": "deepseek",
                "usage": usage,
                "answer_chars": len(answer),
                "duration_ms": round((time.perf_counter() - started) * 1000, 2),
            }
        except Exception as exc:
            context["answer_mode"] = "deterministic_fallback"
            context["llm_error"] = exc.__class__.__name__
            logger.warning(
                "project chat live answer failed; falling back to deterministic answer",
                extra={
                    "event": "project_agent_llm_failed",
                    "project_id": self.project_id,
                    "error": exc.__class__.__name__,
                    "duration_ms": round((time.perf_counter() - started) * 1000, 2),
                    "status": "fallback",
                },
            )
            return draft_answer, {
                "status": "fallback",
                "provider": "deepseek",
                "error": exc.__class__.__name__,
                "answer_chars": len(draft_answer),
                "duration_ms": round((time.perf_counter() - started) * 1000, 2),
            }

    def _call_live_answer_model(
        self,
        message: str,
        intent: ProjectIntent,
        context: dict[str, Any],
        history: list[dict[str, str]],
        draft_answer: str,
    ) -> tuple[str, dict[str, Any]]:
        from llm.deepseek import DeepSeekClient

        client = self.llm_client_factory() if self.llm_client_factory else DeepSeekClient(max_retries=1)
        payload = self._live_answer_payload(message, intent, context, history, draft_answer)
        response = client.complete(
            [
                {"role": "system", "content": LIVE_PROJECT_CHAT_SYSTEM},
                {
                    "role": "user",
                    "content": (
                        "Compose the final project chat answer from this JSON payload. "
                        "Return only the user-facing answer.\n\n"
                        f"{json.dumps(payload, ensure_ascii=False)}"
                    ),
                },
            ],
            thinking=False,
            temperature=0.2,
            max_tokens=1200,
        )
        answer = str(response.get("content") or "").strip()
        if not answer:
            raise RuntimeError("empty_llm_answer")
        return answer, response.get("usage") or {}

    def _live_answer_payload(
        self,
        message: str,
        intent: ProjectIntent,
        context: dict[str, Any],
        history: list[dict[str, str]],
        draft_answer: str,
    ) -> dict[str, Any]:
        return {
            "project_id": self.project_id,
            "user_message": message[:1200],
            "intent": {
                "name": intent.name,
                "rationale": intent.rationale,
                "blocked": intent.blocked,
                "planned_tools": intent.tool_names,
            },
            "conversation_history": history[-6:],
            "evidence": self._collect_evidence(context)[:8],
            "tool_results": self._safe_context(context),
            "deterministic_draft": draft_answer[:2400],
            "answer_requirements": [
                "answer the user directly",
                "use project evidence first",
                "copy exact source_marker values from evidence in parentheses",
                "include a source marker in every evidence-backed paragraph or bullet",
                "mention weak evidence when evidence is metadata-only or absent",
                "for limitations/gaps, separate supported facts from hypotheses to verify",
                "avoid generic feature descriptions",
            ],
        }

    def _safe_context(self, context: dict[str, Any]) -> dict[str, Any]:
        safe: dict[str, Any] = {}
        for name, result in context.items():
            if not isinstance(result, dict):
                continue
            if name == "search_papers":
                safe[name] = {
                    "count": result.get("count", 0),
                    "papers": [
                        {
                            "title": item.get("title"),
                            "year": item.get("year"),
                            "source": item.get("source"),
                            "venue": item.get("venue"),
                        }
                        for item in (result.get("papers") or [])[:6]
                    ],
                }
            elif name == "add_papers_to_project":
                safe[name] = {
                    "count": result.get("count", 0),
                    "added": (result.get("added") or [])[:6],
                }
            elif name == "query_project_rag":
                safe[name] = {
                    "evidence_count": len(result.get("evidence") or []),
                    "fallback": result.get("fallback", ""),
                }
            elif name == "get_project_citation_graph":
                graph = result.get("graph") or {}
                graph_summary = self._graph_summary(graph)
                safe[name] = {
                    "nodes": len(graph.get("nodes") or []),
                    "edges": len(graph.get("edges") or []),
                    "top_nodes": graph_summary["top_nodes"],
                    "top_edges": graph_summary["top_edges"],
                }
            elif name == "draft_report_section":
                safe[name] = {
                    "section_chars": len(result.get("section") or ""),
                    "evidence_count": len(result.get("evidence") or []),
                }
            else:
                safe[name] = {"status": result.get("status", "ok")}
            if result.get("error"):
                safe[name]["error"] = result.get("error")
                safe[name]["message"] = str(result.get("message") or "")[:240]
        return safe

    def _ensure_source_markers(self, answer: str, context: dict[str, Any]) -> str:
        evidence = self._collect_evidence(context)
        if not evidence:
            return answer
        markers = self._evidence_markers(evidence)
        # §21.4: only an explicit [cite:<marker>] token counts as a binding.
        marker_set = {m.lower() for m in markers if m}
        if set(extract_citation_markers(answer)) & marker_set:
            return answer

        source_lines = []
        for item in evidence[:4]:
            marker = self._source_marker(item)
            if not marker:
                continue
            summary = str(item.get("summary") or "").strip()
            if len(summary) > 220:
                summary = summary[:220].rstrip() + "..."
            source_lines.append(f"- ({marker}) {summary}")
        if not source_lines:
            return answer
        return answer.rstrip() + "\n\n证据依据：\n" + "\n".join(source_lines)

    def _compose_answer(self, message: str, intent: ProjectIntent, context: dict[str, Any]) -> str:
        if intent.name == "library":
            return self._compose_library_answer(context.get("list_project_papers", {}))
        if "graph" in intent.name and "get_project_citation_graph" in context and "report" not in intent.name:
            return self._compose_graph_answer(context.get("get_project_citation_graph", {}))
        if "report" in intent.name:
            section = context.get("draft_report_section", {})
            return section.get("section", "") or "没有足够项目证据生成报告章节。"
        if "search_add" in intent.name:
            return self._compose_search_answer(message, context)

        rag_result = context.get("query_project_rag", {})
        evidence = rag_result.get("evidence") or []
        if not evidence:
            return (
                "当前项目库还没有足够证据回答这个问题。"
                "你可以让我继续检索 DBLP/OpenAlex/arXiv 并把论文加入项目库。"
            )
        lines = ["基于当前项目库，可以先得到这个结论：", ""]
        for item in evidence[:6]:
            marker = item.get("citation") or item.get("title") or item.get("docname") or f"paper {item.get('paper_id')}"
            title = item.get("title") or item.get("docname") or f"paper {item.get('paper_id')}"
            lines.append(f"- {title}: {item.get('summary', '')} [cite:{marker}]")
        fallback = rag_result.get("fallback")
        if fallback:
            lines.append("")
            lines.append(f"注：{fallback}")
        search = context.get("search_papers") or {}
        if search.get("papers"):
            lines.append("")
            lines.append("可继续检索的候选方向：")
            for paper in (search.get("papers") or [])[:5]:
                year = paper.get("year") or "n.d."
                source = paper.get("source") or "source"
                lines.append(f"- {paper.get('title')} ({year}, {source})")
        return "\n".join(lines)

    def _should_use_deterministic_answer(self, intent: ProjectIntent) -> bool:
        return "graph" in intent.name and "report" not in intent.name

    def _compose_blocked_answer(self, intent: ProjectIntent) -> str:
        return (
            "这个请求涉及删除、清空或覆盖等破坏性操作，项目 Agent 不会自主执行。\n\n"
            "你可以在 Evidence Board 中显式移出单篇论文，或通过受控 API 操作项目。"
        )

    def _compose_library_answer(self, result: dict[str, Any]) -> str:
        papers = result.get("papers") or []
        if not papers:
            return "当前项目论文库为空。你可以让我检索 DBLP/OpenAlex/arXiv 并把论文加入项目库。"
        lines = [f"当前项目库有 {len(papers)} 篇论文：", ""]
        for paper in papers[:12]:
            year = paper.get("year") or "n.d."
            venue = paper.get("venue") or "Unknown venue"
            lines.append(f"- {paper.get('title')} ({year}, {venue}) · {paper.get('status')}")
        return "\n".join(lines)

    def _compose_graph_answer(self, result: dict[str, Any]) -> str:
        graph = result.get("graph") or {}
        summary = self._graph_summary(graph)
        nodes = summary["node_count"]
        edges = summary["edge_count"]
        if not nodes:
            return "当前项目还没有足够引用关系构建 Citation Map。继续补充带引用元数据的论文后会更有价值。"
        lines = [
            f"已基于项目内 {nodes} 篇论文的引用关系构建 Citation Map（{edges} 条共参考关系）。"
            "下面按该先读哪些、怎么分组、谁和谁关联最紧三个角度帮你规划阅读。",
            "",
        ]
        # 1. 推荐先读：按影响力排序，说明为何相关
        if summary["top_nodes"]:
            lines.append("推荐先读（按影响力排序）：")
            for node in summary["top_nodes"][:6]:
                year = node.get("year") or "n.d."
                cites = node.get("citation_count") or 0
                roles = []
                if node.get("is_root"):
                    roles.append("奠基性")
                if node.get("is_frontier"):
                    roles.append("前沿")
                role_text = f"（{'、'.join(roles)}，{cites} 次引用）" if roles or cites else f"（{cites} 次引用）"
                lines.append(f"- {node.get('title')} ({year}){role_text}")
            lines.append("")
        # 2. 主题分组：告诉用户论文怎么分类
        if len(summary.get("topic_groups") or []) > 1:
            lines.append("主题分组：")
            for group in (summary.get("topic_groups") or [])[:5]:
                lines.append(
                    f"- {group.get('label')}：{group.get('count')} 篇（代表：{group.get('best_title', '')[:50]}）"
                )
            lines.append("")
        # 3. 核心关系：谁和谁关联最紧 + 为什么
        if summary["top_edges"]:
            lines.append("关联最紧密的论文对（共享参考文献最多）：")
            for edge in summary["top_edges"][:4]:
                lines.append(
                    f"- {edge.get('source_title')} ↔ {edge.get('target_title')}（共参考权重 {edge.get('weight')}）"
                )
            lines.append("")
            lines.append("提示：在 Citation Map 中选两篇论文可以查看它们之间的引用连接路径。")
        elif nodes:
            lines.append("当前论文之间暂未形成明显共参考关系，建议补充更多带引用元数据的论文。")
        return "\n".join(lines)

    def _compose_search_answer(self, message: str, context: dict[str, Any]) -> str:
        search = context.get("search_papers", {})
        add = context.get("add_papers_to_project", {})
        rag = context.get("query_project_rag", {})
        if search.get("error") == "tool_timeout":
            return (
                "外部论文检索这次超时了，我没有继续等待。\n\n"
                "已保留当前项目库内容，你可以缩小检索关键词，或直接基于现有项目库继续问。"
            )
        lines = [
            "已完成一轮检索和项目入库。",
            f"- 检索结果：{search.get('count', 0)} 篇",
            f"- 加入/更新项目库：{add.get('count', 0)} 篇",
        ]
        added = add.get("added") or []
        for item in added[:5]:
            marker = "新增" if item.get("created") else "已存在"
            lines.append(f"- {marker}: {item.get('title')}")
        evidence = rag.get("evidence") or []
        if evidence:
            lines.append("")
            lines.append("基于当前项目库的初步证据：")
            for item in evidence[:3]:
                title = item.get("title") or item.get("docname") or f"paper {item.get('paper_id')}"
                marker = self._source_marker(item) or title
                # §21.4: citations use the explicit [cite:<marker>] token form.
                lines.append(f"- {title}: {item.get('summary', '')} [cite:{marker}]")
        return "\n".join(lines)

    async def _quality_check(self, answer: str, intent: ProjectIntent, context: dict[str, Any]) -> dict[str, Any]:
        evidence = self._collect_evidence(context)
        lowered_answer = answer.lower()

        # Task 3.3/3.5: reference resolution is decided by the DATABASE-backed
        # CitationResolver (project scope + membership + chunk id + content
        # hash + active index version). The Harness no longer self-asserts
        # resolution from fields or marker presence. Legacy/non-envelope items
        # stay `legacy_unresolved` and can never be promoted.
        from .citations import CitationResolver
        from .evidence import evidence_identity_key
        from asgiref.sync import sync_to_async

        resolver = CitationResolver(self._context.project_id)
        resolutions = await sync_to_async(resolver.resolve)(evidence)

        seen_ids: set[str] = set()
        citations: list[dict[str, Any]] = []
        # §21.4: citation binding uses ONLY explicit [cite:<marker>] tokens,
        # shared with `_answer_has_any_marker` — never substring matching.
        cited_token_set: set[str] = set(extract_citation_markers(answer))
        for item in evidence:
            marker = self._source_marker(item)
            if not marker:
                continue
            # Resolution identity is the typed evidence id — the same marker
            # may appear on multiple candidates and ALL of them are retained
            # (§20.3, never first-wins).
            identity_key = evidence_identity_key(item)
            if identity_key in seen_ids:
                continue
            seen_ids.add(identity_key)
            marker_present = marker.lower() in cited_token_set
            resolution = resolutions.get(identity_key)
            reference_resolved = bool(resolution and resolution.reference_resolved)
            reason_code = resolution.reason_code if resolution else "no_binding"
            is_fulltext = item.get("evidence_type") == "fulltext" and not item.get("__legacy_unresolved")
            citations.append({
                "marker": marker,
                # dimension 1: marker in answer text
                "citation_marker_status": "present" if marker_present else "absent",
                # dimension 2: database-verified resolution (CitationResolver)
                "reference_resolved": reference_resolved,
                "reference_resolution_status": "resolved" if reference_resolved else "unresolved",
                "resolution_reason": reason_code,
                "project_id": self._context.project_id,
                "paper_id": item.get("paper_id"),
                "chunk_id": item.get("chunk_id"),
                "content_hash": item.get("content_hash"),
                "evidence_id": item.get("evidence_id"),
                "chunk_index": item.get("chunk_index"),
                "page_start": item.get("page_start"),
                "page_end": item.get("page_end"),
                "section": item.get("section") or "",
                "evidence_type": item.get("evidence_type", "unknown"),
                # dimension 3: claim support — Judge-only, initial pending
                "claim_support_status": "pending" if (marker_present and reference_resolved and is_fulltext) else "not_required",
                # Legacy compat (deprecated — never used as a gate; marker
                # presence only, NOT database-verified, per §20.5)
                "verified": marker_present,
                "summary": (item.get("summary") or "")[:120],
            })
        cited_markers = [c["marker"] for c in citations if c["citation_marker_status"] == "present"]
        resolved_citations = [c for c in citations if c["reference_resolved"] and c["citation_marker_status"] == "present"]
        # C2: tool_errors now iterates list context values (P1 fix — context[tool]
        # is a list of per-call results; the old isinstance(dict) guard skipped them).
        tool_errors: list[dict[str, Any]] = []
        for name, value in context.items():
            if name.startswith("__"):
                continue
            call_results = value if isinstance(value, list) else [value]
            for result in call_results:
                if isinstance(result, dict) and result.get("error"):
                    tool_errors.append({"tool": name, "error": result.get("error"),
                                        "message": result.get("message", "")})
        if intent.blocked:
            verdict = "blocked"
        elif tool_errors:
            verdict = "partial"
        elif evidence and cited_markers:
            verdict = "grounded"
        elif evidence:
            verdict = "needs_source_markers"
        else:
            verdict = "needs_more_evidence"

        # Task 4.x: answer_mode is decided by the STRUCTURED capability contract
        # from the intent/router result — the old answer-length / Chinese-keyword
        # / called-tools heuristics are removed and never participate in the gate.
        from .capability import Capability, capability_for_intent

        contract = capability_for_intent(intent)
        resolved_bound = [
            c for c in citations
            if c.get("reference_resolved") and c.get("citation_marker_status") == "present"
        ]
        compare_missing: list[int] = []
        action_failure_mode: dict[str, Any] | None = None
        recovered_warnings: list[dict[str, Any]] = []
        if contract.capability == Capability.BLOCKED:
            # §25.2: destructive requests are blocked, never action_result.
            answer_mode = "blocked"
            evidence_needs = False
        elif contract.capability == Capability.CLARIFY:
            answer_mode = "clarified"
            evidence_needs = False
        elif contract.capability == Capability.ACTION:
            evidence_needs = False
            # §25.3/§26.2: required steps must each succeed at least once AND
            # the terminal tool's final outcome must be success; early errors
            # recovered by a later success are warnings, not failures.
            action_failure_mode = self._action_outcome(context, contract)
            recovered_warnings = self._recovered_warnings(context, contract)
            answer_mode = "action_failed" if action_failure_mode else "action_result"
        elif contract.capability == Capability.COMPARE:
            evidence_needs = True
            # §24.2: the obligation comes from the VALIDATED compare_papers
            # call arguments — never from the tool's own result, which could
            # shrink its own obligation. Tool error or fewer than two valid
            # targets always abstain.
            target_paper_ids = self._compare_target_paper_ids(context)
            if self._compare_tool_error(context) or len(target_paper_ids) < 2:
                compare_missing = list(target_paper_ids)
                answer_mode = "abstained"
            else:
                covered = {c.get("paper_id") for c in resolved_bound if c.get("paper_id") is not None}
                compare_missing = [pid for pid in target_paper_ids if pid not in covered]
                answer_mode = "abstained" if compare_missing else "answered"
        else:  # FACTUAL / REPORT
            evidence_needs = True
            answer_mode = "answered" if resolved_bound else "abstained"

        evidence_status = (
            "sufficient"
            if (evidence_needs and answer_mode == "answered")
            else ("insufficient" if evidence else "none")
        )
        # citation_presence: marker occurrence only (NOT proof of support).
        if not evidence_needs:
            citation_presence = "not_required"
        elif cited_markers and len(cited_markers) == len(citations):
            citation_presence = "present_all"
        elif cited_markers:
            citation_presence = "present_partial"
        else:
            citation_presence = "absent"

        # Task 3.4: retrieval / reference resolution / citation binding / claim
        # support are SEPARATE dimensions. retrieval_status counts ONLY valid
        # typed evidence — legacy / foreign-metadata / missing-project-metadata
        # structures are expressed by legacy_unresolved_count, never as
        # metadata or fulltext availability (§22).
        fulltext_items = [
            e for e in evidence
            if e.get("evidence_type") == "fulltext" and not e.get("__legacy_unresolved")
        ]
        meta_items = [
            e for e in evidence
            if e.get("evidence_type") == "metadata" and not e.get("__legacy_unresolved")
        ]
        if fulltext_items:
            retrieval_status = "fulltext"
        elif meta_items:
            retrieval_status = "metadata"
        else:
            retrieval_status = "none"
        if not evidence_needs:
            citation_binding_status = "not_required"
        elif resolved_citations:
            citation_binding_status = (
                "fully_bound" if len(resolved_citations) == len(cited_markers)
                else "partially_bound")
        else:
            citation_binding_status = "unbound"
        claim_support_status = "pending" if resolved_citations else "not_required"
        legacy_unresolved_count = sum(
            1 for c in citations if c.get("resolution_reason") == "legacy_unresolved")

        return {
            "verdict": verdict,
            "evidence_count": len(evidence),
            "source_marker_count": len(cited_markers),
            "resolved_citation_count": len(resolved_citations),
            "citations": citations,
            "verified_count": len(cited_markers),
            "unverified_count": len(citations) - len(cited_markers),
            "tool_errors": tool_errors,
            # S1 structured fields
            "answer_mode": answer_mode,
            "evidence_status": evidence_status,
            "citation_presence": citation_presence,
            # Task 3.4: separate quality dimensions
            "retrieval_status": retrieval_status,
            "reference_resolution_status": (
                "resolved" if resolved_citations and len(resolved_citations) == len(cited_markers)
                else ("partial" if resolved_citations else "unresolved")),
            "citation_binding_status": citation_binding_status,
            "claim_support_status": claim_support_status,
            "legacy_unresolved_count": legacy_unresolved_count,
            "compare_missing_paper_ids": compare_missing,
            "action_failure_mode": action_failure_mode,
            "recovered_warnings": recovered_warnings,
        }

    def _compare_target_paper_ids(self, context: dict[str, Any]) -> list[int]:
        """§24.2: compare obligation comes from the VALIDATED compare_papers
        call arguments stored by the loop/plan (``__args_compare_papers``),
        never from the tool's own result — a result must not be allowed to
        shrink its own obligation."""
        paper_ids: list[int] = []
        seen: set[int] = set()
        for args in context.get("__args_compare_papers") or []:
            if not isinstance(args, dict):
                continue
            for pid in args.get("paper_ids") or []:
                try:
                    pid = int(pid)
                except (TypeError, ValueError):
                    continue
                if pid not in seen:
                    seen.add(pid)
                    paper_ids.append(pid)
        return paper_ids

    def _compare_tool_error(self, context: dict[str, Any]) -> bool:
        result = context.get("compare_papers")
        results = result if isinstance(result, list) else [result]
        return any(
            isinstance(call, dict) and call.get("error") for call in results
        )

    def _action_outcome(self, context: dict[str, Any],
                        contract) -> dict[str, Any] | None:
        """§25.3/§26.2: an ACTION succeeds only when EVERY required step has at
        least one successful outcome AND the terminal tool's FINAL outcome is
        success. A required step that never succeeded, a terminal tool that
        never ran, or a terminal failure → action_failed."""
        required_steps = getattr(contract, "required_steps", ())
        terminal_tools = getattr(contract, "terminal_tools", ())
        for step in required_steps:
            result = context.get(step)
            results = result if isinstance(result, list) else [result]
            if not any(isinstance(r, dict) and not r.get("error")
                       for r in results):
                return {"mode": "required_step_failed", "tool": step}
        for tool in terminal_tools:
            result = context.get(tool)
            results = result if isinstance(result, list) else [result]
            results = [r for r in results if isinstance(r, dict)]
            if not results:
                return {"mode": "required_step_not_executed", "tool": tool}
            latest = results[-1]
            if latest.get("error"):
                return {"mode": "terminal_failure", "tool": tool,
                        "error": str(latest["error"])[:120]}
        return None

    def _recovered_warnings(self, context: dict[str, Any],
                            contract) -> list[dict[str, Any]]:
        """Errors of ANY step (required, terminal or other) whose LATEST
        outcome succeeded — the earlier error was recovered and is recorded as
        a warning, never as an action failure (§25.3/§26.2)."""
        relevant = set(getattr(contract, "required_steps", ())) | \
            set(getattr(contract, "terminal_tools", ()))
        warnings: list[dict[str, Any]] = []
        for name, value in context.items():
            if name.startswith("__"):
                continue
            results = value if isinstance(value, list) else [value]
            results = [r for r in results if isinstance(r, dict)]
            if not results:
                continue
            if results[-1].get("error"):
                continue  # latest outcome failed → handled by _action_outcome
            first_error = next(
                (str(r.get("error"))[:120] for r in results if r.get("error")),
                None)
            if first_error is not None:
                warnings.append({"tool": name, "error": first_error,
                                 "step": "required" if name in relevant else "other"})
        return warnings

    def _compose_abstention(self, quality: dict[str, Any], intent, context: dict[str, Any]) -> str:
        """Standard fail-closed abstention. Never appends citations or a
        "证据依据" block; a compare shortfall is disclosed structurally."""
        from .capability import Capability, capability_for_intent

        lines = [
            "项目库暂无相关证据来回答这个问题。",
            "",
            "你可以：",
            "- 明确想问的论文或主题，我再检索",
            "- 让我搜索外部论文源（DBLP/arXiv/OpenAlex）补充",
            "- 上传相关 PDF 入库后重新提问",
        ]
        contract = capability_for_intent(intent)
        if contract.capability == Capability.COMPARE:
            missing = quality.get("compare_missing_paper_ids") or []
            if missing:
                lines.insert(
                    0,
                    f"对比证据不足：有 {len(missing)} 篇目标论文缺少可验证的全文证据，"
                    "无法完成可靠比较。",
                )
        return "\n".join(lines)

    def _compose_action_failure(self, quality: dict[str, Any]) -> str:
        """Deterministic safe failure note for a failed action tool (§24.3).

        Never echoes the raw model's success claim, never includes payloads or
        exception internals; the raw answer is preserved in quality."""
        mode = quality.get("action_failure_mode") or {}
        tool = str(mode.get("tool") or "工具")
        return (
            f"该操作未能成功完成：{tool} 返回了错误。"
            "已保留你的输入，请重试或检查项目状态。"
        )

    def _evidence_markers(self, evidence: list[dict[str, Any]]) -> list[str]:
        markers: list[str] = []
        for item in evidence:
            marker = self._source_marker(item)
            if marker and marker not in markers:
                markers.append(marker)
        return markers

    def _answer_has_any_marker(self, answer: str, context: dict[str, Any]) -> bool:
        """Did the model itself cite any evidence marker in its raw answer?

        B3 (§3.4): distinguishes genuine model citations from markers that
        `_ensure_source_markers` appended afterwards. Uses the SAME explicit
        `[cite:<marker>]` token extractor as `_quality_check` (§21.4) — a bare
        marker or natural-language substring never counts as a citation.
        """
        tokens = set(extract_citation_markers(answer))
        if not tokens:
            return False
        evidence = self._collect_evidence(context)
        if evidence:
            marker_set = {m.lower() for m in self._evidence_markers(evidence) if m}
            if tokens & marker_set:
                return True
        # any explicit [cite:...] token the model itself wrote is a citation
        return True

    def _source_marker(self, item: dict[str, Any]) -> str:
        for key in ("source_marker", "citation", "title", "docname"):
            marker = str(item.get(key) or "").strip()
            if marker:
                return marker
        return ""

    def _collect_evidence(self, context: dict[str, Any]) -> list[dict[str, Any]]:
        """Typed evidence collection (Task 3.6, §20.4): scan EVERY tool result
        in the context and parse every evidence-shaped entry through the shared
        parser/factory instead of a tool-name whitelist.

        - ``evidence`` lists and ``chunks`` lists are both parsed.
        - Valid fulltext envelopes and valid metadata evidence are collected.
        - Migration-period legacy structures are downgraded (marked
          ``__legacy_unresolved``) — kept for UI/audit but never counted as
          full-text availability.
        - Malformed structures are dropped entirely.
        """
        from .evidence import parse_evidence_item

        evidence: list[dict[str, Any]] = []
        trusted_project_id = self._context.project_id
        for name, value in context.items():
            if name.startswith("__"):
                continue
            results = value if isinstance(value, list) else [value]
            for result in results:
                if not isinstance(result, dict):
                    continue
                raw_items = list(result.get("evidence") or []) + list(result.get("chunks") or [])
                # compare_papers nests per-paper evidence/chunks inside
                # ``papers`` — the typed collector follows it (Task 4.x).
                for paper in result.get("papers") or []:
                    if not isinstance(paper, dict):
                        continue
                    raw_items += list(paper.get("evidence") or []) + list(paper.get("chunks") or [])
                for item in raw_items:
                    parsed = parse_evidence_item(item, trusted_project_id=trusted_project_id)
                    if parsed.kind == "malformed":
                        continue
                    evidence.append(parsed.item)
        return evidence

    def _safe_args(self, args: dict[str, Any]) -> dict[str, Any]:
        safe = dict(args)
        if "papers" in safe:
            safe["papers"] = f"{len(safe.get('papers') or [])} papers"
        return safe

    def _tool_summary(self, name: str, result: dict[str, Any]) -> dict[str, Any]:
        if result.get("error"):
            return {
                "status": "error",
                "error": result.get("error", ""),
                "error_message": result.get("message", ""),
            }
        if name == "search_papers":
            return {"count": result.get("count", 0)}
        if name == "add_papers_to_project":
            return {"count": result.get("count", 0)}
        if name == "list_project_papers":
            return {"count": result.get("count", 0)}
        if name == "query_project_rag":
            return {"count": len(result.get("evidence") or []), "fallback": result.get("fallback", "")}
        if name == "get_project_citation_graph":
            graph = result.get("graph") or {}
            return {"nodes": len(graph.get("nodes") or []), "edges": len(graph.get("edges") or [])}
        if name == "draft_report_section":
            return {"length": len(result.get("section") or "")}
        return {"status": "ok"}

    def _graph_summary(self, graph: dict[str, Any]) -> dict[str, Any]:
        nodes = graph.get("nodes") or []
        edges = graph.get("edges") or []
        title_by_id = {str(node.get("id")): node.get("title") for node in nodes}
        # 推荐先读：按 pagerank（seminal）降序，seminal 相同时按引用数
        top_nodes = sorted(
            nodes,
            key=lambda node: (
                float(node.get("seminal") or 0),
                int(node.get("citation_count") or 0),
                int(node.get("year") or 0),
            ),
            reverse=True,
        )[:8]
        top_edges = []
        for edge in sorted(edges, key=lambda item: item.get("weight") or 0, reverse=True)[:8]:
            source = edge.get("source")
            target = edge.get("target")
            top_edges.append(
                {
                    "source": source,
                    "target": target,
                    "source_title": title_by_id.get(str(source), str(source)),
                    "target_title": title_by_id.get(str(target), str(target)),
                    "weight": edge.get("weight", 1),
                }
            )
        # 主题分组：按 cluster 聚合，每组取 seminal 最高的论文标题作为主题标签
        topic_groups: dict[int, dict[str, Any]] = {}
        for node in nodes:
            cid = node.get("cluster") or 0
            if cid not in topic_groups:
                topic_groups[cid] = {
                    "label": node.get("cluster_label") or f"主题 {cid}",
                    "members": [],
                    "best_seminal": -1.0,
                    "best_title": "",
                }
            g = topic_groups[cid]
            g["members"].append(node.get("title") or "")
            seminal = float(node.get("seminal") or 0)
            if seminal > g["best_seminal"]:
                g["best_seminal"] = seminal
                g["best_title"] = node.get("title") or ""
        grouped = sorted(
            [
                {"label": g["label"], "count": len(g["members"]), "best_title": g["best_title"]}
                for g in topic_groups.values()
            ],
            key=lambda x: x["count"],
            reverse=True,
        )
        return {
            "node_count": len(nodes),
            "edge_count": len(edges),
            "top_nodes": [
                {
                    "id": node.get("id"),
                    "title": node.get("title"),
                    "year": node.get("year"),
                    "citation_count": node.get("citation_count"),
                    "cluster": node.get("cluster"),
                    "cluster_label": node.get("cluster_label"),
                    "seminal": node.get("seminal"),
                    "is_root": node.get("is_root"),
                    "is_frontier": node.get("is_frontier"),
                }
                for node in top_nodes
            ],
            "top_edges": top_edges,
            "topic_groups": grouped,
        }

    def _search_results_payload(self, result: dict[str, Any]) -> dict[str, Any]:
        return {
            "count": result.get("count", 0),
            "papers": [
                {
                    "title": item.get("title"),
                    "year": item.get("year"),
                    "source": item.get("source"),
                    "venue": item.get("venue"),
                    "citation_count": item.get("citation_count"),
                }
                for item in (result.get("papers") or [])[:8]
            ],
        }

    def _chunks(self, text: str, size: int = 48) -> list[str]:
        return [text[i : i + size] for i in range(0, len(text), size)] or [""]
