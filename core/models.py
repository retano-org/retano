# core/models.py

import hashlib
import secrets
import uuid

from django.conf import settings
from django.db import models
from django.db.models.signals import post_save
from django.dispatch import receiver
from django.utils import timezone


@receiver(post_save, sender=settings.AUTH_USER_MODEL)
def create_tenant_for_new_user(sender, instance, created, **kwargs):
    if created:
        Tenant.objects.get_or_create(owner=instance)


class Tenant(models.Model):
    owner = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)

    def __str__(self):
        return f"Tenant #{self.id} — {self.owner}"


class UploadJob(models.Model):
    """
    Tracks the lifecycle of one asynchronous Excel upload (customers,
    products, or coupons) so the frontend can poll for real progress
    instead of the request blocking until the import finishes.

    Lifecycle:
        queued      → task has been created and handed to Celery, worker
                      has not started reading the file yet
        processing  → worker is actively reading/COPYing rows;
                      processed_rows / total_rows updates as it goes
        success     → flush_*_staging completed, rows_saved is final
        partial     → flush completed but some rows were skipped
                      (missing required fields) — mirrors the old
                      per-row "if not internal_id: continue" behavior
        failed      → error_type / message explain why, exactly like the
                      old synchronous error response shape
    """

    class Status(models.TextChoices):
        QUEUED = "queued", "Queued"
        PROCESSING = "processing", "Processing"
        SUCCESS = "success", "Success"
        PARTIAL = "partial", "Partial"
        FAILED = "failed", "Failed"

    class UploadType(models.TextChoices):
        CUSTOMERS = "customers", "Customers"
        PRODUCTS = "products", "Products"
        COUPONS = "coupons", "Coupons"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.ForeignKey(
        "core.Tenant", on_delete=models.CASCADE, related_name="upload_jobs"
    )
    upload_type = models.CharField(max_length=20, choices=UploadType.choices)
    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.QUEUED
    )

    # Storage location of the originally uploaded file (Supabase Storage key,
    # not a local path — the worker may run on a different machine than the
    # web process that accepted the upload).
    storage_key = models.CharField(max_length=500)
    original_filename = models.CharField(max_length=255, blank=True, default="")

    # Same shape as the old customers_mapping / products_mapping /
    # coupons_mapping dicts — field name -> zero-based column index.
    mapping = models.JSONField()
    column_headers = models.JSONField(default=list, blank=True)

    total_rows = models.PositiveIntegerField(null=True, blank=True)
    processed_rows = models.PositiveIntegerField(default=0)
    rows_saved = models.PositiveIntegerField(default=0)

    error_type = models.CharField(max_length=50, null=True, blank=True)
    message = models.TextField(blank=True, default="")

    celery_task_id = models.CharField(max_length=155, null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "upload_job"
        indexes = [
            models.Index(fields=["tenant", "status"]),
        ]
        ordering = ["-created_at"]

    def __str__(self):
        return f"UploadJob({self.id}, {self.upload_type}, {self.status})"

    @property
    def progress_percentage(self) -> float:
        if not self.total_rows:
            return 0.0
        return round(
            min(self.processed_rows, self.total_rows) / self.total_rows * 100, 2
        )

    def to_status_dict(self) -> dict:
        """Shape returned by GET /api/v1/uploads/jobs/{id}/"""
        return {
            "job_id": str(self.id),
            "upload_type": self.upload_type,
            "status": self.status,
            "total_rows": self.total_rows,
            "processed_rows": self.processed_rows,
            "rows_saved": self.rows_saved,
            "progress_percentage": self.progress_percentage,
            "error_type": self.error_type,
            "message": self.message,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
        }


# ─────────────────────────────────────────────────────────────────────────────
# Coupon
# ─────────────────────────────────────────────────────────────────────────────


class Coupon(models.Model):
    status = models.CharField(default="available")
    coupon_code = models.TextField(unique=True, max_length=2000)
    discount_percentage = models.DecimalField(max_digits=5, decimal_places=2)
    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE)

    def __str__(self):
        return f"Coupon {self.coupon_code} | Tenant {self.tenant_id}"


