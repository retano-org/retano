# core/serializers_schema.py
"""
Output-only serializers used exclusively for OpenAPI schema generation via
@extend_schema(responses=...) in core/schema.py.
"""

from rest_framework import serializers


# ─────────────────────────────────────────────────────────────────────────
# Shared / reusable pieces
# ─────────────────────────────────────────────────────────────────────────


class ErrorResponseSerializer(serializers.Serializer):
    """
    The standard error envelope produced by
    core.exceptions.custom_exception_handler for every RAISED DRF
    exception (ValidationError, NotAuthenticated, PermissionDenied,
    Http404, unhandled 500s, and any APIException subclass such as
    core.exceptions.OTPError / BusinessLogicError / TenantPermissionError).

    Attach this to the 400 / 401 / 403 / 404 / 409 / 429 / 500 responses
    of any endpoint whose errors are raised as exceptions rather than
    hand-built as a Response() (see individual view docstrings in
    core/schema.py for the handful of endpoints that do NOT go through
    this handler — those are documented with their own, different,
    actual shape instead of this one).
    """

    error = serializers.BooleanField(
        default=True,
        help_text="Always true on an error response.",
    )
    status_code = serializers.IntegerField(
        help_text="Same value as the HTTP status code of this response."
    )
    message = serializers.CharField(
        help_text="Human-readable summary of what went wrong."
    )
    details = serializers.DictField(
        child=serializers.ListField(child=serializers.CharField()),
        required=False,
        help_text=(
            "Field-level validation errors, e.g. "
            '{"phone_number": ["This field is required."]}. '
            "Empty object ({}) when the error has no field-level "
            "breakdown (e.g. authentication/permission/404 errors)."
        ),
    )


class ChoiceOptionSerializer(serializers.Serializer):
    """One {value, label} pair, as used throughout CampaignMetaView."""

    value = serializers.CharField(
        help_text=(
            "The raw value to submit back to the API. Usually a string, "
            "except for coupon_discount_percentage where it is an integer "
            "0-100 (see CampaignMetaResponseSerializer)."
        )
    )
    label = serializers.CharField(help_text="Human-readable Persian label to display.")


# ─────────────────────────────────────────────────────────────────────────
# Dashboard
# ─────────────────────────────────────────────────────────────────────────


class DashboardCampaignCountsSerializer(serializers.Serializer):
    total = serializers.IntegerField()
    active = serializers.IntegerField(
        help_text="is_active=True AND campaign_start_date <= today <= campaign_end_date."
    )
    ended = serializers.IntegerField(
        help_text="campaign_end_date < today, regardless of is_active."
    )
    deleted = serializers.IntegerField(
        help_text="is_active=False (soft-delete flag; the row is never hard-deleted)."
    )


class DashboardCustomerCountsSerializer(serializers.Serializer):
    active = serializers.IntegerField(
        help_text="rfm_segment IN ('vip', 'new', 'active')."
    )
    inactive = serializers.IntegerField(
        help_text="rfm_segment IN ('churned', 'at_risk')."
    )
    total = serializers.IntegerField(help_text="active + inactive.")


class DashboardTopProductSerializer(serializers.Serializer):
    name = serializers.CharField()
    total_revenue = serializers.FloatField(
        help_text="Sum of order_items.subtotal for this product, in tomans."
    )


class YearlyRetentionPointSerializer(serializers.Serializer):
    """
    One entry of core.utils.analytics.get_yearly_retention(). Shared by
    DashboardView's "monthly_trends" key (name is a known misnomer — the
    data is yearly, not monthly) and RetentionReportView's "years" key.
    """

    jalali_year = serializers.IntegerField()
    customers = serializers.IntegerField(
        help_text="Distinct customers with >=1 order in this Jalali year."
    )
    retained = serializers.IntegerField(
        help_text="Of those customers, how many also ordered in jalali_year + 1."
    )
    retention_rate_percent = serializers.FloatField()
    churn_rate_percent = serializers.FloatField(help_text="100 - retention_rate_percent.")


