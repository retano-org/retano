from pathlib import Path

import django.db.models.deletion
from django.db import migrations, models


def install_sql(apps, schema_editor):
    sql = Path(__file__).resolve().parents[2] / "sql" / "manual_upload_replacement.sql"
    with schema_editor.connection.cursor() as cursor:
        cursor.execute(sql.read_text(encoding="utf-8"))


def uninstall_sql(apps, schema_editor):
    with schema_editor.connection.cursor() as cursor:
        cursor.execute("""
            DROP TRIGGER IF EXISTS retano_archive_campaign_send ON public.trigger_results;
            DROP FUNCTION IF EXISTS public.retano_archive_campaign_send();
            DROP FUNCTION IF EXISTS public.retano_replace_manual_upload_data(bigint, boolean, uuid);
        """)


class Migration(migrations.Migration):
    dependencies = [("core", "0056_sync_incremental_runs")]

    operations = [
        migrations.CreateModel(
            name="ManualUploadPair",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("status", models.CharField(max_length=20, choices=[
                    ("products_processing", "Products processing"),
                    ("awaiting_customers", "Awaiting customers"),
                    ("customers_processing", "Customers processing"),
                    ("ready", "Ready for post-upload SQL"),
                    ("complete", "Complete"),
                    ("expired", "Expired"),
                    ("failed", "Failed"),
                    ("superseded", "Superseded"),
                ])),
                ("customers_due_at", models.DateTimeField(blank=True, null=True)),
                ("completed_at", models.DateTimeField(blank=True, null=True)),
                ("cache_queued_at", models.DateTimeField(blank=True, null=True)),
                ("customers_job", models.OneToOneField(
                    blank=True, null=True, on_delete=django.db.models.deletion.RESTRICT,
                    related_name="manual_customers_pair", to="core.uploadjob",
                )),
                ("products_job", models.OneToOneField(
                    on_delete=django.db.models.deletion.RESTRICT,
                    related_name="manual_products_pair", to="core.uploadjob",
                )),
                ("tenant", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, to="core.tenant")),
            ],
            options={
                "indexes": [
                    models.Index(fields=["status", "customers_due_at"], name="manual_pair_due_idx"),
                ],
            },
        ),
        migrations.CreateModel(
            name="SentCampaignMessage",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("trigger_result_id", models.BigIntegerField()),
                ("tenant_id", models.IntegerField()),
                ("user_id", models.TextField()),
                ("rule_id", models.BigIntegerField(db_index=True)),
                ("sent_at", models.DateTimeField()),
                ("sms_message_id", models.TextField(null=True)),
                ("delivered_at", models.DateTimeField(null=True)),
                ("snapshot", models.JSONField()),
            ],
            options={
                "constraints": [
                    models.UniqueConstraint(
                        fields=["trigger_result_id", "sent_at"], name="unique_archived_campaign_send",
                    ),
                ],
            },
        ),
        migrations.RunPython(install_sql, uninstall_sql),
    ]
