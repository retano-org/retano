# core/models_supabase.py
"""
Unmanaged mirrors of tables that live in Supabase but were created by hand
via raw SQL (see the CREATE TABLE statements the project already ran) —
NOT by any Django migration. Every model here has ``managed = False``:
Django will never create, alter, or drop these tables. They exist purely
so application code (views, Celery tasks) can query/update them through
the ORM instead of raw SQL scattered across the codebase.

Do NOT run makemigrations against this file expecting CREATE TABLE
statements — Django deliberately skips schema operations for
managed=False models. Any real schema change to these tables must be
applied by hand in the Supabase SQL editor (or via pg_cron, as the
project already does for process_campaign_eligibility() etc.).

Scope note (Campaign Detail phase): only the fields actually needed for
the campaign detail stats endpoint and the SMS send/delivery pipeline are
declared below. Columns that exist in Supabase but aren't used anywhere
in this phase (e.g. users.semantic_profile, products.semantic_text,
user_summary.rep_top1/2/3) are intentionally omitted — add them later if
a future phase needs them; omitting a column here does not affect the
real table.

Foreign keys here point at core.models.Campaign / core.models.Tenant
where the relationship is real (trigger_results.rule_id -> core_campaign.id
already exists as a DB-level FK per the CREATE TABLE you ran). Where the
FK crosses into another unmanaged model (e.g. orders.user_id -> users.user_id)
we still declare it as a Django ForeignKey — Django will use it for JOINs
and .select_related() without trying to create the constraint itself,
since managed=False also skips constraint creation.
"""

from django.db import models

from core.models import Campaign, InstantCampaign, Tenant


# ─────────────────────────────────────────────────────────────────────────────
# users  (Supabase-native, distinct from Django's CustomUser / auth system)
# ─────────────────────────────────────────────────────────────────────────────


class SupabaseUser(models.Model):
    """
    Mirrors public.users — the normalised end-customer record produced by
    the upload pipeline (sync_users_normalized_batch), NOT the platform's
    own login/auth user (that's users.models.CustomUser, a completely
    separate table/system).
    """

    user_id = models.TextField(primary_key=True)
    phone_number = models.TextField(null=True, blank=True)
    gender = models.TextField(null=True, blank=True)
    first_name = models.CharField(max_length=100, null=True, blank=True)
    last_name = models.CharField(max_length=100, null=True, blank=True)
    tenant_id = models.IntegerField(null=True, blank=True, default=5)

    class Meta:
        managed = False
        db_table = "users"

    def __str__(self):
        return f"SupabaseUser({self.user_id})"


# ─────────────────────────────────────────────────────────────────────────────
# orders
# ─────────────────────────────────────────────────────────────────────────────


class SupabaseOrder(models.Model):
    """
    Mirrors public.orders. order_date is DATE only (no time-of-day
    component) — the campaign detail "72 hour window" calculation
    therefore approximates using whole calendar days, not exact
    hour/minute/second, per product decision.
    """

    order_id = models.TextField(primary_key=True)
    user = models.ForeignKey(
        SupabaseUser,
        to_field="user_id",
        db_column="user_id",
        on_delete=models.DO_NOTHING,
        null=True,
        blank=True,
        related_name="orders",
    )
    order_date = models.DateField(null=True, blank=True)
    total_amount = models.DecimalField(
        max_digits=20, decimal_places=2, null=True, blank=True
    )
    product_count = models.IntegerField()
    created_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        managed = False
        db_table = "orders"

    def __str__(self):
        return f"SupabaseOrder({self.order_id})"


# ─────────────────────────────────────────────────────────────────────────────
# order_items  (not queried directly by this phase's endpoint, but declared
# for completeness / future phases — e.g. per-product campaign attribution,
# which is explicitly OUT of scope for this phase per product decision)
# ─────────────────────────────────────────────────────────────────────────────


class SupabaseOrderItem(models.Model):
    order = models.ForeignKey(
        SupabaseOrder,
        to_field="order_id",
        db_column="order_id",
        on_delete=models.DO_NOTHING,
        related_name="items",
    )
    product_id = models.TextField()
    quantity = models.IntegerField(null=True, blank=True)
    price = models.DecimalField(max_digits=20, decimal_places=2, null=True, blank=True)
    subtotal = models.DecimalField(
        max_digits=20, decimal_places=2, null=True, blank=True
    )
    computed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        managed = False
        db_table = "order_items"
        # order_items has a composite primary key (order_id, product_id) in
        # Postgres. Django requires a single-column PK for the ORM, so we
        # let Django fall back to an implicit `id` — EXCEPT the real table
        # has no such column. Since this model is not used for writes in
        # this phase (read-only joins only, via raw SQL in the stats view,
        # not this ORM model), this is left as a documented limitation
        # rather than worked around. Do NOT call .save()/.create() through
        # this model as-is.

    def __str__(self):
        return f"SupabaseOrderItem({self.order_id}, {self.product_id})"


# ─────────────────────────────────────────────────────────────────────────────
# trigger_results  — the campaign <-> targeted-user join table.
# This is the centerpiece of the campaign detail stats endpoint.
# ─────────────────────────────────────────────────────────────────────────────


class TriggerResult(models.Model):
    """
    Mirrors public.trigger_results.

    One row per (user, tenant) — see the DB-level UNIQUE constraint
    uq_trigger_results_user_tenant that process_campaign_eligibility()
    creates defensively if missing. rule_id is a real FK to
    core_campaign.id at the DB level, so it's declared as a genuine
    Django ForeignKey to core.models.Campaign here.

    New columns added for this phase (NOT part of the original CREATE
    TABLE you shared — see the accompanying migration SQL):
        sms_message_id  — the message id returned by sms.ir's
                          send_like_to_like call, used later to look up
                          delivery status via report_message().
        delivered_at    — stamped once report_message() confirms the
                          message was actually delivered (as opposed to
                          merely submitted/sent).
    """

    id = models.BigAutoField(primary_key=True)
    tenant_id = models.IntegerField()
    user = models.ForeignKey(
        SupabaseUser,
        to_field="user_id",
        db_column="user_id",
        on_delete=models.DO_NOTHING,
        related_name="trigger_results",
    )
    rule = models.ForeignKey(
        Campaign,
        db_column="rule_id",
        on_delete=models.DO_NOTHING,
        related_name="trigger_results",
        null=True,
    )
    instant_campaign = models.ForeignKey(
        InstantCampaign,
        on_delete=models.DO_NOTHING,
        related_name="trigger_results",
        null=True,
    )
    rule_priority = models.CharField(max_length=50, null=True, blank=True)
    detected_at = models.DateTimeField(null=True, blank=True)
    status = models.TextField(null=True, blank=True)
    final_message = models.TextField(null=True, blank=True)
    processed = models.BooleanField(default=False, null=True, blank=True)
    hash = models.CharField(max_length=255, null=True, blank=True, unique=True)
    retry_count = models.IntegerField(default=0, null=True, blank=True)
    sent_at = models.DateTimeField(null=True, blank=True)
    error_message = models.TextField(null=True, blank=True)
    send_sms_date = models.DateField(null=True, blank=True)
    is_selected = models.BooleanField(null=True, blank=True)
    phone_number = models.TextField(null=True, blank=True)

    # ── New in this phase ────────────────────────────────────────────────
    sms_message_id = models.TextField(null=True, blank=True)
    delivered_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        managed = False
        db_table = "trigger_results"

    def __str__(self):
        return f"TriggerResult(user={self.user_id}, rule={self.rule_id})"
