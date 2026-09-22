import django.db.models.deletion
from django.db import migrations, models
from django.utils import timezone


def close_legacy_running_runs(apps, schema_editor):
    SyncRun = apps.get_model("core", "SyncRun")
    SyncRun.objects.filter(status="running").update(
        status="failed",
        failure_stage="unknown",
        failure_detail="Closed while installing the incremental sync protocol.",
        finished_at=timezone.now(),
    )


class Migration(migrations.Migration):
    dependencies = [("core", "0055_global_identity_upload_pipeline")]

    operations = [
        migrations.AddField(
            model_name="syncconfig",
            name="product_cursor_column",
            field=models.CharField(blank=True, default="", max_length=255),
        ),
        migrations.AddField(
            model_name="syncconfig",
            name="product_cursor_value",
            field=models.JSONField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="syncconfig",
            name="user_cursor_column",
            field=models.CharField(blank=True, default="", max_length=255),
        ),
        migrations.AddField(
            model_name="syncconfig",
            name="user_cursor_value",
            field=models.JSONField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="syncrun",
            name="client_run_id",
            field=models.UUIDField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="syncrun",
            name="instance_id",
            field=models.UUIDField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="syncrun",
            name="lease_expires_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="syncrun",
            name="customers_upload_job",
            field=models.OneToOneField(
                blank=True, null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="customer_sync_run", to="core.uploadjob",
            ),
        ),
        migrations.AddField(
            model_name="syncrun",
            name="products_upload_job",
            field=models.OneToOneField(
                blank=True, null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="product_sync_run", to="core.uploadjob",
            ),
        ),
        migrations.RunPython(close_legacy_running_runs, migrations.RunPython.noop),
        migrations.AddConstraint(
            model_name="syncrun",
            constraint=models.UniqueConstraint(
                fields=("tenant", "client_run_id"),
                name="uq_sync_run_tenant_client_run",
            ),
        ),
        migrations.AddConstraint(
            model_name="syncrun",
            constraint=models.UniqueConstraint(
                condition=models.Q(("status", "running")),
                fields=("tenant",),
                name="uq_sync_run_one_running_tenant",
            ),
        ),
        migrations.CreateModel(
            name="SyncBatch",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("entity", models.CharField(choices=[("user", "User"), ("product", "Product")], max_length=10)),
                ("batch_number", models.PositiveIntegerField()),
                ("idempotency_key", models.CharField(max_length=200, unique=True)),
                ("payload_hash", models.CharField(max_length=64)),
                ("cursor_after", models.JSONField(blank=True, null=True)),
                ("response_payload", models.JSONField(default=dict)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("run", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="batches", to="core.syncrun")),
            ],
            options={
                "ordering": ["entity", "batch_number"],
                "indexes": [models.Index(fields=["run", "entity"], name="core_syncba_run_id_828a16_idx")],
                "constraints": [models.UniqueConstraint(fields=("run", "entity", "batch_number"), name="uq_sync_batch_run_entity_number")],
            },
        ),
    ]
