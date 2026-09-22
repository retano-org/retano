"""Run ownership, retry idempotency, and atomic finalization for ETL sync."""

import hashlib
import json
import uuid
from datetime import timedelta

from django.conf import settings
from django.db import connection, transaction
from django.db.models import Max
from django.utils import timezone

from core.models import (
    ProductsUnNormalizedDataStaging,
    SyncBatch,
    SyncConfig,
    SyncRun,
    UploadJob,
    UsersUnNormalizedDataStaging,
)
from core.services.sync_pipeline import ingest_product_rows, ingest_user_rows
from core.utils.analytics_cache import schedule_analytics_refresh


class SyncProtocolError(Exception):
    def __init__(self, message, *, code="sync_protocol_error", status_code=409):
        super().__init__(message)
        self.code = code
        self.status_code = status_code


def parse_run_headers(request):
    try:
        instance_id = uuid.UUID(request.headers.get("X-Sync-Instance-ID", ""))
        run_id = uuid.UUID(request.headers.get("X-Sync-Run-ID", ""))
    except (TypeError, ValueError):
        raise SyncProtocolError(
            "X-Sync-Instance-ID and X-Sync-Run-ID must be valid UUIDs.",
            code="invalid_run_headers", status_code=400,
        )
    return instance_id, run_id


def _deadline():
    seconds = int(getattr(settings, "SYNC_LEASE_SECONDS", 21600))
    return timezone.now() + timedelta(seconds=max(seconds, 60))


def _delete_staging(run):
    if run.customers_upload_job_id:
        UsersUnNormalizedDataStaging.objects.filter(
            upload_job_id=run.customers_upload_job_id
        ).delete()
    if run.products_upload_job_id:
        ProductsUnNormalizedDataStaging.objects.filter(
            upload_job_id=run.products_upload_job_id
        ).delete()


def _fail_run(run, detail):
    _delete_staging(run)
    now = timezone.now()
    for job in (run.customers_upload_job, run.products_upload_job):
        if job:
            job.status = UploadJob.Status.FAILED
            job.error_type = "sync_failed"
            job.message = detail
            job.save(update_fields=["status", "error_type", "message", "updated_at"])
    run.status = "failed"
    run.failure_stage = "unknown"
    run.failure_detail = detail
    run.finished_at = now
    run.lease_expires_at = now
    run.save(update_fields=[
        "status", "failure_stage", "failure_detail", "finished_at",
        "lease_expires_at",
    ])


@transaction.atomic
def begin_run(tenant, sync_config, instance_id, run_id):
    config = SyncConfig.objects.select_for_update().get(pk=sync_config.pk)
    if not config.user_cursor_column.strip() or not config.product_cursor_column.strip():
        raise SyncProtocolError(
            "Incremental cursor columns are not configured for this tenant.",
            code="cursor_incomplete", status_code=409,
        )
    existing = SyncRun.objects.select_for_update().filter(
        tenant=tenant, client_run_id=run_id
    ).first()
    if existing:
        if existing.instance_id != instance_id:
            raise SyncProtocolError("This run ID belongs to another ETL instance.")
        if existing.status != "running":
            raise SyncProtocolError("This run ID has already finished.", code="run_finished")
        existing.lease_expires_at = _deadline()
        existing.save(update_fields=["lease_expires_at"])
        return config, existing

    active = SyncRun.objects.select_for_update().filter(
        tenant=tenant, status="running"
    ).first()
    if active:
        if active.lease_expires_at and active.lease_expires_at > timezone.now():
            raise SyncProtocolError(
                "Another sync instance currently holds this tenant's lease.",
                code="sync_already_running",
            )
        _fail_run(active, "The ETL lease expired before the run was finalized.")

    customers_job = UploadJob.objects.create(
        tenant=tenant,
        upload_type=UploadJob.UploadType.CUSTOMERS,
        status=UploadJob.Status.PROCESSING,
        storage_key=f"sync://{run_id}/customers",
        original_filename="automated-sync",
        mapping={"source": "automated_sync"},
    )
    products_job = UploadJob.objects.create(
        tenant=tenant,
        upload_type=UploadJob.UploadType.PRODUCTS,
        status=UploadJob.Status.PROCESSING,
        storage_key=f"sync://{run_id}/products",
        original_filename="automated-sync",
        mapping={"source": "automated_sync"},
    )
    run = SyncRun.objects.create(
        tenant=tenant, client_run_id=run_id, instance_id=instance_id,
        lease_expires_at=_deadline(), customers_upload_job=customers_job,
        products_upload_job=products_job,
    )
    return config, run


