"""§32.4: shared safe eval error helper.

Every eval artifact/result/log error field uses ONLY:
- exception type (stable public code)
- error hash (sha256 digest of type+message — the message itself never leaves)
- fixed user copy (business verdicts may be kept, Python/HTTP exception
  messages are never written to JSON/Markdown/console log)

Regex-only redaction of exception text is forbidden — opaque bodies without
secret keywords would pass through.
"""
from __future__ import annotations

from typing import Any


def exception_record(exc: BaseException) -> dict[str, Any]:
    """Safe machine-readable error record for eval artifacts."""
    from agent.events import error_hash

    return {
        "error": exc.__class__.__name__,
        "error_hash": error_hash(exc),
    }


def exception_message(exc: BaseException) -> str:
    """Fixed user copy for eval result reasons (no exception text)."""
    return f"{exc.__class__.__name__}: 评测执行失败，请查看服务日志。"