# ─────────────────────────────────────────────────────────────────────────────
# File Upload Models
# ─────────────────────────────────────────────────────────────────────────────


class CustomerFileUpload(models.Model):
    upload_job = models.OneToOneField(
        UploadJob,
        on_delete=models.SET_NULL,
        related_name="customer_upload_record",
        null=True,
        blank=True,
    )
    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE)
    customers_file = models.FileField(upload_to="campaign_customers/")
    customers_mapping = models.JSONField(
        default=dict,
        help_text=(
            "Maps field names to zero-based column indices "
            "in the customers Excel file."
        ),
    )
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"CustomerFileUpload #{self.id} | Tenant {self.tenant_id}"


class ProductFileUpload(models.Model):
    upload_job = models.OneToOneField(
        UploadJob,
        on_delete=models.SET_NULL,
        related_name="product_upload_record",
        null=True,
        blank=True,
    )
    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE)
    products_file = models.FileField(upload_to="campaign_products/")
    products_mapping = models.JSONField(
        default=dict,
        help_text=(
            "Maps field names to zero-based column indices "
            "in the products Excel file."
        ),
    )
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"ProductFileUpload #{self.id} | Tenant {self.tenant_id}"


class CouponFileUpload(models.Model):
    upload_job = models.OneToOneField(
        UploadJob,
        on_delete=models.SET_NULL,
        related_name="coupon_upload_record",
        null=True,
        blank=True,
    )
    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE)
    coupons_file = models.FileField(upload_to="campaign_coupons/")
    coupons_mapping = models.JSONField(
        default=dict,
        help_text=(
            "Maps field names to zero-based column indices "
            "in the coupons Excel file."
        ),
    )
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"CouponFileUpload #{self.id} | Tenant {self.tenant_id}"


# ─────────────────────────────────────────────────────────────────────────────
# Campaign
# ─────────────────────────────────────────────────────────────────────────────