class DashboardRFMSegmentsSerializer(serializers.Serializer):
    """
    Always all 5 keys, zero-filled — unlike SegmentsReportView, this is a
    flat dict of segment -> count, not a list of {segment, label, count,
    percentage} objects. The two endpoints intentionally differ in shape;
    do not assume they match.
    """

    vip = serializers.IntegerField()
    active = serializers.IntegerField()
    new = serializers.IntegerField()
    at_risk = serializers.IntegerField()
    churned = serializers.IntegerField()


class DashboardResponseSerializer(serializers.Serializer):
    """
    GET /api/v1/dashboard/ — full response shape. Tenant-scoped snapshots
    are served immediately and refreshed in the background when stale.
    """

    campaigns = DashboardCampaignCountsSerializer()
    customers = DashboardCustomerCountsSerializer()
    monthly_sales = serializers.FloatField(
        help_text="Sum of order totals for the current Jalali month, in tomans."
    )
    top_products = DashboardTopProductSerializer(many=True)
    monthly_trends = YearlyRetentionPointSerializer(
        many=True,
        help_text=(
            "NOTE: despite the key name, this is YEARLY retention/churn "
            "data (same shape and same underlying function as "
            "GET /api/v1/reports/retention/'s \"years\" key), not monthly. "
            "The most recent year is omitted (no year+1 data exists yet "
            "to measure retention against)."
        ),
    )
    rfm_segments = DashboardRFMSegmentsSerializer()
    sms_balance = serializers.IntegerField(
        help_text="CustomUser.num_available_sms for the requesting user."
    )
    support_unread_count = serializers.IntegerField(
        help_text=(
            "Unread notifications.models.Notification rows for this "
            "tenant (is_read=False). Field name is kept as "
            "support_unread_count for frontend compatibility even though "
            "it no longer refers to the old tickets support-chat system, "
            "which has been removed — it now reflects the one-way "
            "admin-authored notification system instead."
        )
    )


# ─────────────────────────────────────────────────────────────────────────
# Reports — trends (year / month, two distinct shapes)
# ─────────────────────────────────────────────────────────────────────────


class TrendsYearPointSerializer(serializers.Serializer):
    jalali_year = serializers.IntegerField()
    customer_count = serializers.IntegerField()
    revenue = serializers.FloatField(help_text="Tomans.")
    clv = serializers.FloatField(help_text="revenue / customer_count for this year.")


class TrendsMonthPointSerializer(serializers.Serializer):
    jalali_year = serializers.IntegerField()
    jalali_month = serializers.IntegerField(help_text="1-12.")
    month_name = serializers.CharField(help_text="Persian month name, e.g. 'اردیبهشت'.")
    customer_count = serializers.IntegerField()
    revenue = serializers.FloatField(help_text="Tomans.")
    clv = serializers.FloatField(help_text="revenue / customer_count for this month.")


class TrendsYearlyResponseSerializer(serializers.Serializer):
    """Returned when ?granularity=year (the default — no query param sent)."""

    granularity = serializers.ChoiceField(choices=["year"])
    data = TrendsYearPointSerializer(
        many=True,
        help_text=(
            "Exactly 4 points: the last 4 Jalali years ending at the "
            "current Jalali year, oldest first. Zero-filled for years "
            "with no orders — this is a fixed 4-year window, not the "
            "tenant's full order history."
        ),
    )


class TrendsMonthlyResponseSerializer(serializers.Serializer):
    """Returned when ?granularity=month."""

    granularity = serializers.ChoiceField(choices=["month"])
    data = TrendsMonthPointSerializer(
        many=True,
        help_text=(
            "Exactly 6 points: the last 6 Jalali months including the "
            "current month, oldest first."
        ),
    )


class TrendsBadGranularityResponseSerializer(serializers.Serializer):
    """
    400 response when ?granularity= is anything other than 'year' or
    'month'. NOTE: this endpoint builds this Response() by hand and does
    NOT go through core.exceptions.custom_exception_handler — the shape
    is genuinely just {"detail": "..."}, not the standard {error,
    status_code, message, details} envelope. Documented here exactly as
    it actually behaves.
    """

    detail = serializers.CharField(default="granularity must be 'year' or 'month'.")


class TenantMissingResponseSerializer(serializers.Serializer):
    """
    403 response shared by every reports/* view and DashboardView when
    request.user.tenant does not exist (e.g. a superuser created without
    the Tenant-creation signal firing). Like
    TrendsBadGranularityResponseSerializer above, this is a hand-built
    Response({"detail": ...}, 403) that does NOT go through
    custom_exception_handler — documented as its actual shape, not the
    standard error envelope.
    """

    detail = serializers.CharField(default="Tenant record not found for this user.")


