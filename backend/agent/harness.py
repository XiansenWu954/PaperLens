"""Project Agent execution harness.

The harness owns tool routing, events, logging, and stream shape. It keeps
views thin and makes Function Calling/Prompt Engineering boundaries explicit.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from collections.abc import AsyncIterator
from typing import Any

from asgiref.sync import sync_to_async

from .intent import ProjectIntent, classify_project_intent
from .prompts import LIVE_PROJECT_CHAT_SYSTEM
from .project_tools import execute_project_tool

logger = logging.getLogger(__name__)


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
    ) -> None:
        self.project_id = project_id
        self.session_id = session_id
        self.tool_executor = tool_executor or execute_project_tool
        self.tool_timeout_seconds = tool_timeout_seconds
        self.use_llm = use_llm if use_llm is not None else os.environ.get("PAPERLENS_PROJECT_CHAT_LIVE_LLM") == "1"
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
                "message_preview": message[:120],
                "status": "running",
            },
        )

        async def emit(event_type: str, payload: dict[str, Any]) -> dict[str, Any]:
            await sync_to_async(ProjectRunEvent.objects.create)(
                run=run, event_type=event_type, payload=payload
            )
            return {"event": event_type, "data": payload}

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
            else:
                async for tool_event in self._execute_plan(intent, context):
                    if tool_event["event"] in {"tool_call", "tool_result", "search_results", "evidence", "paper_added", "graph"}:
                        yield await emit(tool_event["event"], tool_event["data"])
                    else:
                        yield tool_event
                answer = self._compose_answer(message, intent, context)
                should_use_llm = self.use_llm and not self._should_use_deterministic_answer(intent)
                if should_use_llm:
                    yield await emit(
                        "llm_call",
                        {
                            "provider": "deepseek",
                            "mode": "project_chat_answer",
                            "model": os.environ.get("DEEPSEEK_MODEL", "deepseek-v4-flash"),
                        },
                    )
                    answer, llm_event = await self._compose_live_answer(message, intent, context, history, answer)
                    yield await emit("llm_result", llm_event)
                else:
                    context.setdefault("answer_mode", "deterministic")

            answer = self._ensure_source_markers(answer, context)
            quality = self._quality_check(answer, intent, context)
            yield await emit("quality_check", quality)

            for chunk in self._chunks(answer):
                yield {"event": "token", "data": {"text": chunk}}

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
            logger.exception(
                "project chat harness failed",
                extra={
                    "event": "project_agent_run_failed",
                    "project_id": self.project_id,
                    "run_id": run.id,
                    "session_id": session.id,
                    "message_preview": message[:120],
                    "duration_ms": round((time.perf_counter() - started) * 1000, 2),
                    "status": "error",
                    "error": exc.__class__.__name__,
                },
            )
            run.status = "error"
            run.error_message = str(exc)[:1000]
            await sync_to_async(run.save)()
            yield await emit("error", {"message": str(exc)})

    async def _execute_plan(self, intent: ProjectIntent, context: dict[str, Any]) -> AsyncIterator[dict[str, Any]]:
        for step in intent.tool_plan:
            args = dict(step.arguments)
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
            try:
                result = await asyncio.wait_for(
                    self.tool_executor(step.name, args),
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
        lowered_answer = answer.lower()
        if any(marker.lower() in lowered_answer for marker in markers):
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
            citation = f" {item['citation']}" if item.get("citation") else ""
            title = item.get("title") or item.get("docname") or f"paper {item.get('paper_id')}"
            lines.append(f"- {title}: {item.get('summary', '')}{citation}")
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
            f"已刷新项目 Citation Map：{nodes} 个节点，{edges} 条关系。",
            "",
        ]
        if summary["top_nodes"]:
            lines.append("关键节点：")
            for node in summary["top_nodes"][:5]:
                role = []
                if node.get("is_root"):
                    role.append("root")
                if node.get("is_frontier"):
                    role.append("frontier")
                label = f" · {', '.join(role)}" if role else ""
                lines.append(f"- {node.get('title')} ({node.get('year') or 'n.d.'}){label}")
        if summary["top_edges"]:
            lines.append("")
            lines.append("主要关系：")
            for edge in summary["top_edges"][:5]:
                lines.append(
                    f"- {edge.get('source_title')} <-> {edge.get('target_title')}，共享参考权重 {edge.get('weight')}"
                )
        elif nodes:
            lines.append("")
            lines.append("当前图谱没有可解释的边，说明这些论文在已入库引用元数据上暂未形成明显共参考关系。")
        lines.append("")
        lines.append("图谱关系来自项目内论文的 referenced_works 共参考相似度，适合辅助识别根论文、前沿论文和主题簇。")
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
                lines.append(f"- {title}: {item.get('summary', '')} ({marker})")
        return "\n".join(lines)

    def _quality_check(self, answer: str, intent: ProjectIntent, context: dict[str, Any]) -> dict[str, Any]:
        evidence = self._collect_evidence(context)
        markers = set(self._evidence_markers(evidence))
        lowered_answer = answer.lower()
        cited_markers = [marker for marker in markers if marker.lower() in lowered_answer]
        tool_errors = [
            {"tool": name, "error": result.get("error"), "message": result.get("message", "")}
            for name, result in context.items()
            if isinstance(result, dict) and result.get("error")
        ]
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
        return {
            "verdict": verdict,
            "evidence_count": len(evidence),
            "source_marker_count": len(cited_markers),
            "tool_errors": tool_errors,
        }

    def _evidence_markers(self, evidence: list[dict[str, Any]]) -> list[str]:
        markers: list[str] = []
        for item in evidence:
            marker = self._source_marker(item)
            if marker and marker not in markers:
                markers.append(marker)
        return markers

    def _source_marker(self, item: dict[str, Any]) -> str:
        for key in ("source_marker", "citation", "title", "docname"):
            marker = str(item.get(key) or "").strip()
            if marker:
                return marker
        return ""

    def _collect_evidence(self, context: dict[str, Any]) -> list[dict[str, Any]]:
        evidence: list[dict[str, Any]] = []
        for name in ("query_project_rag", "draft_report_section"):
            result = context.get(name) or {}
            if isinstance(result, dict):
                evidence.extend(result.get("evidence") or [])
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
        top_nodes = sorted(
            nodes,
            key=lambda node: (
                bool(node.get("is_root")),
                bool(node.get("is_frontier")),
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
                    "is_root": node.get("is_root"),
                    "is_frontier": node.get("is_frontier"),
                }
                for node in top_nodes
            ],
            "top_edges": top_edges,
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
