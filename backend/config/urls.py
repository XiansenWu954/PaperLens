"""URL configuration for config project."""
import os

from django.conf import settings
from django.contrib import admin
from django.db import connection
from django.http import JsonResponse
from django.urls import include, path


def health(request):
    """健康检查 + 配置就绪状态端点。

    用于自检和诊断：报告 LLM key 是否配置、当前数据库后端、embedding provider。
    绝不返回任何密钥本身，只返回布尔就绪标志。
    """
    db_engine = ""
    try:
        db_engine = connection.settings_dict.get("ENGINE", "")
    except Exception:
        db_engine = ""
    return JsonResponse(
        {
            "status": "ok",
            "service": "PaperLens",
            "version": "0.3.0",
            "config": {
                # 仅返回布尔值，绝不回显 key 本身
                "deepseek_key_configured": bool(os.environ.get("DEEPSEEK_API_KEY", "")),
                "embedding_provider": getattr(settings, "PAPERLENS_EMBEDDING_PROVIDER", "bge-m3"),
                "database": "postgres" if "postgresql" in db_engine else "sqlite",
            },
        }
    )


def workflow_health(request):
    """P2-B-CX-02: durable workflow health.

    Reports durable_workflow_enabled and workflow_checkpointer_ready.
    Never exposes DSN, host, user, password, or table content."""
    from config.health import durable_workflow_health
    return JsonResponse(durable_workflow_health())


urlpatterns = [
    path("", health, name="health"),
    path("health/workflow", workflow_health, name="workflow_health"),
    path("api/", include("api.urls")),
    path("admin/", admin.site.urls),
]