class Campaign(models.Model):

    COUPON_DISCOUNT_PERCENTAGE_CHOICES = [(i, f"{i}%") for i in range(0, 101)]

    ACTIVATION_BASE_CHOICES = [
        ("همیشه", "همیشه"),
        ("آخرین خرید", "آخرین خرید"),
        ("اولین خرید", "اولین خرید"),
        ("یادآوری خرید بعدی", "یادآوری خرید بعدی"),
        ("قدرت خرید", "قدرت خرید"),
    ]

    COMPARISON_TYPE_CHOICES = [
        ("بزرگتر از", "بزرگتر از"),
        ("کوچکتر از", "کوچکتر از"),
        ("برابر با", "برابر با"),
    ]

    VALUE_UNIT_CHOICES = [
        ("روز", "روز"),
        ("تومان", "تومان"),
    ]

    GENDER_CHOICES = [
        ("آقایان", "آقایان"),
        ("بانوان", "بانوان"),
        ("همه", "همه"),
    ]

    BUYING_POWER_CHOICES = [
        ("همه", "همه"),
        ("خیلی بالا", "خیلی بالا"),
        ("بالا", "بالا"),
        ("متوسط", "متوسط"),
        ("پایین", "پایین"),
        ("خیلی پایین", "خیلی پایین"),
    ]

    PRIORITIES_CHOICES = [
        ("خیلی بالا", "خیلی بالا"),
        ("بالا", "بالا"),
        ("متوسط", "متوسط"),
        ("پایین", "پایین"),
        ("خیلی پایین", "خیلی پایین"),
    ]

    CUSTOMER_TYPE_CHOICES = [
        ("همه", "همه"),
        ("ویژه", "ویژه"),
        ("فعال", "فعال"),
        ("تازه وارد", "تازه وارد"),
        ("در خطر ریزش", "در خطر ریزش"),
        ("از دست رفته", "از دست رفته"),
    ]

    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE)
    name = models.CharField(max_length=255)
    rule_number = models.IntegerField(editable=False)

    coupon_discount_percentage = models.DecimalField(
        choices=COUPON_DISCOUNT_PERCENTAGE_CHOICES,
        max_digits=5,
        decimal_places=2,
        null=True,
        blank=True,
    )

    campaign_start_date = models.DateField(null=True, blank=True)
    campaign_end_date = models.DateField(null=True, blank=True)
    send_sms_time = models.TimeField(default="11:00:00")

    activation_base = models.CharField(
        blank=True,
        null=True,
        max_length=50,
        choices=ACTIVATION_BASE_CHOICES,
        default="همیشه",
    )
    comparison_type = models.CharField(
        null=True,
        blank=True,
        max_length=50,
        choices=COMPARISON_TYPE_CHOICES,
        default="بزرگتر از",
    )
    comparison_value = models.IntegerField(null=True, blank=True, default=1)
    value_unit = models.CharField(
        null=True,
        blank=True,
        max_length=50,
        choices=VALUE_UNIT_CHOICES,
        default="روز",
    )
    priority = models.CharField(
        max_length=20,
        choices=PRIORITIES_CHOICES,
        default="خیلی بالا",
    )
    buying_power = models.CharField(
        max_length=20,
        choices=BUYING_POWER_CHOICES,
        default="همه",
    )
    customer_type = models.CharField(
        max_length=50,
        choices=CUSTOMER_TYPE_CHOICES,
        default="همه",
    )
    gender = models.CharField(
        max_length=10,
        choices=GENDER_CHOICES,
        default="همه",
    )

    first_product_attribute = models.CharField(
        max_length=255,
        default="همه",
        blank=True,
        verbose_name="ویژگی اول محصول",
    )
    second_product_attribute = models.CharField(
        max_length=255,
        default="همه",
        blank=True,
        verbose_name="ویژگی دوم محصول",
    )

    is_active = models.BooleanField(default=True)
    description = models.TextField(blank=True, default="")
    message_pattern = models.TextField(default="الگوی پیام")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [
            models.Index(fields=["tenant_id"]),
        ]

    def save(self, *args, **kwargs):
        if not self.pk:
            last_rule = (
                Campaign.objects.filter(tenant_id=self.tenant_id)
                .order_by("-rule_number")
                .first()
            )
            self.rule_number = last_rule.rule_number + 1 if last_rule else 1
        super().save(*args, **kwargs)

    def __str__(self):
        return f"Campaign {self.rule_number} | Tenant {self.tenant_id}"


# ─────────────────────────────────────────────────────────────────────────────
# Users flat store  (customers-file pipeline)
# ─────────────────────────────────────────────────────────────────────────────


class UsersUnNormalizedData(models.Model):
    """
    Permanent flat store for the customers-file pipeline.
    All three public IDs are assigned from persistent global identity maps.
    Product metadata may arrive later, but product_id is established by the
    customer upload itself and is never tenant-local.
    subtotal is computed in Postgres as quantity * then_product_price.
    """

    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE)

    internal_user_id = models.TextField(default="null")
    user_id = models.BigIntegerField()

    first_name = models.CharField(max_length=200)
    last_name = models.TextField(null=True, blank=True)
    gender = models.TextField(null=True, blank=True, default="زن")
    phone_number = models.TextField(null=True, blank=True)

    internal_order_id = models.TextField(default="null")
    order_id = models.BigIntegerField()
    order_date = models.DateTimeField(null=True, blank=True)

    internal_product_id = models.TextField(default="null")
    product_id = models.BigIntegerField()

    then_product_price = models.DecimalField(max_digits=10, decimal_places=2)
    quantity = models.IntegerField()
    subtotal = models.DecimalField(
        max_digits=12, decimal_places=2, null=True, blank=True
    )

    column_mapping = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "users_unnormalized_data"
        indexes = [
            models.Index(fields=["tenant_id"], name="users_unnorm_tenant_idx"),
            models.Index(
                fields=["internal_product_id"],
                name="users_unnorm_int_prod_idx",
            ),
        ]

    def __str__(self):
        return f"UsersUnNorm order {self.order_id} | Tenant {self.tenant_id}"


