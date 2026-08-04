# Generated for PaperLens V3 hybrid RAG.

import rag.fields
from django.db import migrations, models


def enable_pgvector(apps, schema_editor):
    if schema_editor.connection.vendor == "postgresql":
        with schema_editor.connection.cursor() as cursor:
            cursor.execute("CREATE EXTENSION IF NOT EXISTS vector")


def alter_embedding_to_vector(apps, schema_editor):
    if schema_editor.connection.vendor == "postgresql":
        with schema_editor.connection.cursor() as cursor:
            cursor.execute(
                """
                ALTER TABLE rag_text
                ALTER COLUMN embedding TYPE vector(1024)
                USING (
                    CASE
                        WHEN jsonb_typeof(embedding) = 'array'
                         AND jsonb_array_length(embedding) = 1024
                        THEN embedding::text::vector
                        ELSE NULL
                    END
                )
                """
            )


def create_postgres_hybrid_indexes(apps, schema_editor):
    if schema_editor.connection.vendor == "postgresql":
        with schema_editor.connection.cursor() as cursor:
            cursor.execute(
                """
                CREATE INDEX IF NOT EXISTS rag_text_embedding_hnsw
                ON rag_text USING hnsw (embedding vector_cosine_ops)
                """
            )
            cursor.execute(
                """
                CREATE INDEX IF NOT EXISTS rag_text_search_vector_gin
                ON rag_text USING GIN (to_tsvector('english', search_vector))
                """
            )


def drop_postgres_hybrid_indexes(apps, schema_editor):
    if schema_editor.connection.vendor == "postgresql":
        with schema_editor.connection.cursor() as cursor:
            cursor.execute("DROP INDEX IF EXISTS rag_text_search_vector_gin")
            cursor.execute("DROP INDEX IF EXISTS rag_text_embedding_hnsw")


class Migration(migrations.Migration):

    dependencies = [
        ("rag", "0001_initial"),
    ]

    operations = [
        migrations.RunPython(enable_pgvector, migrations.RunPython.noop),
        migrations.SeparateDatabaseAndState(
            database_operations=[
                migrations.RunPython(alter_embedding_to_vector, migrations.RunPython.noop),
            ],
            state_operations=[
                migrations.AlterField(
                    model_name="text",
                    name="embedding",
                    field=rag.fields.HybridVectorField(default=list, dimensions=1024),
                ),
            ],
        ),
        migrations.AddField(
            model_name="text",
            name="embedding_model",
            field=models.CharField(blank=True, max_length=160),
        ),
        migrations.AddField(
            model_name="text",
            name="embedding_dim",
            field=models.IntegerField(default=1024),
        ),
        migrations.AddField(
            model_name="text",
            name="embedding_version",
            field=models.CharField(blank=True, max_length=240),
        ),
        migrations.AddField(
            model_name="text",
            name="content_hash",
            field=models.CharField(blank=True, db_index=True, max_length=64),
        ),
        migrations.AddField(
            model_name="text",
            name="page_start",
            field=models.IntegerField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="text",
            name="page_end",
            field=models.IntegerField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="text",
            name="section",
            field=models.CharField(blank=True, max_length=240),
        ),
        migrations.AddField(
            model_name="text",
            name="char_start",
            field=models.IntegerField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="text",
            name="char_end",
            field=models.IntegerField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="text",
            name="search_vector",
            field=models.TextField(blank=True),
        ),
        migrations.AddField(
            model_name="text",
            name="indexed_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddIndex(
            model_name="text",
            index=models.Index(fields=["paper", "chunk_index"], name="rag_text_paper_i_c9e3bb_idx"),
        ),
        migrations.AddIndex(
            model_name="text",
            index=models.Index(fields=["content_hash"], name="rag_text_content_5b09bb_idx"),
        ),
        migrations.AddIndex(
            model_name="text",
            index=models.Index(fields=["embedding_model", "embedding_version"], name="rag_text_embeddi_f9c262_idx"),
        ),
        migrations.RunPython(create_postgres_hybrid_indexes, drop_postgres_hybrid_indexes),
    ]