# ─────────────────────────────────────────────────────────────────────────
# Reports — sales ranges / segments / active users / retention
# ─────────────────────────────────────────────────────────────────────────


class SalesRangeBucketSerializer(serializers.Serializer):
    key = serializers.ChoiceField(
        choices=["0_to_0.5m", "0.5_to_1m", "1_to_3m", "above_3m"]
    )
    label = serializers.CharField(help_text="Persian bucket label, e.g. '۰ تا ۰٫۵'.")
    order_count = serializers.IntegerField()


class SalesRangeResponseSerializer(serializers.Serializer):
    buckets = SalesRangeBucketSerializer(
        many=True,
        help_text=(
            "Always exactly 4 buckets in this fixed order: "
            "0_to_0.5m, 0.5_to_1m, 1_to_3m, above_3m. Every order in the "
            "tenant's entire history counts exactly once, in tomans."
        ),
    )


class SegmentEntrySerializer(serializers.Serializer):
    segment = serializers.ChoiceField(
        choices=["vip", "active", "new", "at_risk", "churned"]
    )
    label = serializers.CharField(help_text="Persian label for this segment.")
    count = serializers.IntegerField()
    percentage = serializers.FloatField(help_text="Rounded to 2 decimal places.")


class SegmentsResponseSerializer(serializers.Serializer):
    total_users = serializers.IntegerField(
        help_text="Sum of all 5 segment counts (only users with a non-null rfm_segment)."
    )
    segments = SegmentEntrySerializer(
        many=True,
        help_text=(
            "Always exactly 5 entries in a fixed order (vip, active, new, "
            "at_risk, churned), zero-filled for empty segments."
        ),
    )


class ActiveUsersResponseSerializer(serializers.Serializer):
    total_users = serializers.IntegerField()
    active_count = serializers.IntegerField()
    inactive_count = serializers.IntegerField()
    active_percent = serializers.FloatField(help_text="Rounded to 1 decimal place.")
    inactive_percent = serializers.FloatField(help_text="100.0 - active_percent.")


class RetentionResponseSerializer(serializers.Serializer):
    years = YearlyRetentionPointSerializer(
        many=True,
        help_text=(
            "One entry per computable Jalali year, oldest first. The most "
            "recent year is always omitted (no year+1 data exists yet)."
        ),
    )


# ─────────────────────────────────────────────────────────────────────────
# Campaigns — meta
# ─────────────────────────────────────────────────────────────────────────


class CampaignMetaResponseSerializer(serializers.Serializer):
    """
    GET /api/v1/campaigns/meta/

    The response is a dict keyed by field name, not a fixed set of named
    top-level keys — DRF/drf-spectacular has no native way to express
    "object with these exact 8 keys, each a list of {value, label}" other
    than naming each key explicitly, which is what this serializer does.
    All 8 keys are always present.

    coupon_discount_percentage's "value" is an INTEGER (0-100), not a
    string, unlike every other field here where "value" is a string —
    this mirrors Campaign.COUPON_DISCOUNT_PERCENTAGE_CHOICES exactly
    ([(i, f"{i}%") for i in range(0, 101)]).
    """

    coupon_discount_percentage = serializers.ListField(
        child=serializers.DictField(),
        help_text=(
            "101 entries, one per integer 0-100. Each entry is "
            '{"value": <int 0-100>, "label": "<int>%"} — value is an '
            "integer here, unlike every other field below."
        ),
    )
    activation_base = ChoiceOptionSerializer(many=True)
    comparison_type = ChoiceOptionSerializer(many=True)
    value_unit = ChoiceOptionSerializer(many=True)
    gender = ChoiceOptionSerializer(many=True)
    buying_power = ChoiceOptionSerializer(many=True)
    priority = ChoiceOptionSerializer(many=True)
    customer_type = ChoiceOptionSerializer(many=True)


# ─────────────────────────────────────────────────────────────────────────
# Uploads
# ─────────────────────────────────────────────────────────────────────────