class UsersUnNormalizedDataStaging(models.Model):
    """
    Staging mirror of users_unnormalized_data.
    managed = False — created by RunSQL in migration 0044.
    Rows created by file uploads are scoped to an UploadJob and flushed by
    flush_customers_upload_job(p_job_id). Rows created by the direct-sync
    pipeline retain a NULL upload_job and use the legacy tenant flush path.
    """

    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE)
    upload_job = models.ForeignKey(
        UploadJob,
        db_column="upload_job_id",
        db_constraint=False,
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="customer_staging_rows",
    )

    internal_user_id = models.TextField(default="null")
    user_id = models.BigIntegerField(null=True, blank=True)

    first_name = models.CharField(max_length=200)
    last_name = models.TextField(null=True, blank=True)
    gender = models.TextField(null=True, blank=True, default="زن")
    phone_number = models.TextField(null=True, blank=True)

    internal_order_id = models.TextField(default="null")
    order_id = models.BigIntegerField(null=True, blank=True)
    order_date = models.DateTimeField(null=True, blank=True)

    internal_product_id = models.TextField(default="null")
    product_id = models.BigIntegerField(null=True, blank=True)

    then_product_price = models.DecimalField(max_digits=10, decimal_places=2)
    quantity = models.IntegerField()
    subtotal = models.DecimalField(
        max_digits=12, decimal_places=2, null=True, blank=True
    )

    column_mapping = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "users_unnormalized_data_staging"
        managed = False

    def __str__(self):
        return f"UsersStaging order {self.order_id} | Tenant {self.tenant_id}"


# ─────────────────────────────────────────────────────────────────────────────
# Products flat store  (products-file pipeline)
# ─────────────────────────────────────────────────────────────────────────────


class ProductsUnNormalizedData(models.Model):
    """
    Permanent flat store for the products-file pipeline.

    first_product_attribute and second_product_attribute are raw values
    from the products Excel file — no normalisation is applied.
    current_product_price is initialised from the Excel file and updated
    manually over time. It is the price used in all analytics.
    """

    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE)

    internal_product_id = models.TextField(default="null")
    product_id = models.BigIntegerField()

    product_name = models.CharField(max_length=255)
    product_category = models.CharField(max_length=100)
    current_product_price = models.DecimalField(max_digits=10, decimal_places=2)
    product_link = models.URLField(max_length=2000, blank=True)

    # Generic product attributes — raw values, no normalisation
    first_product_attribute = models.TextField(null=True, blank=True)
    second_product_attribute = models.TextField(null=True, blank=True)

    column_mapping = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "products_unnormalized_data"
        indexes = [
            models.Index(fields=["tenant_id"], name="products_unnorm_tenant_idx"),
            models.Index(
                fields=["internal_product_id"],
                name="products_unnorm_int_prod_idx",
            ),
        ]

    def __str__(self):
        return f"ProductsUnNorm {self.product_name} | Tenant {self.tenant_id}"


class ProductsUnNormalizedDataStaging(models.Model):
    """
    Staging mirror of products_unnormalized_data.
    managed = False — created by RunSQL in migration 0044, altered in 0045.
    File-upload rows are scoped to an UploadJob. Direct-sync rows retain a
    NULL upload_job for compatibility with the legacy tenant flush path.
    """

    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE)
    upload_job = models.ForeignKey(
        UploadJob,
        db_column="upload_job_id",
        db_constraint=False,
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="product_staging_rows",
    )

    internal_product_id = models.TextField(default="null")
    product_id = models.BigIntegerField(null=True, blank=True)

    product_name = models.CharField(max_length=255)
    product_category = models.CharField(max_length=100)
    current_product_price = models.DecimalField(max_digits=10, decimal_places=2)
    product_link = models.URLField(max_length=2000, blank=True)

    first_product_attribute = models.TextField(null=True, blank=True)
    second_product_attribute = models.TextField(null=True, blank=True)

    column_mapping = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "products_unnormalized_data_staging"
        managed = False

    def __str__(self):
        return f"ProductsStaging {self.product_name} | Tenant {self.tenant_id}"


