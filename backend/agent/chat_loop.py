"""ChatAgentLoop: LLM 自主决策工具的 ReAct 循环(chat 主路径)。

替换旧的"关键词意图分类 → 固定工具链"模式。让 LLM 通过 function calling
自由决定调哪些工具、什么顺序、调几次(最多 max_iterations 轮),直到证据
足够后生成最终答案。

设计原则(对标 PaperQA2 / ReAct):
- LLM 决定"做什么"(选工具/参数/顺序),确定性代码保证"怎么做不出错"
- 破坏性操作(DESTRUCTIVE_TOKENS)硬拦截,LLM 不可绕过
- 工具预算上限(max_iterations)防失控
- 每轮 emit SSE 事件(tool_call/tool_result/evidence/...),前端实时可见
"""
from __future__ import annotations

import json
import logging
import os
import time
from typing import Any, AsyncIterator

from .context import ToolExecutionContext, create_context
from .project_tools import (
    AUTH_ARGUMENT_FIELDS,
    PROJECT_AGENT_TOOLS,
    execute_project_tool,
    strip_auth_fields,
)

logger = logging.getLogger(__name__)

# 复用 intent.py 的破坏性拦截(确定性安全网)
_DESTRUCTIVE_KEYWORDS = ("删除", "清空", "覆盖报告", "delete", "remove all", "clear", "drop", "overwrite")

SYSTEM_PROMPT = """你是 PaperLens，一个专业的 CS（计算机科学）论文研究 Agent。
你在一个"项目"内工作，项目有自己的论文库。你通过调用工具来帮助研究者完成文献调研。

你可以使用以下工具（按需自由组合，不必全用）：

## 工具清单

1. **query_project_rag**: 在当前项目论文库内做证据检索（混合向量+词级），返回带来源的 grounded 证据。
   - 何时用：用户问论文方法/细节/对比，需要从已入库论文找证据时。
   - 项目库为空或证据不足时，先考虑 search_papers 补充。

2. **search_papers**: 从 DBLP/OpenAlex/arXiv 检索外部论文。
   - 何时用：项目库不足以回答，或用户要"找/搜索/补充/扩大范围"时。

3. **add_papers_to_project**: 把论文加入项目库（非破坏性，可重复）。
   - 何时用：检索到新论文后入库，让后续 RAG 能检索到。

4. **list_project_papers**: 列出项目库的论文。
   - 何时用：用户问"有哪些论文/库多大"。

5. **get_project_citation_graph**: 构建项目引用图谱，返回推荐先读、主题分组、关联论文。
   - 何时用：用户要"图谱/引用关系/推荐先读/主题"。

6. **draft_report_section**: 基于项目证据起草报告章节（带 [cite] 引用）。
   - 何时用：用户要"综述/报告/草稿/related work/总结"。

7. **compare_papers**: 对比多篇论文，对每篇单独执行全文 Hybrid RAG，均衡提取证据。
   - 何时用：**任何比较/对比任务必须用这个工具**（不要用 query_project_rag 做比较）。
   - 它会对每篇论文取 2-3 个全文 chunk（不是摘要），确保两侧都有证据覆盖。
   - 返回 paper_coverage：若 < 1.0 说明有论文缺全文证据，比较可能不完整，需说明。

8. **read_paper_section**: 读单篇论文的指定章节或全文 chunk。
   - 何时用：用户问某篇论文的具体细节，需要读特定章节时。

9. **export_bibtex**: 导出项目论文库为 BibTeX。
   - 何时用：用户要"导出/BibTeX/参考文献格式"。

## 硬约束（必须遵守）

1. **只引用工具返回的真实证据**。绝不在证据之外编造论断、论文标题或引用。
   **如果工具返回的证据不足或为空，你必须明确说明"项目库暂无相关证据"，不得用自己的领域知识填充答案。**
   这是 P0：没有项目证据时回答领域事实 = 编造。
2. **破坏性操作（删除论文/清空项目/覆盖报告）不要自主执行**，提示用户走显式操作。
3. **证据不足时明确说明缺口**（"项目库暂无相关证据，建议检索 X 方向"），不要硬答。
4. **比较任务必须用 compare_papers**（不是 query_project_rag），确保每篇论文都有全文证据。
5. **不能把仅摘要/未入库全文的论文描述为已阅读全文**；metadata fallback 要明确标注。
6. 回答用中文，标注来源用 [cite:论文标题] 格式。
7. 工具参数：query 是检索关键词；**project_id 由服务端绑定，你不用也不应提供**。
8. **工具返回关键错误时（DoesNotExist/IntegrityError/error 字段），必须如实告知用户该操作失败，不要假装成功或用领域知识替代。**

## 答题策略

- 简单事实问答：直接 query_project_rag 取证据 → 基于证据回答。
- 证据不足：先 search_papers 补充 → add_papers_to_project 入库 → 再 query_project_rag。
- 综述/报告：query_project_rag 取充分证据 → draft_report_section 生成。
- **对比多篇：compare_papers（对每篇取全文证据，不要只用 query_project_rag 或摘要）**。
- 图谱/关系：get_project_citation_graph。

不要为了调工具而调工具。证据够就停，给用户清晰的答案。"""


