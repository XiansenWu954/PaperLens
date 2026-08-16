"""P2-GLM-01: private ingestion execution lease fields.

Additive nullable/blank columns; legacy rows default to an empty token and
no lease, which means "no live attempt" and is claimable. Backward
migration simply drops the three columns.
"""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("api", "0007_workflow_lifecycle_timestamps"),
    ]

    operations = [
        migrations.AddField(
            model_name="paperingestionjob",
            name="execution_token",
            field=models.CharField(
                blank=True, default="", max_length=128),
        ),
        migrations.AddField(
            model_name="paperingestionjob",
            name="execution_heartbeat_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="paperingestionjob",
            name="execution_lease_expires_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
