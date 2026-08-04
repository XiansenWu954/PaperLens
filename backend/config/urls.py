"""URL configuration for config project."""
from django.contrib import admin
from django.http import JsonResponse
from django.urls import include, path


def health(request):
    """地基健康检查端点。"""
    return JsonResponse({"status": "ok", "service": "PaperLens", "version": "0.1.0"})


urlpatterns = [
    path("", health, name="health"),
    path("api/", include("api.urls")),
    path("admin/", admin.site.urls),
]
