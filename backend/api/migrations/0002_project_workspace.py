# Generated for PaperLens V2 project workspace.

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("papers", "0001_initial"),
        ("api", "0001_initial"),
    ]

    operations = [
        migrations.CreateModel(
            name="ResearchProject",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("title", models.CharField(max_length=240)),
                ("description", models.TextField(blank=True)),
                ("status", models.CharField(choices=[("active", "Active"), ("archived", "Archived")], default="active", max_length=16)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
        ),
        migrations.CreateModel(
            name="ChatSession",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("title", models.CharField(blank=True, max_length=240)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("project", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="chat_sessions", to="api.researchproject")),
            ],
        ),
        migrations.CreateModel(
            name="ProjectRun",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("kind", models.CharField(choices=[("research", "Research"), ("chat", "Chat"), ("report", "Report"), ("demo", "Demo")], default="research", max_length=16)),
                ("status", models.CharField(choices=[("pending", "Pending"), ("running", "Running"), ("done", "Done"), ("error", "Error")], default="pending", max_length=16)),
                ("question", models.TextField(blank=True)),
                ("output", models.TextField(blank=True)),
                ("error_message", models.TextField(blank=True)),
                ("sources", models.JSONField(blank=True, default=list)),
                ("citation_graph", models.JSONField(blank=True, default=dict)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("project", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="runs", to="api.researchproject")),
            ],
        ),
        migrations.CreateModel(
            name="ReportVersion",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("title", models.CharField(default="Research report", max_length=240)),
                ("content", models.TextField()),
                ("source", models.CharField(default="agent", max_length=32)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("project", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="reports", to="api.researchproject")),
            ],
            options={"ordering": ["-created_at", "-id"]},
        ),
        migrations.CreateModel(
            name="ProjectPaper",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("status", models.CharField(choices=[("candidate", "Candidate"), ("included", "Included"), ("core", "Core"), ("excluded", "Excluded")], default="candidate", max_length=16)),
                ("source_reason", models.TextField(blank=True)),
                ("added_by", models.CharField(choices=[("user", "User"), ("agent", "Agent"), ("demo", "Demo")], default="user", max_length=16)),
                ("notes", models.TextField(blank=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("paper", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="project_links", to="papers.paper")),
                ("project", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="project_papers", to="api.researchproject")),
            ],
        ),
        migrations.AddField(
            model_name="researchtask",
            name="project",
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="legacy_tasks", to="api.researchproject"),
        ),
        migrations.CreateModel(
            name="ProjectRunEvent",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("event_type", models.CharField(db_index=True, max_length=64)),
                ("payload", models.JSONField(blank=True, default=dict)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("run", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="events", to="api.projectrun")),
            ],
            options={"ordering": ["created_at", "id"]},
        ),
        migrations.CreateModel(
            name="ChatMessage",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("role", models.CharField(choices=[("user", "User"), ("assistant", "Assistant"), ("tool", "Tool"), ("system", "System")], max_length=16)),
                ("content", models.TextField()),
                ("metadata", models.JSONField(blank=True, default=dict)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("session", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="messages", to="api.chatsession")),
            ],
            options={"ordering": ["created_at", "id"]},
        ),
        migrations.AddConstraint(
            model_name="projectpaper",
            constraint=models.UniqueConstraint(fields=("project", "paper"), name="uniq_project_paper"),
        ),
    ]