def _payload_hash(entity, batch_number, cursor_after, rows):
    canonical = json.dumps(
        {"entity": entity, "batch_number": batch_number,
         "cursor_after": cursor_after, "rows": rows},
        sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@transaction.atomic
def ingest_batch(tenant, instance_id, run_id, entity, batch_number,
                 idempotency_key, cursor_after, rows):
    run = SyncRun.objects.select_for_update().select_related(
        "customers_upload_job", "products_upload_job"
    ).filter(tenant=tenant, client_run_id=run_id).first()
    if not run or run.instance_id != instance_id or run.status != "running":
        raise SyncProtocolError("The sync run is not active.", code="run_not_active")

    digest = _payload_hash(entity, batch_number, cursor_after, rows)
    existing = SyncBatch.objects.filter(idempotency_key=idempotency_key).first()
    numbered = SyncBatch.objects.filter(
        run=run, entity=entity, batch_number=batch_number
    ).first()
    replay = existing or numbered
    if replay:
        if replay.run_id != run.id or replay.entity != entity or replay.payload_hash != digest:
            raise SyncProtocolError(
                "An idempotency key or batch number was reused with different data.",
                code="idempotency_conflict",
            )
        run.lease_expires_at = _deadline()
        run.save(update_fields=["lease_expires_at"])
        return replay.response_payload

    last_number = SyncBatch.objects.filter(run=run, entity=entity).aggregate(
        value=Max("batch_number")
    )["value"] or 0
    if batch_number != last_number + 1:
        raise SyncProtocolError(
            f"Expected batch {last_number + 1} for {entity}, got {batch_number}.",
            code="batch_out_of_order",
        )
    if rows and cursor_after is None:
        raise SyncProtocolError(
            "cursor_after is required for a non-empty batch.",
            code="missing_cursor", status_code=400,
        )

    job = run.customers_upload_job if entity == "user" else run.products_upload_job
    result = (
        ingest_user_rows(tenant, job, rows)
        if entity == "user"
        else ingest_product_rows(tenant, job, rows)
    )
    response = result.as_dict()
    SyncBatch.objects.create(
        run=run, entity=entity, batch_number=batch_number,
        idempotency_key=idempotency_key, payload_hash=digest,
        cursor_after=cursor_after, response_payload=response,
    )
    job.total_rows = (job.total_rows or 0) + result.rows_received
    job.processed_rows += result.rows_received
    job.save(update_fields=["total_rows", "processed_rows", "updated_at"])
    run.lease_expires_at = _deadline()
    run.save(update_fields=["lease_expires_at"])
    return response


def _batch_totals(run, entity):
    totals = {"received": 0, "accepted": 0, "rejected": 0}
    for payload in run.batches.filter(entity=entity).values_list(
        "response_payload", flat=True
    ):
        totals["received"] += int(payload.get("rows_received", 0))
        totals["accepted"] += int(payload.get("rows_accepted", 0))
        totals["rejected"] += int(payload.get("rows_rejected", 0))
    return totals


def _flush_job(job, function_name, rejected):
    timeout_ms = int(getattr(settings, "UPLOAD_DB_STATEMENT_TIMEOUT_MS", 600000))
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT set_config('statement_timeout', %s, true)", [str(timeout_ms)]
        )
        cursor.execute("SELECT allocate_upload_job_ids(%s)", [job.id])
        cursor.execute(f"SELECT {function_name}(%s)", [job.id])
        rows_saved = cursor.fetchone()[0] or 0
    job.rows_saved = rows_saved
    job.status = UploadJob.Status.PARTIAL if rejected else UploadJob.Status.SUCCESS
    job.message = f"{rows_saved} automated sync rows committed."
    job.save(update_fields=["rows_saved", "status", "message", "updated_at"])
    return rows_saved


def _terminal_response(run):
    return {
        "message": "Report recorded.",
        "run_id": str(run.client_run_id),
        "status": run.status,
    }


@transaction.atomic
def finalize_run(tenant, instance_id, run_id, report):
    # Keep lock ordering identical to begin_run(): config first, then run.
    config = SyncConfig.objects.select_for_update().get(tenant=tenant)
    run = SyncRun.objects.select_for_update().select_related(
        "customers_upload_job", "products_upload_job"
    ).filter(tenant=tenant, client_run_id=run_id).first()
    if not run or run.instance_id != instance_id:
        raise SyncProtocolError("The sync run does not exist.", code="run_not_found")
    if run.status != "running":
        return _terminal_response(run)

    if report["status"] == "failed":
        _delete_staging(run)
        for job in (run.customers_upload_job, run.products_upload_job):
            job.status = UploadJob.Status.FAILED
            job.error_type = "sync_failed"
            job.message = report.get("failure_detail", "")
            job.save(update_fields=["status", "error_type", "message", "updated_at"])
        run.status = "failed"
        run.failure_stage = report.get("failure_stage") or "unknown"
        run.failure_detail = report.get("failure_detail", "")
    else:
        users = _batch_totals(run, "user")
        products = _batch_totals(run, "product")
        users_saved = _flush_job(
            run.customers_upload_job, "flush_customers_upload_job",
            users["rejected"],
        )
        products_saved = _flush_job(
            run.products_upload_job, "flush_products_upload_job",
            products["rejected"],
        )
        if users_saved or products_saved:
            transaction.on_commit(
                lambda tenant_id=tenant.id: schedule_analytics_refresh(tenant_id)
            )

        last_users = run.batches.filter(entity="user").order_by("-batch_number").first()
        last_products = run.batches.filter(entity="product").order_by("-batch_number").first()
        update_fields = ["updated_at"]
        if last_users:
            config.user_cursor_value = last_users.cursor_after
            update_fields.append("user_cursor_value")
        if last_products:
            config.product_cursor_value = last_products.cursor_after
            update_fields.append("product_cursor_value")
        config.save(update_fields=update_fields)

        run.users_rows_received = users["received"]
        run.users_rows_accepted = users["accepted"]
        run.users_rows_rejected = users["rejected"]
        run.products_rows_received = products["received"]
        run.products_rows_accepted = products["accepted"]
        run.products_rows_rejected = products["rejected"]
        run.status = (
            "partial" if users["rejected"] + products["rejected"] else "success"
        )

    run.finished_at = timezone.now()
    run.lease_expires_at = run.finished_at
    run.save(update_fields=[
        "status", "failure_stage", "failure_detail",
        "users_rows_received", "users_rows_accepted", "users_rows_rejected",
        "products_rows_received", "products_rows_accepted",
        "products_rows_rejected", "finished_at", "lease_expires_at",
    ])
    return _terminal_response(run)
