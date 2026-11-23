from datetime import timedelta

from django.db import connection, transaction
from django.utils import timezone

from core.models import ManualUploadPair, Tenant, UploadJob
from core.services.storage import upload_fileobj_to_storage

CUSTOMER_UPLOAD_WINDOW = timedelta(minutes=60)
PAIR_BUSY = (
    ManualUploadPair.Status.PRODUCTS_PROCESSING,
    ManualUploadPair.Status.CUSTOMERS_PROCESSING,
    ManualUploadPair.Status.READY,
)
POST_UPLOAD_FUNCTIONS = (
    "refresh_user_summary_rfm_metrics",
    "refresh_buying_power",
    "refresh_rfm_materialized_view",
    "refresh_rfm_scores",
    "refresh_product_co_purchase",
    "update_user_summary_recommendations",
    "update_user_top_products",
    "process_campaign_eligibility",
    "process_campaign_send_dates",
    "fill_final_messages",
)


class ManualUploadConflict(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def lock_tenant(tenant_id):
    Tenant.objects.select_for_update(no_key=True).only("id").get(pk=tenant_id)


@transaction.atomic
def queue_manual_upload(tenant, upload_type, uploaded_file, mapping, task):
    lock_tenant(tenant.pk)
    pairs = ManualUploadPair.objects.filter(tenant=tenant)
    if pairs.filter(status__in=PAIR_BUSY).exists():
        raise ManualUploadConflict(
            "upload_in_progress",
            "پردازش فایل محصولات یا مشتریان شما هنوز به پایان نرسیده است.",
        )

    is_customer = upload_type == UploadJob.UploadType.CUSTOMERS
    if is_customer:
        pair = pairs.filter(
            status=ManualUploadPair.Status.AWAITING_CUSTOMERS,
            customers_due_at__gt=timezone.now(),
        ).first()
        if pair is None:
            raise ManualUploadConflict(
                "product_upload_required",
                "فایل مشتریان باید حداکثر تا ۶۰ دقیقه پس از پایان موفق پردازش فایل محصولات ارسال شود.",
            )

    storage_key = upload_fileobj_to_storage(
        uploaded_file, tenant.pk, upload_type, uploaded_file.name
    )
    job = UploadJob.objects.create(
        tenant=tenant, upload_type=upload_type, storage_key=storage_key,
        original_filename=uploaded_file.name, mapping=mapping,
    )
    if is_customer:
        pair.customers_job = job
        pair.status = ManualUploadPair.Status.CUSTOMERS_PROCESSING
        pair.save(update_fields=["customers_job", "status"])
    else:
        ManualUploadPair.objects.create(
            tenant=tenant, products_job=job,
            status=ManualUploadPair.Status.PRODUCTS_PROCESSING,
        )
    transaction.on_commit(lambda: task.delay(str(job.pk)), robust=True)
    return job


def pair_for_job(job):
    if job.upload_type == UploadJob.UploadType.CUSTOMERS:
        return ManualUploadPair.objects.filter(customers_job=job).first()
    if job.upload_type == UploadJob.UploadType.PRODUCTS:
        return ManualUploadPair.objects.filter(products_job=job).first()
    return None


def claim_pair(job):
    lock_tenant(job.tenant_id)
    pair = pair_for_job(job)
    if pair is None:
        return None
    expected_status = {
        UploadJob.UploadType.CUSTOMERS: ManualUploadPair.Status.CUSTOMERS_PROCESSING,
        UploadJob.UploadType.PRODUCTS: ManualUploadPair.Status.PRODUCTS_PROCESSING,
    }[job.upload_type]
    if pair.status != expected_status:
        raise ManualUploadConflict("upload_inactive", "This upload is no longer active.")
    return pair


def delete_previous_manual_data(cursor, pair, *, expires=False):
    replace_products = expires or pair.status == ManualUploadPair.Status.PRODUCTS_PROCESSING
    cursor.execute(
        "SELECT public.retano_replace_manual_upload_data(%s, %s, %s)",
        [pair.tenant_id, replace_products, pair.products_job_id if expires else None],
    )


def mark_pair_imported(pair):
    if pair.status == ManualUploadPair.Status.PRODUCTS_PROCESSING:
        ManualUploadPair.objects.filter(
            tenant_id=pair.tenant_id, status=ManualUploadPair.Status.AWAITING_CUSTOMERS,
        ).update(status=ManualUploadPair.Status.SUPERSEDED)
        pair.status = ManualUploadPair.Status.AWAITING_CUSTOMERS
        pair.customers_due_at = timezone.now() + CUSTOMER_UPLOAD_WINDOW
    else:
        pair.status = ManualUploadPair.Status.READY
    pair.save(update_fields=["status", "customers_due_at"])


def mark_pair_failed(pair):
    pair.status = (
        ManualUploadPair.Status.AWAITING_CUSTOMERS
        if pair.status == ManualUploadPair.Status.CUSTOMERS_PROCESSING
        else ManualUploadPair.Status.FAILED
    )
    pair.save(update_fields=["status"])


@transaction.atomic
def expire_product_upload(pair_id, tenant_id):
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_try_advisory_xact_lock_shared(873421, 1)")
        if not cursor.fetchone()[0]:
            return
    lock_tenant(tenant_id)
    pair = ManualUploadPair.objects.filter(
        pk=pair_id, status=ManualUploadPair.Status.AWAITING_CUSTOMERS,
        customers_due_at__lte=timezone.now(),
    ).first()
    if pair is None:
        return
    with connection.cursor() as cursor:
        delete_previous_manual_data(cursor, pair, expires=True)
    pair.status = ManualUploadPair.Status.EXPIRED
    pair.save(update_fields=["status"])


@transaction.atomic
def run_post_upload_sql(pair_id, tenant_id, timeout_ms):
    with connection.cursor() as cursor:
        # Both staging tables must stay empty for the entire global SQL run.
        # The nonblocking locks let staging writers finish before the next attempt.
        cursor.execute("SELECT pg_try_advisory_xact_lock(873421, 1)")
        if not cursor.fetchone()[0]:
            return False
        lock_tenant(tenant_id)
        pair = ManualUploadPair.objects.filter(
            pk=pair_id, status=ManualUploadPair.Status.READY,
        ).first()
        if pair is None:
            return False
        cursor.execute("""
            LOCK TABLE public.users_unnormalized_data_staging,
                       public.products_unnormalized_data_staging IN SHARE MODE NOWAIT
        """)
        cursor.execute("""
            SELECT EXISTS (SELECT 1 FROM public.users_unnormalized_data_staging)
                OR EXISTS (SELECT 1 FROM public.products_unnormalized_data_staging)
        """)
        if cursor.fetchone()[0]:
            return False
        cursor.execute(
            """
            SELECT EXISTS (
                       SELECT 1 FROM public.users_unnormalized_data f
                       JOIN public.users u ON u.user_id = f.user_id::text
                       WHERE f.tenant_id = %s AND f.column_mapping->>'upload_job_id' = %s
                   )
               AND EXISTS (
                       SELECT 1 FROM public.products_unnormalized_data f
                       JOIN public.products p ON p.product_id = f.product_id::text
                       WHERE f.tenant_id = %s AND f.column_mapping->>'upload_job_id' = %s
                   )
            """,
            [tenant_id, str(pair.customers_job_id), tenant_id, str(pair.products_job_id)],
        )
        if not cursor.fetchone()[0]:
            return False
        cursor.execute("SELECT set_config('statement_timeout', %s, true)", [str(timeout_ms)])
        for function in POST_UPLOAD_FUNCTIONS:
            cursor.execute(f"SELECT public.{function}()")

    pair.status = ManualUploadPair.Status.COMPLETE
    pair.completed_at = timezone.now()
    pair.save(update_fields=["status", "completed_at"])
    return True
