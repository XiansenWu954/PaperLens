# Generated for PaperLens V3 PDF ingestion jobs.

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("papers", "0001_initial"),
        ("api", "0002_project_workspace"),
    ]

    operations = [
        migrations.AlterField(
            model_name="projectrun",
            name="kind",
            field=models.CharField(
                choices=[
                    ("research", "Research"),
                    ("chat", "Chat"),
                    ("report", "Report"),
                    ("ingestion", "Ingestion"),
                    ("workflow", "Workflow"),
                    ("demo", "Demo"),
                ],
                default="research",
                max_length=16,
            ),
        ),
        migrations.CreateModel(
            name="PaperIngestionJob",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("status", models.CharField(choices=[("pending", "Pending"), ("parsing", "Parsing"), ("embedded", "Embedded"), ("failed", "Failed")], default="pending", max_length=16)),
                ("file_name", models.CharField(blank=True, max_length=255)),
                ("file_hash", models.CharField(blank=True, db_index=True, max_length=64)),
                ("file_path", models.CharField(blank=True, max_length=512)),
                ("source_url", models.URLField(blank=True, max_length=512)),
                ("chunk_count", models.IntegerField(default=0)),
                ("error_message", models.TextField(blank=True)),
                ("celery_task_id", models.CharField(blank=True, max_length=255)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("paper", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="ingestion_jobs", to="papers.paper")),
                ("project", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="ingestion_jobs", to="api.researchproject")),
            ],
            options={
                "ordering": ["-created_at", "-id"],
            },
        ),
        migrations.AddIndex(
            model_name="paperingestionjob",
            index=models.Index(fields=["project", "paper", "status"], name="api_paperin_project_b19d8d_idx"),
        ),
        migrations.AddIndex(
            model_name="paperingestionjob",
            index=models.Index(fields=["file_hash"], name="api_paperin_file_ha_75a3f1_idx"),
        ),
    ]
