"""Production tool-argument validation (§25.1, §26.1).

Every tool call — from the ReAct loop or the deterministic plan — MUST pass
through this single entry before the executor is invoked. Validation is the
FULL JSON Schema validation of ``PROJECT_AGENT_TOOLS`` declarations (root type,
required, property types, array items, minItems/maxItems, minimum/maximum and
additionalProperties=false) via the standard ``jsonschema`` validator.

Errors are stable and fully desensitized: neither the tool name, the argument
values, prompts nor payloads are ever echoed. Invalid arguments NEVER reach
the executor, never enter ``__args_<tool>`` and never form a compare
obligation.
"""
from __future__ import annotations

from typing import Any

import jsonschema

from .project_tools import PROJECT_AGENT_TOOLS

_SCHEMAS: dict[str, dict[str, Any]] = {
    tool["function"]["name"]: tool["function"]["parameters"]
    for tool in PROJECT_AGENT_TOOLS
}

_VALIDATOR = jsonschema.Draft202012Validator


def _field_path(exc: jsonschema.ValidationError) -> str:
    if exc.path:
        return ".".join(str(part) for part in exc.path)
    if exc.validator == "required":
        # jsonschema sets path to the missing property name
        return ".".join(str(p) for p in exc.path) or ""
    return ""


def validate_tool_arguments(
    tool_name: str, arguments: Any
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Validate ``arguments`` (any JSON-decoded root value) against the tool's
    declared JSON Schema.

    Returns ``(cleaned_args, None)`` on success (only schema-declared keys,
    with defaults untouched) or ``(None, stable_error)`` on failure. A
    non-object root (list/string/null) is an ``invalid_arguments`` error —
    never an exception.
    """
    schema = _SCHEMAS.get(tool_name)
    if schema is None:
        # §26.1: do NOT echo the model-provided tool name.
        return None, {"error": "unknown_tool", "message": "未知工具。"}
    if not isinstance(arguments, dict):
        return None, {"error": "invalid_arguments", "field": "",
                      "message": "参数不合法。"}
    try:
        _VALIDATOR(schema).validate(arguments)
    except jsonschema.ValidationError as exc:
        return None, {
            "error": "invalid_arguments",
            "field": _field_path(exc),
            "message": "参数不合法。",
        }
    cleaned = {key: value for key, value in arguments.items()
               if key in schema.get("properties", {})}
    return cleaned, None