# ─────────────────────────────────────────────────────────────────────────────
# Error Log
# ─────────────────────────────────────────────────────────────────────────────


class ErrorLog(models.Model):

    SEVERITY_CHOICES = [
        ("critical", "Critical"),
        ("error", "Error"),
        ("warning", "Warning"),
        ("info", "Info"),
    ]

    SOURCE_CHOICES = [
        ("coupon", "Coupon"),
        ("sms", "SMS"),
        ("campaign", "Campaign"),
        ("trigger", "Trigger"),
        ("system", "System"),
        ("other", "Other"),
    ]

    tenant = models.ForeignKey(
        Tenant,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        help_text="The tenant this error belongs to, if applicable",
    )
    source = models.CharField(
        max_length=50,
        choices=SOURCE_CHOICES,
        default="other",
        help_text="Which part of the system raised this error",
    )
    severity = models.CharField(
        max_length=20,
        choices=SEVERITY_CHOICES,
        default="error",
        help_text="How severe is this error",
    )
    error_code = models.CharField(
        max_length=100,
        null=True,
        blank=True,
        help_text="Machine-readable error code, e.g. NO_COUPON_FOUND",
    )
    message = models.TextField(help_text="Human-readable error message")
    context = models.JSONField(
        default=dict,
        blank=True,
        help_text="Structured data relevant to this error",
    )
    resolved = models.BooleanField(
        default=False,
        help_text="Whether this error has been acknowledged and resolved",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["tenant_id"]),
            models.Index(fields=["source"]),
            models.Index(fields=["severity"]),
            models.Index(fields=["resolved"]),
            models.Index(fields=["created_at"]),
        ]

    def __str__(self):
        return (
            f"[{self.severity.upper()}] {self.source} | "
            f"{self.error_code or 'N/A'} | {self.created_at}"
        )


def _generate_raw_api_key() -> str:
    """
    32 bytes of urlsafe randomness, prefixed so keys are visually
    identifiable in logs/support tickets without revealing the secret.
    Example: "rsk_3f9a1c7b2e4d5f6a8b9c0d1e2f3a4b5c..."
    """
    return f"rsk_{secrets.token_urlsafe(32)}"


def _hash_api_key(raw_key: str) -> str:
    """
    We only ever store the SHA-256 hash of the key, never the raw value.
    This mirrors password-hashing hygiene: a DB leak does not leak usable
    credentials. The raw key is shown to the tenant exactly once, at
    generation time, and never again (matches every serious API-key UX:
    Stripe, GitHub PATs, etc.)
    """
    return hashlib.sha256(raw_key.encode("utf-8")).hexdigest()