class UploadErrorResponseSerializer(serializers.Serializer):
    """
    400 response shared by all three upload endpoints (customers, products,
    coupons) for file_error, mapping_error, and (coupons only)
    duplicate_coupon_error. Hand-built Response(), not routed through
    custom_exception_handler — rows_processed/rows_saved are always 0
    here since the error occurs before any row is read.
    """

    status = serializers.ChoiceField(choices=["error"])
    error_type = serializers.ChoiceField(
        choices=["file_error", "mapping_error", "duplicate_coupon_error"]
    )
    message = serializers.CharField(help_text="Persian, user-facing error message.")
    rows_processed = serializers.IntegerField(default=0)
    rows_saved = serializers.IntegerField(default=0)


class UploadAcceptedResponseSerializer(serializers.Serializer):
    """202 response shared by all three upload endpoints on successful enqueue."""

    status = serializers.ChoiceField(choices=["accepted"])
    job_id = serializers.UUIDField()
    status_url = serializers.CharField(
        help_text="Relative path — poll this with GET to track progress, e.g. "
        "/api/v1/uploads/jobs/<job_id>/."
    )
    message = serializers.CharField(help_text="Persian confirmation message.")


class UploadJobStatusResponseSerializer(serializers.Serializer):
    """GET /api/v1/uploads/jobs/{job_id}/ — mirrors UploadJob.to_status_dict() exactly."""

    job_id = serializers.UUIDField()
    upload_type = serializers.ChoiceField(choices=["customers", "products", "coupons"])
    status = serializers.ChoiceField(
        choices=["queued", "processing", "success", "partial", "failed"]
    )
    total_rows = serializers.IntegerField(
        allow_null=True,
        help_text="Null until the worker has finished counting rows in the file.",
    )
    processed_rows = serializers.IntegerField()
    rows_saved = serializers.IntegerField()
    progress_percentage = serializers.FloatField(
        help_text="min(processed_rows, total_rows) / total_rows * 100, rounded to 2dp. "
        "0.0 while total_rows is still null."
    )
    error_type = serializers.CharField(allow_null=True)
    message = serializers.CharField(
        allow_blank=True, help_text="Persian status/result message."
    )
    created_at = serializers.DateTimeField()
    updated_at = serializers.DateTimeField()


class UploadHistoryItemResponseSerializer(UploadJobStatusResponseSerializer):
    original_filename = serializers.CharField()
    column_headers = serializers.ListField(child=serializers.CharField())
    download_url = serializers.URLField()


class UploadJobNotFoundResponseSerializer(serializers.Serializer):
    """
    404 response for GET /api/v1/uploads/jobs/{job_id}/ when the job does
    not exist or does not belong to the requesting tenant. NOTE: this is
    a DIFFERENT shape than UploadErrorResponseSerializer above (no
    error_type / rows_processed / rows_saved) and does not go through
    custom_exception_handler — it is a hand-built Response({"status":
    "error", "message": ...}, 404).
    """

    status = serializers.ChoiceField(choices=["error"])
    message = serializers.CharField(default="Job not found.")


class SampleFilesResponseSerializer(serializers.Serializer):
    customers = serializers.URLField(help_text="Absolute URL to the sample customers .xlsx.")
    products = serializers.URLField(help_text="Absolute URL to the sample products .xlsx.")
    coupons = serializers.URLField(help_text="Absolute URL to the sample coupons .xlsx.")


# ─────────────────────────────────────────────────────────────────────────
# Sync (ETL-facing)
# ─────────────────────────────────────────────────────────────────────────


class SyncRowRejectionSerializer(serializers.Serializer):
    index = serializers.IntegerField(help_text="0-based index of this row within the submitted batch.")
    internal_id = serializers.CharField(
        allow_null=True,
        help_text="internal_user_id or internal_product_id of the rejected row, if it was present.",
    )
    field = serializers.CharField(help_text="Name of the field whose coercion failed.")
    reason = serializers.CharField(help_text="Human-readable coercion failure reason.")


