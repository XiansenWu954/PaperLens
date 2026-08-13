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


urlpatterns = [
    path("", health, name="health"),
    path("api/", include("api.urls")),
    path("admin/", admin.site.urls),
]
