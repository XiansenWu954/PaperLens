"""Database fields used by the RAG index."""
from __future__ import annotations

import json
from typing import Any

from django.db import models


class HybridVectorField(models.JSONField):
    """Store vectors as pgvector on PostgreSQL and JSON elsewhere.

    The V3 demo path uses PostgreSQL + pgvector. SQLite remains useful for fast
    local tests, so the Python API stays as a plain list in both modes.
    """

    description = "pgvector on PostgreSQL, JSON elsewhere"

    def __init__(self, *, dimensions: int = 1024, **kwargs: Any) -> None:
        self.dimensions = dimensions
        super().__init__(**kwargs)

    def db_type(self, connection) -> str:
        if connection.vendor == "postgresql":
            return f"vector({self.dimensions})"
        return super().db_type(connection)

    def get_placeholder(self, value, compiler, connection) -> str:
        if connection.vendor == "postgresql":
            return "%s::vector"
        return "%s"

    def get_db_prep_value(self, value, connection, prepared: bool = False):
        if connection.vendor != "postgresql":
            return super().get_db_prep_value(value, connection, prepared)
        if value is None or hasattr(value, "as_sql"):
            return value
        if hasattr(value, "tolist"):
            value = value.tolist()
        return "[" + ",".join(f"{float(item):.8f}" for item in value) + "]"

    def from_db_value(self, value, expression, connection):
        if value is None:
            return []
        if isinstance(value, list):
            return value
        if hasattr(value, "tolist"):
            return value.tolist()
        if not isinstance(value, str):
            try:
                return list(value)
            except TypeError:
                return value
        text = value.strip()
        if text.startswith("[") and text.endswith("]"):
            try:
                return [float(item) for item in text[1:-1].split(",") if item]
            except ValueError:
                pass
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return []

    def deconstruct(self):
        name, path, args, kwargs = super().deconstruct()
        kwargs["dimensions"] = self.dimensions
        return name, path, args, kwargs