class SyncConfig(models.Model):
    """
    Per-tenant configuration for the automated ETL synchronization system.

    One row per Tenant. Created lazily the first time a tenant opens the
    "تنظیم API" page (see SyncConfigView.get_or_create semantics), not via
    a post_save signal — a tenant with no interest in this feature should
    not accumulate an empty row.
    """

    tenant = models.OneToOneField(
        "core.Tenant",
        on_delete=models.CASCADE,
        related_name="sync_config",
    )

    is_enabled = models.BooleanField(
        default=False,
        help_text=(
            "Whether the ETL is permitted to sync for this tenant. "
            "Set True only after تولید API has succeeded (i.e. a key exists "
            "and every required field mapping row is complete)."
        ),
    )

    # ── API key (hashed) ────────────────────────────────────────────────────
    api_key_hash = models.CharField(max_length=64, unique=True, null=True, blank=True)
    api_key_prefix = models.CharField(
        max_length=12,
        null=True,
        blank=True,
        help_text="First 12 chars of the raw key, kept for display only "
        "(e.g. 'rsk_3f9a1c7b'). Never sufficient to authenticate.",
    )
    api_key_generated_at = models.DateTimeField(null=True, blank=True)

    # ── ETL run behavior ────────────────────────────────────────────────────
    batch_size = models.PositiveIntegerField(
        default=1000,
        help_text="Max rows per POST from the ETL to the data ingest endpoints.",
    )
    user_cursor_column = models.CharField(max_length=255, blank=True, default="")
    product_cursor_column = models.CharField(max_length=255, blank=True, default="")
    user_cursor_value = models.JSONField(null=True, blank=True)
    product_cursor_value = models.JSONField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            models.Index(fields=["api_key_hash"]),
        ]

    def __str__(self):
        return f"SyncConfig | Tenant {self.tenant_id} | enabled={self.is_enabled}"

    def generate_new_api_key(self) -> str:
        """
        Generates a new key, stores its hash, and returns the RAW key so the
        caller (the تولید API view) can return it to the client exactly once.

        Rotating the key (calling this again later) immediately invalidates
        the previous key, since only the hash is stored and it is overwritten.
        """
        raw_key = _generate_raw_api_key()
        self.api_key_hash = _hash_api_key(raw_key)
        self.api_key_prefix = raw_key[:12]
        self.api_key_generated_at = timezone.now()
        self.save(
            update_fields=["api_key_hash", "api_key_prefix", "api_key_generated_at"]
        )
        return raw_key

    @staticmethod
    def resolve_from_raw_key(raw_key: str) -> "SyncConfig | None":
        """Used by the sync authentication class. O(1) hash lookup."""
        if not raw_key:
            return None
        key_hash = _hash_api_key(raw_key)
        return (
            SyncConfig.objects.select_related("tenant")
            .filter(api_key_hash=key_hash, is_enabled=True)
            .first()
        )


class SyncFieldMapping(models.Model):
    """
    One row per (tenant, entity, field_name): the tenant's declaration of
    which table/column in THEIR database corresponds to ONE of our
    canonical fields.

    entity distinguishes the two logical groups the UI collects
    ("user" fields feed UsersUnNormalizedDataStaging /
    UsersUnNormalizedData; "product" fields feed
    ProductsUnNormalizedDataStaging / ProductsUnNormalizedData).

    field_name must be a key in core.sync.field_registry.FIELD_REGISTRY for
    the given entity — enforced in the serializer, not as a DB choices
    constraint, so the registry stays the single source of truth and this
    model never needs a migration when a field is added/removed.

    client_table / client_column are free text: the tenant's own schema,
    which we have zero visibility into ourselves.
    """

    ENTITY_CHOICES = [
        ("user", "User"),
        ("product", "Product"),
    ]

    tenant = models.ForeignKey(
        "core.Tenant",
        on_delete=models.CASCADE,
        related_name="sync_field_mappings",
    )
    entity = models.CharField(max_length=10, choices=ENTITY_CHOICES)
    field_name = models.CharField(
        max_length=100,
        help_text="One of our canonical field names, see core/sync/field_registry.py",
    )

    client_table = models.CharField(max_length=255, blank=True, default="")
    client_column = models.CharField(max_length=255, blank=True, default="")

    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "entity", "field_name"],
                name="uq_sync_field_mapping_tenant_entity_field",
            )
        ]
        indexes = [
            models.Index(fields=["tenant", "entity"]),
        ]

    def __str__(self):
        return (
            f"{self.entity}.{self.field_name} → "
            f"{self.client_table}.{self.client_column} | Tenant {self.tenant_id}"
        )

    @property
    def is_filled(self) -> bool:
        return bool(self.client_table.strip()) and bool(self.client_column.strip())


