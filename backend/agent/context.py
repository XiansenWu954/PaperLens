"""Trusted execution context (frozen, server-created).

Tasks 2.1: project authorization for every Agent/MCP tool call comes ONLY from
this frozen context. The model never supplies authorization fields; the API
route/session, the harness run, or an authorized MCP selector bootstrap creates
the context, and tool implementations read identity exclusively from it.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass


@dataclass(frozen=True)
class ToolExecutionContext:
    """Immutable identity for one project tool invocation.

    Fields:
    - project_id: the ONLY trusted project identity (int).
    - run_id / session_id / request_id / actor: audit traceability, never
      accepted from model arguments.
    """

    project_id: int
    run_id: int | None = None
    session_id: int | None = None
    request_id: str = ""
    actor: str = "system"

    def to_audit(self) -> dict:
        """Safe identity summary for logs/events (no prompt, keys, or bodies)."""
        return {
            "project_id": self.project_id,
            "run_id": self.run_id,
            "session_id": self.session_id,
            "request_id": self.request_id,
            "actor": self.actor,
        }


def create_context(
    project_id: int,
    *,
    run_id: int | None = None,
    session_id: int | None = None,
    request_id: str = "",
    actor: str = "system",
) -> ToolExecutionContext:
    """Single creation entry point for ToolExecutionContext (Task 2.1)."""
    return ToolExecutionContext(
        project_id=int(project_id),
        run_id=run_id,
        session_id=session_id,
        request_id=request_id or uuid.uuid4().hex[:12],
        actor=actor,
    )