class SyncIngestResponseSerializer(serializers.Serializer):
    """
    200 response shared by POST /api/v1/sync/data/users/ and
    POST /api/v1/sync/data/products/. Mirrors
    core.services.sync_pipeline.IngestResult.as_dict() exactly.

    Rejections are per-ROW, not per-batch — a batch with some rejected
    rows still returns 200, not a 4xx. rows_rejected + rows_accepted
    always equals rows_received.
    """

    rows_received = serializers.IntegerField()
    rows_accepted = serializers.IntegerField()
    rows_rejected = serializers.IntegerField()
    rejections = SyncRowRejectionSerializer(many=True)


class SyncReportRecordedResponseSerializer(serializers.Serializer):
    """201 response for POST /api/v1/sync/report/."""

    message = serializers.CharField(default="Report recorded.")


# ─────────────────────────────────────────────────────────────────────────
# Sync-conf (تنظیم API page, JWT-authenticated)
# ─────────────────────────────────────────────────────────────────────────


class SyncFieldMappingRowSerializer(serializers.Serializer):
    entity = serializers.ChoiceField(choices=["user", "product"])
    field_name = serializers.CharField(
        help_text="Canonical field name from core.sync.field_registry.FIELD_REGISTRY."
    )
    client_table = serializers.CharField(
        allow_blank=True, help_text="Empty string if not yet configured."
    )
    client_column = serializers.CharField(
        allow_blank=True, help_text="Empty string if not yet configured."
    )
    is_filled = serializers.BooleanField(
        help_text="True only when both client_table and client_column are non-blank."
    )


class SyncFieldMappingListResponseSerializer(serializers.Serializer):
    """
    GET /api/v1/sync-conf/mapping/ — always returns exactly 17 rows (10
    user fields + 7 product fields), including rows that have never been
    saved (represented with empty client_table/client_column and
    is_filled=false), so the frontend can render every rectangle on
    first page load.
    """

    mappings = SyncFieldMappingRowSerializer(many=True)


class SyncFieldMappingSavedResponseSerializer(serializers.Serializer):
    """200 response for PUT /api/v1/sync-conf/mapping/ on success."""

    message = serializers.CharField(default="نگاشت ستون‌ها با موفقیت ذخیره شد.")


class SyncApiKeyMappingIncompleteResponseSerializer(serializers.Serializer):
    """
    400 response for POST /api/v1/sync-conf/generate-key/ when one or
    more of the 17 mapping rows is missing or incomplete. Hand-built
    Response(), not routed through custom_exception_handler.
    """

    status = serializers.ChoiceField(choices=["error"])
    error_type = serializers.ChoiceField(choices=["mapping_incomplete"])
    message = serializers.CharField()
    missing_fields = serializers.ListField(
        child=serializers.CharField(),
        help_text='e.g. ["user.phone_number", "product.product_link"].',
    )


# ─────────────────────────────────────────────────────────────────────────
# Auth (users app)
# ─────────────────────────────────────────────────────────────────────────


class OTPRequestResponseSerializer(serializers.Serializer):
    """
    200 response for POST /api/v1/auth/otp/request/.

    debug_code is ONLY ever present when the server is running with
    OTP_FAKE_MODE=True (see config/settings/development.py) — it is never
    sent by a production deployment. Documented as not required so the
    schema reflects that a client must not assume it will be there.
    """

    phone_number = serializers.CharField(help_text="Normalized E.164 form, e.g. +989121234567.")
    ttl_seconds = serializers.IntegerField(help_text="How long the OTP code remains valid.")
    resend_in_seconds = serializers.IntegerField(
        help_text="Client must wait this long before requesting another OTP for this number."
    )
    debug_code = serializers.RegexField(
        regex=r"^[0-9]{4}$",
        min_length=4,
        max_length=4,
        required=False,
        help_text=(
            "DEV/TEST ONLY. Present only when the server has "
            "OTP_FAKE_MODE=True. Never present in production."
        ),
    )


class OTPVerifyResponseSerializer(serializers.Serializer):
    """
    200 response for POST /api/v1/auth/otp/verify/. Creates the user on
    first successful verification if one does not already exist for this
    phone number (implicit signup) — the response shape is identical
    either way.
    """

    user_id = serializers.IntegerField()
    phone_number = serializers.CharField()
    access = serializers.CharField(help_text="Simple JWT access token.")
    refresh = serializers.CharField(help_text="Simple JWT refresh token.")


