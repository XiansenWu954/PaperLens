"""Tasks 5.x (§30.1): unified event publisher.

The SINGLE production entry point for every project event. Harness, API views,
Celery ingestion/workflow tasks and the LangGraph workflow all publish through
``EventPublisher`` — direct ``ProjectRunEvent.objects.create`` calls are
forbidden.

The publisher:
- serializes the payload through the recursive per-type schema
  (``agent.events.sanitize_event``) — never persists raw contexts
- derives correlation ids from the server-bound run/project context
  (``project_id``/``run_id`` are required for run events; ``session_id`` /
  ``request_id`` may be null for Celery/workflow but the FIELDS are always
  present — ids are never fabricated to inflate completeness)
- persists ``ProjectRunEvent`` (unless ``persist=False``, e.g. token events)
- returns the public SSE payload ``{"event", "data"}``
"""
from __future__ import annotations

from typing import Any

from .events import sanitize_event


class EventPublisher:
    """One publisher per run (or per run-less workflow context)."""

    def __init__(
        self,
        run=None,
        *,
        project_id: int | None = None,
        run_id: int | None = None,
        session_id: int | None = None,
        request_id: str | None = None,
        persist: bool = True,
    ) -> None:
        if run is not None:
            self.run = run
            self.project_id = int(run.project_id)
            self.run_id = int(run.id)
            self.session_id = session_id
            self.request_id = request_id or None
        else:
            self.run = None
            self.project_id = int(project_id) if project_id is not None else None
            self.run_id = int(run_id) if run_id is not None else None
            self.session_id = session_id
            self.request_id = request_id or None
        self.persist = persist
        self._ids = {
            "project_id": self.project_id,
            "run_id": self.run_id,
            "session_id": self.session_id,
            "request_id": self.request_id,
        }

    def publish(
        self,
        event_type: str,
        payload: dict[str, Any],
        *,
        persist: bool | None = None,
    ) -> dict[str, Any]:
        """Serialize + persist + return the public SSE payload.

        ``persist=False`` (token events) still returns the serialized payload
        but never writes ProjectRunEvent.
        """
        safe_payload = sanitize_event(event_type, payload, ids=self._ids)
        should_persist = self.persist if persist is None else persist
        if should_persist and safe_payload and self.run is not None:
            from api.models import ProjectRunEvent

            ProjectRunEvent.objects.create(
                run=self.run,
                event_type=event_type,
                payload=safe_payload,
            )
        elif should_persist and safe_payload and self.run is None and self.run_id is not None:
            from api.models import ProjectRunEvent

            ProjectRunEvent.objects.create(
                run_id=self.run_id,
                event_type=event_type,
                payload=safe_payload,
            )
        return {"event": event_type, "data": safe_payload}

    def publish_with_key(
        self,
        event_type: str,
        payload: dict[str, Any],
        *,
        dedupe_key: str = "",
        persist: bool | None = None,
    ) -> dict[str, Any]:
        """Idempotent variant (Task 4.5): persists the event with a stable
        per-run dedupe key. A duplicate delivery of the same key within the
        run is silently suppressed (converge, never duplicate).

        The dedupe key MUST be derived from stable run identity + stable
        phase (e.g. ``run:{id}:rag_committed``); it must never contain
        question, payload or free text.
        """
        safe_payload = sanitize_event(event_type, payload, ids=self._ids)
        should_persist = self.persist if persist is None else persist
        if should_persist and safe_payload and dedupe_key:
            from api.models import ProjectRunEvent

            lookup = {"event_type": event_type, "dedupe_key": dedupe_key}
            if self.run is not None:
                lookup["run"] = self.run
            else:
                lookup["run_id"] = self.run_id
            ProjectRunEvent.objects.get_or_create(
                **lookup, defaults={"payload": safe_payload})
        elif should_persist and safe_payload:
            return self.publish(event_type, payload, persist=persist)
        return {"event": event_type, "data": safe_payload}
