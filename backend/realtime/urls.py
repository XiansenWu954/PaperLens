"""realtime SSE 路由。"""
from django.urls import path

from . import views

urlpatterns = [
    path("<int:task_id>/stream", views.research_stream, name="research_stream"),
]