class RegisterResponseSerializer(serializers.Serializer):
    """201 response for POST /api/v1/auth/register/."""

    user_id = serializers.IntegerField()
    phone_number = serializers.CharField()
    access = serializers.CharField(help_text="Simple JWT access token.")
    refresh = serializers.CharField(help_text="Simple JWT refresh token.")


class AccountStatusResponseSerializer(serializers.Serializer):
    user_id = serializers.IntegerField()
    phone_number = serializers.CharField()
    phone_verified = serializers.BooleanField(
        help_text=(
            "Always true for any authenticated request in this system — "
            "reaching this endpoint at all requires a valid JWT, which is "
            "only issued after OTP verification or registration. There is "
            "no 'logged in but unverified' state to represent."
        )
    )
    is_premium = serializers.BooleanField()
    has_tenant = serializers.BooleanField()
    profile_complete = serializers.BooleanField(
        help_text="True iff first_name, last_name, and shop_name are all non-empty."
    )


# ─────────────────────────────────────────────────────────────────────────
# SMS / Billing (users app)
# ─────────────────────────────────────────────────────────────────────────


class SMSBalanceResponseSerializer(serializers.Serializer):
    num_available_sms = serializers.IntegerField(
        help_text="Set manually by an admin after payment is confirmed out of band."
    )


# ─────────────────────────────────────────────────────────────────────────
# Notifications
# ─────────────────────────────────────────────────────────────────────────


class NotificationListItemResponseSerializer(serializers.Serializer):
    """
    One row of GET /api/v1/notifications/ — mirrors
    notifications.serializers.NotificationListSerializer exactly. Body
    text (`content`) is deliberately excluded from the list shape; it
    only appears on the detail endpoint below.
    """

    id = serializers.IntegerField()
    title = serializers.CharField()
    created_at = serializers.DateTimeField(help_text="Gregorian, ISO-8601.")
    created_at_jalali = serializers.CharField(
        help_text="Pre-formatted Jalali date string, e.g. '1405/03/10'."
    )
    is_read = serializers.BooleanField()


class NotificationDetailResponseSerializer(serializers.Serializer):
    """
    GET /api/v1/notifications/{id}/ — mirrors
    notifications.serializers.NotificationDetailSerializer. Fetching
    this endpoint marks this specific notification as read as a side
    effect (is_read becomes true on this call if it wasn't already) —
    other notifications belonging to the same tenant are unaffected.
    """

    id = serializers.IntegerField()
    title = serializers.CharField()
    content = serializers.CharField()
    created_at = serializers.DateTimeField(help_text="Gregorian, ISO-8601.")
    created_at_jalali = serializers.CharField(
        help_text="Pre-formatted Jalali date string, e.g. '1405/03/10'."
    )
    is_read = serializers.BooleanField(
        help_text="Always true in the response body for this call, since fetching marks it read."
    )


class NotificationUnreadCountResponseSerializer(serializers.Serializer):
    unread_count = serializers.IntegerField(
        help_text=(
            "Unread notifications for this tenant. Polling this endpoint "
            "does NOT mark anything as read — only fetching a specific "
            "notification's detail does that."
        )
    )


# ─────────────────────────────────────────────────────────────────────────
# Campaigns — detail stats
# ─────────────────────────────────────────────────────────────────────────


class CampaignDetailStatsResponseSerializer(serializers.Serializer):
    """
    GET /api/v1/campaigns/{id}/stats/ — mirrors
    CampaignDetailStatsView.get()'s response dict exactly.
    """

    campaign_id = serializers.IntegerField()
    targeted_users = serializers.IntegerField(
        help_text="Total trigger_results rows for this campaign."
    )
    customer_count = serializers.IntegerField(
        help_text="Distinct users who ordered within their own send window."
    )
    order_count = serializers.IntegerField()
    sales_amount = serializers.FloatField(help_text="Tomans.")
    sms_sent = serializers.IntegerField()
    sms_delivered = serializers.IntegerField()
    sms_delivery_rate_percent = serializers.FloatField(
        help_text="sms_delivered / sms_sent * 100, rounded to 1dp. 0.0 if sms_sent is 0."
    )
    conversion_rate_percent = serializers.FloatField(
        help_text="customer_count / sms_delivered * 100, rounded to 1dp. 0.0 if sms_delivered is 0."
    )