class SyncRun(models.Model):
    """
    Audit log — one row per ETL cycle attempt, including cycles that never
    got past the ETL's own pre-flight schema check.

    This is what the "تنظیم API" page's status banner reads from, and what
    lets a tenant self-diagnose "why did my sync fail" without opening a
    support ticket.
    """

    STATUS_CHOICES = [
        ("running", "Running"),
        ("success", "Success"),
        ("partial", "Partial success"),
        ("failed", "Failed"),
    ]

    FAILURE_STAGE_CHOICES = [
        ("schema_table", "Missing table"),
        ("schema_column", "Missing column"),
        ("connection", "Could not connect to client database"),
        ("ingest", "Rejected during ingest"),
        ("unknown", "Unknown / unclassified"),
    ]

    tenant = models.ForeignKey(
        "core.Tenant",
        on_delete=models.CASCADE,
        related_name="sync_runs",
    )

    client_run_id = models.UUIDField(null=True, blank=True)
    instance_id = models.UUIDField(null=True, blank=True)
    lease_expires_at = models.DateTimeField(null=True, blank=True)
    customers_upload_job = models.OneToOneField(
        UploadJob, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="customer_sync_run",
    )
    products_upload_job = models.OneToOneField(
        UploadJob, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="product_sync_run",
    )

    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default="running")

    failure_stage = models.CharField(
        max_length=20, choices=FAILURE_STAGE_CHOICES, null=True, blank=True
    )
    failure_detail = models.TextField(
        blank=True,
        default="",
        help_text="Precise machine detail, e.g. the exact missing table/column name. "
        "Not necessarily the same string shown to the end user.",
    )

    users_rows_received = models.PositiveIntegerField(default=0)
    users_rows_accepted = models.PositiveIntegerField(default=0)
    users_rows_rejected = models.PositiveIntegerField(default=0)

    products_rows_received = models.PositiveIntegerField(default=0)
    products_rows_accepted = models.PositiveIntegerField(default=0)
    products_rows_rejected = models.PositiveIntegerField(default=0)

    started_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-started_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "client_run_id"],
                name="uq_sync_run_tenant_client_run",
            ),
            models.UniqueConstraint(
                fields=["tenant"], condition=models.Q(status="running"),
                name="uq_sync_run_one_running_tenant",
            ),
        ]
        indexes = [
            models.Index(fields=["tenant", "-started_at"]),
            models.Index(fields=["status"]),
        ]

    def __str__(self):
        return f"SyncRun #{self.id} | Tenant {self.tenant_id} | {self.status}"

    @property
    def user_facing_message(self) -> str:
        """
        The exact string the frontend should render for a failed run, per
        the project spec: mismatched table/column names produce this fixed
        message regardless of which table/column was the culprit (the
        precise detail stays in failure_detail for support/debugging use,
        not shown to the tenant directly).
        """
        if self.status == "failed" and self.failure_stage in (
            "schema_table",
            "schema_column",
        ):
            return (
                "Table names or columns do not match, failed to find the "
                "corresponding table name or column, please check the api "
                "conf page again."
            )
        if self.status == "failed" and self.failure_stage == "connection":
            return "Could not connect to your database. Please check your credentials."
        if self.status == "partial":
            return (
                "Sync completed with some rows skipped due to invalid data. "
                "See details for the affected rows."
            )
        if self.status == "success":
            return "Sync completed successfully."
        return ""


class SyncBatch(models.Model):
    """One transport-idempotent batch belonging to an automated sync run."""

    ENTITY_CHOICES = [("user", "User"), ("product", "Product")]

    run = models.ForeignKey(
        SyncRun, on_delete=models.CASCADE, related_name="batches"
    )
    entity = models.CharField(max_length=10, choices=ENTITY_CHOICES)
    batch_number = models.PositiveIntegerField()
    idempotency_key = models.CharField(max_length=200, unique=True)
    payload_hash = models.CharField(max_length=64)
    cursor_after = models.JSONField(null=True, blank=True)
    response_payload = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["run", "entity", "batch_number"],
                name="uq_sync_batch_run_entity_number",
            )
        ]
        indexes = [
            models.Index(
                fields=["run", "entity"],
                name="core_syncba_run_id_828a16_idx",
            )
        ]
        ordering = ["entity", "batch_number"]
