"""0004: PaperIndexVersion + Text.index_version + legacy backfill.

Backfills every legacy Text chunk into an immutable version grouped by
(paper, embedding_model, embedding_version, embedding_dim). The group whose
embedding identity matches the CURRENT configured embedding metadata becomes
``active``; every other group becomes ``superseded``. No chunk is dropped,
deleted or re-embedded — all existing rows are fully assigned before the
``index_version`` field is made non-null and before the uniqueness constraint
is switched to ``(index_version, chunk_index)``.

ROLLBACK NOTE: the reverse migration removes the versioning data model and
restores the legacy ``(paper, chunk_index)`` uniqueness. It is only safe when
NO new-version chunks exist (a paper with two versions both containing
chunk_index=0 would break the legacy constraint). Once new versions exist,
production rollback MUST use a corrective forward migration instead — never
the plain reverse.
"""
from __future__ import annotations

import hashlib

from django.db import migrations, models

import rag.fields  # noqa: F401  (HybridVectorField import for deconstruction)


def backfill_versions(apps, schema_editor):
    """Deterministic legacy backfill (ING-B-fix P1).

    - Reads the ACTIVE embedding identity from settings CONSTANTS only — it
      never instantiates the embedding provider.
    - Groups legacy chunks by (paper, embedding_model, embedding_version,
      embedding_dim); the group matching the configured identity becomes
      ``active``, every other group becomes ``superseded``.
    - When the configured identity is missing/empty the result is
      deterministic: NO group is activated (all ``superseded``) so an
      ambiguous migration never silently activates a wrong index.
    - ``source_sha256`` is a digest over the group's ordered chunk content
      hashes — legacy provenance, never masquerading as a real file SHA.
    """
    Text = apps.get_model("rag", "Text")
    PaperIndexVersion = apps.get_model("rag", "PaperIndexVersion")

    from django.conf import settings

    active_model = str(getattr(settings, "PAPERLENS_EMBEDDING_MODEL", "") or "")
    active_version = str(getattr(settings, "PAPERLENS_EMBEDDING_VERSION", "") or "")
    active_dim = int(getattr(settings, "PAPERLENS_EMBEDDING_DIM", 0) or 0)
    config_present = bool(active_model and active_version and active_dim)

    rows = list(
        Text.objects.order_by("paper_id", "embedding_model",
                              "embedding_version", "embedding_dim", "id")
        .values("id", "paper_id", "embedding_model", "embedding_version",
                "embedding_dim", "content_hash"))

    groups: dict[tuple, list[dict]] = {}
    for row in rows:
        key = (row["paper_id"], row["embedding_model"],
               row["embedding_version"], row["embedding_dim"])
        groups.setdefault(key, []).append(row)

    def _legacy_sha(group_rows) -> str:
        ordered = sorted(
            (r["content_hash"] or f"legacy:{r['id']}" for r in group_rows))
        return hashlib.sha256(
            "\n".join(ordered).encode("utf-8")).hexdigest()[:64]

    for (paper_id, model, version, dim), group_rows in groups.items():
        is_active = (
            config_present
            and model == active_model
            and version == active_version
            and dim == active_dim)
        version_obj = PaperIndexVersion.objects.create(
            paper_id=paper_id,
            status="active" if is_active else "superseded",
            source_sha256=_legacy_sha(group_rows),
            pipeline_signature="legacy-backfill-v1",
            parser_identity="legacy-backfill",
            chunk_config_hash="legacy-backfill",
            embedding_model=model,
            embedding_version=version,
            embedding_dim=dim,
            chunk_count=len(group_rows),
        )
        Text.objects.filter(id__in=[r["id"] for r in group_rows]).update(
            index_version_id=version_obj.id)


class Migration(migrations.Migration):

    dependencies = [
        ("rag", "0003_bge_m3_sparse_weights"),
    ]

    operations = [
        migrations.CreateModel(
            name="PaperIndexVersion",
            fields=[
                ("id", models.BigAutoField(auto_created=True,
                                         primary_key=True, serialize=False,
                                         verbose_name="ID")),
                ("paper", models.ForeignKey(
                    on_delete=models.deletion.CASCADE,
                    related_name="index_versions",
                    to="papers.paper")),
                ("status", models.CharField(
                    choices=[("building", "Building"), ("active", "Active"),
                             ("superseded", "Superseded"),
                             ("failed", "Failed")],
                    default="building", max_length=16)),
                ("source_sha256", models.CharField(max_length=64)),
                ("pipeline_signature", models.CharField(max_length=64)),
                ("parser_identity", models.CharField(
                    blank=True, default="", max_length=160)),
                ("chunk_config_hash", models.CharField(
                    blank=True, default="", max_length=64)),
                ("embedding_model", models.CharField(
                    blank=True, max_length=160)),
                ("embedding_version", models.CharField(
                    blank=True, max_length=240)),
                ("embedding_dim", models.IntegerField(default=1024)),
                ("chunk_count", models.IntegerField(default=0)),
                ("error_code", models.CharField(
                    blank=True, default="", max_length=64)),
                ("error_hash", models.CharField(
                    blank=True, default="", max_length=32)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("activated_at", models.DateTimeField(blank=True,
                                                      null=True)),
                ("failed_at", models.DateTimeField(blank=True, null=True)),
            ],
            options={
                "ordering": ["-created_at", "-id"],
            },
        ),
        migrations.AddConstraint(
            model_name="paperindexversion",
            constraint=models.UniqueConstraint(
                fields=("paper", "source_sha256", "pipeline_signature"),
                name="uniq_paper_index_version_identity",
            ),
        ),
        migrations.AddConstraint(
            model_name="paperindexversion",
            constraint=models.UniqueConstraint(
                fields=("paper",),
                condition=models.Q(("status", "active")),
                name="uniq_paper_index_version_one_active",
            ),
        ),
        migrations.AddIndex(
            model_name="paperindexversion",
            index=models.Index(fields=["paper", "status"],
                               name="rag_piv_paper_st_idx"),
        ),
        migrations.AddIndex(
            model_name="paperindexversion",
            index=models.Index(fields=["embedding_model", "embedding_version"],
                               name="rag_piv_embeddi_idx"),
        ),
        migrations.AddField(
            model_name="text",
            name="index_version",
            field=models.ForeignKey(
                blank=True, null=True,
                on_delete=models.deletion.CASCADE,
                related_name="chunks", to="rag.paperindexversion"),
        ),
        migrations.RunPython(backfill_versions, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="text",
            name="index_version",
            field=models.ForeignKey(
                on_delete=models.deletion.CASCADE,
                related_name="chunks", to="rag.paperindexversion"),
        ),
        migrations.RemoveConstraint(
            model_name="text",
            name="uniq_text_paper_chunk",
        ),
        migrations.AddConstraint(
            model_name="text",
            constraint=models.UniqueConstraint(
                fields=("index_version", "chunk_index"),
                name="uniq_text_index_version_chunk"),
        ),
        migrations.AddIndex(
            model_name="text",
            index=models.Index(fields=["index_version"],
                               name="rag_text_index_v_d7c215_idx"),
        ),
    ]