class ChatAgentLoop:
    """LLM 自主决策工具的 ReAct 循环。"""

    def __init__(
        self,
        project_id: int,
        *,
        max_iterations: int = 8,
        tool_executor=execute_project_tool,
        context: ToolExecutionContext | None = None,
    ) -> None:
        # Task 2.1 (§18.1): the frozen ToolExecutionContext is the ONLY trusted
        # identity. A supplied context whose project differs from the positional
        # project_id is an identity split and must be rejected outright — logs,
        # scope-violation events and the executor all derive from `self.context`.
        if context is not None and int(context.project_id) != int(project_id):
            raise ValueError(
                f"ChatAgentLoop identity mismatch: project_id={project_id} vs "
                f"context.project_id={context.project_id}. ToolExecutionContext "
                "is the only trusted project identity."
            )
        self.project_id = int(project_id)
        self.max_iterations = max_iterations
        self.tool_executor = tool_executor
        self.context = context or create_context(project_id)

    # B1: tool budget caps (GPT decision). Prevents runaway search loops.
    TOOL_BUDGET = 12  # total tool calls before forcing a final answer
    SEARCH_PER_RUN = 3  # max search_papers calls per conversation

    async def run(
        self,
        message: str,
        history: list[dict] | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        """跑 ReAct 循环，yield SSE 事件。

        事件序列：agent_thinking → (tool_call → tool_result)* → final_answer
        每个事件是 {"event": str, "data": dict}。
        """
        from llm.deepseek import DeepSeekClient

        client = DeepSeekClient()
        messages: list[dict] = [{"role": "system", "content": SYSTEM_PROMPT}]

        # 注入多轮历史（解决指代/省略："再找 3 篇"）
        if history:
            messages.extend(history[-8:])

        messages.append({"role": "user", "content": message})

        # P0-1: strip project_id from the LLM-visible schema. The model must not
        # supply project_id (server binds it). GPT: "从模型可见 schema 删除 project_id"。
        tools_schema = []
        for tool in PROJECT_AGENT_TOOLS:
            fn = dict(tool.get("function", {}))
            params = dict(fn.get("parameters", {}))
            props = dict(params.get("properties", {}))
            props.pop("project_id", None)
            params["properties"] = props
            required = [r for r in params.get("required", []) if r != "project_id"]
            if required:
                params["required"] = required
            elif "required" in params:
                del params["required"]
            fn["parameters"] = params
            tools_schema.append({"type": "function", "function": fn})

        context: dict[str, Any] = {}  # 工具结果缓存（供 quality_check）
        tool_call_count = 0
        search_count = 0  # B1: search_papers call counter
        search_paper_ids: set = set()  # B2: track papers found across searches
        consecutive_empty_search = 0  # B2: consecutive searches with no new candidates

        yield {"event": "agent_mode", "data": {"mode": "react", "max_iterations": self.max_iterations}}

        for iteration in range(self.max_iterations):
            logger.info(
                "chat agent loop iteration",
                extra={
                    "event": "chat_agent_iteration",
                    "project_id": self.project_id,
                    "iteration": iteration,
                    "tool_calls_so_far": tool_call_count,
                },
            )
            yield {"event": "llm_call", "data": {"phase": "react", "iteration": iteration + 1}}
            _llm_started = time.perf_counter()
            r = client.complete_with_tools(
                messages, tools_schema, thinking=True, max_tokens=2048,
            )
            # §31.2: real measurements — status/answer_chars/duration_ms come
            # from the actual call, never fabricated constants.
            yield {"event": "llm_result", "data": {
                "phase": "react", "iteration": iteration + 1,
                "usage": r.get("usage", {}),
                "status": "ok",
                "answer_chars": len(r.get("content") or ""),
                "duration_ms": round((time.perf_counter() - _llm_started) * 1000, 2),
            }}
            tool_calls = r.get("tool_calls", [])
            content = r.get("content", "")
            # DeepSeek thinking-mode: the follow-up request must carry back the
            # prior reasoning_content, or the API can return 400 on the next
            # tool round (deepseek-live-evaluation-plan §3.3).
            reasoning_content = r.get("reasoning_content")

            # 追加 assistant 消息（保持 LLM 对话上下文）
            assistant_msg: dict[str, Any] = {"role": "assistant", "content": content}
            # reasoning_content 仅在有工具调用时需要回填（无工具调用即结束，无需再请求）
            if tool_calls and reasoning_content:
                assistant_msg["reasoning_content"] = reasoning_content
            if tool_calls:
                assistant_msg["tool_calls"] = [
                    {"id": tc["id"], "type": "function",
                     "function": {"name": tc["name"], "arguments": tc["arguments"]}}
                    for tc in tool_calls
                ]
            messages.append(assistant_msg)

            # 没有工具调用 → LLM 决定直接回答，ReAct 结束
            if not tool_calls:
                logger.info(
                    "chat agent loop complete (no more tool calls)",
                    extra={"event": "chat_agent_complete", "iterations": iteration, "tool_calls": tool_call_count},
                )
                yield {"event": "final_answer_raw", "data": {"answer": content, "context": context}}
                return

            # 执行每个工具调用
            for tc in tool_calls:
                tool_name = tc["name"]
                try:
                    raw_args = tc["arguments"]
                    parsed_args = json.loads(raw_args) if raw_args else {}
                except json.JSONDecodeError:
                    parsed_args = {}
                if not isinstance(parsed_args, dict):
                    # §26.1: a non-object root argument (list/string/null) is a
                    # stable invalid_arguments error — never a crash, never run.
                    tool_result = {"error": "invalid_arguments", "field": "",
                                   "message": "参数不合法。"}
                    messages.append({"role": "tool", "tool_call_id": tc["id"],
                                     "content": json.dumps(tool_result, ensure_ascii=False)})
                    context.setdefault(tool_name, []).append(tool_result)
                    context[f"__latest_{tool_name}"] = tool_result
                    yield {"event": "tool_result", "data": {
                        "name": tool_name, "status": "error",
                        "error": "invalid_arguments",
                        "error_message": "参数不合法。"}}
                    continue
                args = parsed_args

                # Task 2.1/2.2 (§18.1): authorization fields are server-bound.
                # Every smuggled copy is stripped; all rejected field NAMES are
                # audited (never their values, never the full prompt/payload).
                # A forged numeric project_id that differs from the trusted
                # context additionally emits a safe scope-violation event.
                smuggled = {k: v for k, v in args.items() if k in AUTH_ARGUMENT_FIELDS}
                args = strip_auth_fields(args)
                attempted_project_id = None
                if smuggled:
                    rejected_fields = sorted(smuggled.keys())
                    raw_pid = smuggled.get("project_id")
                    try:
                        forged = int(raw_pid) if raw_pid is not None else None
                    except (TypeError, ValueError):
                        forged = None
                    if forged is not None and forged != self.context.project_id:
                        attempted_project_id = forged
                        yield {"event": "tool_scope_violation", "data": {
                            "project_id": self.context.project_id,
                            "run_id": self.context.run_id,
                            "session_id": self.context.session_id,
                            "request_id": self.context.request_id,
                            "tool": tool_name,
                            "rejected_fields": rejected_fields,
                            "attempted_project_id": forged,
                        }}
                    logger.warning(
                        "auth fields rejected",
                        extra={
                            "event": "auth_fields_rejected",
                            "tool_name": tool_name,
                            "rejected_fields": rejected_fields,
                            **self.context.to_audit(),
                        },
                    )

                # 破坏性拦截（确定性安全网）
                blocked_reason = self._check_destructive(tool_name, args)
                if blocked_reason:
                    yield {"event": "tool_call", "data": {
                        "name": tool_name, "tool_call_id": tc["id"],
                        "summary": "blocked: destructive operation"}}
                    tool_result = {"error": "blocked", "message": blocked_reason}
                    messages.append({"role": "tool", "tool_call_id": tc["id"],
                                     "content": json.dumps(tool_result, ensure_ascii=False)})
                    continue

                # B1: total tool budget cap
                if tool_call_count >= self.TOOL_BUDGET:
                    tool_result = {"error": "budget_exceeded",
                                   "message": f"工具调用预算已用完（{self.TOOL_BUDGET} 次），基于现有证据回答。"}
                    messages.append({"role": "tool", "tool_call_id": tc["id"],
                                     "content": json.dumps(tool_result, ensure_ascii=False)})
                    break

                # B1: search-per-run cap + B2: consecutive empty search stop
                if tool_name == "search_papers":
                    if search_count >= self.SEARCH_PER_RUN:
                        tool_result = {"error": "search_budget",
                                       "message": f"搜索预算已用完（{self.SEARCH_PER_RUN} 次），基于现有证据回答。"}
                        messages.append({"role": "tool", "tool_call_id": tc["id"],
                                         "content": json.dumps(tool_result, ensure_ascii=False)})
                        continue
                    if consecutive_empty_search >= 2:
                        tool_result = {"error": "no_new_candidates",
                                       "message": "连续两次搜索无新增候选，停止搜索。"}
                        messages.append({"role": "tool", "tool_call_id": tc["id"],
                                         "content": json.dumps(tool_result, ensure_ascii=False)})
                        continue

                tool_call_count += 1
                # Tasks 5.x (§28.1): the tool_call event carries only the tool
                # name, iteration, call id and audit id — never question/query,
                # papers payloads or full arguments (the harness sanitizer
                # enforces the allowlist at emit time).
                yield {"event": "tool_call", "data": {
                    "name": tool_name,
                    "tool_call_id": tc["id"],
                    "iteration": iteration + 1,
                    "model_supplied_project_id": attempted_project_id,  # None unless a forged project_id was seen
                }}

                # §25.1: production schema validation BEFORE execution. Invalid
                # arguments never reach the executor, never enter
                # ``__args_<tool>`` and never form a compare obligation.
                from .validation import validate_tool_arguments

                validated_args, validation_error = validate_tool_arguments(tool_name, args)
                if validation_error is not None:
                    tool_result = {
                        "error": validation_error["error"],
                        "message": validation_error["message"],
                        "field": validation_error.get("field", ""),
                    }
                    messages.append({"role": "tool", "tool_call_id": tc["id"],
                                     "content": json.dumps(tool_result, ensure_ascii=False)})
                    context.setdefault(tool_name, []).append(tool_result)
                    context[f"__latest_{tool_name}"] = tool_result
                    yield {"event": "tool_result", "data": {
                        "name": tool_name, "status": "error",
                        "error": validation_error["error"],
                        "error_message": validation_error["message"]}}
                    continue
                args = validated_args

                try:
                    result = await self.tool_executor(self.context, tool_name, args)
                except Exception as exc:
                    # §32.2: the model context receives ONLY the fixed failure
                    # copy + exception type + digest — the raw exception body
                    # must never be amplified into the final answer.
                    from .events import error_hash

                    result = {
                        "error": exc.__class__.__name__,
                        "message": "工具执行失败，请稍后重试。",
                        "error_hash": error_hash(exc),
                    }

                # P1: context[tool_name] overwrites earlier calls of the same tool.
                # Use an ordered list keyed by tool name so multi-call results survive.
                context.setdefault(tool_name, [])
                if isinstance(context[tool_name], list):
                    context[tool_name].append(result)
                else:
                    context[tool_name] = [context[tool_name], result]
                # §24.2: keep the server-validated call arguments (auth fields
                # already stripped) so capability policy can derive obligations
                # from what was actually requested — never from tool results.
                context.setdefault(f"__args_{tool_name}", []).append(dict(args))
                # Keep a flat latest-result alias for _quality_check / _compose helpers
                # that expect a single dict (backward compat).
                context[f"__latest_{tool_name}"] = result
                messages.append({"role": "tool", "tool_call_id": tc["id"],
                                 "content": json.dumps(result, ensure_ascii=False, default=str)[:3000]})

                # emit 工具结果 + 富事件
                yield {"event": "tool_result", "data": self._tool_summary(tool_name, result)}
                if tool_name == "query_project_rag":
                    yield {"event": "evidence", "data": result}
                elif tool_name == "add_papers_to_project":
                    yield {"event": "paper_added", "data": result}
                elif tool_name == "search_papers":
                    # B1/B2: track search count + new candidates
                    search_count += 1
                    found_ids = set()
                    for p in (result.get("papers") or result.get("results") or []):
                        if isinstance(p, dict):
                            found_ids.add(p.get("source_id") or p.get("title") or "")
                    new_ids = found_ids - search_paper_ids
                    if not new_ids:
                        consecutive_empty_search += 1
                    else:
                        consecutive_empty_search = 0
                        search_paper_ids.update(new_ids)
                    yield {"event": "search_results", "data": result}
                elif tool_name == "search_papers":
                    yield {"event": "search_results", "data": result}
                elif tool_name == "get_project_citation_graph":
                    yield {"event": "graph", "data": result.get("graph", {"nodes": [], "edges": []})}

        # 达到 max_iterations，强制让 LLM 基于已有证据生成最终答案
        logger.info(
            "chat agent loop reached max iterations, forcing final answer",
            extra={"event": "chat_agent_max_iterations", "max": self.max_iterations},
        )
        messages.append({"role": "user", "content": "（工具调用预算已用完，请基于已收集的证据给出最终回答，不要再调工具。）"})
        yield {"event": "llm_call", "data": {"phase": "force_answer", "iteration": self.max_iterations}}
        _llm_started = time.perf_counter()
        r = client.complete_with_tools(
            messages, tools_schema, thinking=False, max_tokens=1500,
        )
        yield {"event": "llm_result", "data": {
            "phase": "force_answer", "iteration": self.max_iterations,
            "usage": r.get("usage", {}),
            "status": "ok",
            "answer_chars": len(r.get("content") or ""),
            "duration_ms": round((time.perf_counter() - _llm_started) * 1000, 2),
        }}
        yield {"event": "final_answer_raw", "data": {"answer": r.get("content", ""), "context": context}}

    def _check_destructive(self, tool_name: str, args: dict) -> str | None:
        """破坏性操作拦截。"""
        text = f"{tool_name} {args.get('query', '')} {args.get('question', '')}".lower()
        for kw in _DESTRUCTIVE_KEYWORDS:
            if kw in text:
                return f"检测到破坏性关键词 '{kw}'，该操作需用户显式执行。"
        return None

    def _tool_summary(self, name: str, result: dict) -> dict:
        """工具结果的摘要（给前端 trace 用）。C1: error 契约与 harness 对齐。"""
        if result.get("error"):
            return {"name": name, "status": "error",
                    "error": result.get("error", ""),  # error_code (class name)
                    "error_message": result.get("message", ""),  # human-readable
                    "retryable": False}
        if name == "query_project_rag":
            return {"name": name, "count": len(result.get("evidence") or []),
                    "fallback": result.get("fallback", "")}
        if name in ("search_papers", "add_papers_to_project", "list_project_papers"):
            return {"name": name, "count": result.get("count", 0)}
        if name == "get_project_citation_graph":
            graph = result.get("graph") or {}
            return {"name": name, "nodes": len(graph.get("nodes") or []),
                    "edges": len(graph.get("edges") or [])}
        if name == "draft_report_section":
            return {"name": name, "length": len(result.get("section") or "")}
        return {"name": name, "status": "ok"}
