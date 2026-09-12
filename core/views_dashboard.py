# core/views_dashboard.py
"""
Dashboard page (داشبورد) — single aggregation endpoint.

Charts/cards on this page and where their data comes from:

    فروش ماه                  -> public.orders, current Jalali month
    مشتری فعال / غیرفعال      -> public.user_summary.rfm_segment
    نرخ نگهداری / نرخ ریزش    -> core.utils.analytics.get_yearly_retention
                                  (SAME function the Reports page uses)
    کمپین: کل/فعال/پایان‌یافته/حذف‌شده -> core.models.Campaign (Django ORM)
    دسته‌بندی RFM              -> public.user_summary.rfm_segment
    پرفروش‌ترین محصولات/خدمات -> public.order_items + public.products,
                                  ranked by total revenue
    موجودی پیامک              -> CustomUser.num_available_sms
    Notification unread badge -> notifications.models.Notification
                                  (unread count for this tenant; replaces
                                  the old tickets.Thread.unread_count() —
                                  the tickets app no longer exists)

Cached per tenant. Stale snapshots are returned immediately and refreshed by
Celery so navigation never waits for a repeated aggregation.
"""

from datetime import date

from django.conf import settings
from django.db import connection

from rest_framework import permissions, status
from rest_framework.response import Response
from rest_framework.views import APIView
from core.schema import DASHBOARD_SCHEMA
from core.models import Campaign
from core.utils.jalali import (
    jalali_month_to_gregorian_range,
)
from core.utils.analytics import get_yearly_retention
from core.utils.analytics_cache import (
    get_cached_payload,
    schedule_analytics_refresh,
    set_cached_payload,
)
from notifications.models import Notification

import jdatetime

DASHBOARD_CACHE_NAME = "dashboard"


# ─────────────────────────────────────────────────────────────────────────────
# Campaign status definitions (from product spec):
#   کمپین فعال       -> is_active=True AND start_date <= today <= end_date
#                        (more precisely: start has passed, end has not)
#   کمپین پایان‌یافته -> campaign_end_date < today (regardless of is_active)
#   کمپین حذف‌شده     -> is_active = False
#                        (there is no real delete — the "حذف" button only
#                        flips is_active to False)
#   کل کمپین‌ها       -> all campaigns for this tenant, no filter
# ─────────────────────────────────────────────────────────────────────────────

@DASHBOARD_SCHEMA
class DashboardView(APIView):
    """
    GET /api/v1/dashboard/

    Returns all data needed to render the دشبورد page in one request.
    Response is cached per tenant. Stale data is served while Celery refreshes
    the snapshot in the background.
    """

    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        user = request.user
        try:
            tenant = user.tenant
        except AttributeError:
            return Response(
                {"detail": "Tenant record not found for this user."},
                status=status.HTTP_403_FORBIDDEN,
            )

        tenant_id = tenant.id
        force_refresh = getattr(request, "_analytics_force_refresh", False)
        if not force_refresh:
            cached, is_stale = get_cached_payload(
                tenant_id,
                DASHBOARD_CACHE_NAME,
                fresh_seconds=settings.DASHBOARD_CACHE_FRESH_SECONDS,
            )
            if cached is not None:
                if is_stale:
                    schedule_analytics_refresh(tenant_id)
                return Response(cached, status=status.HTTP_200_OK)

        data = {}

        # ── 1. Campaign counts ────────────────────────────────────────────
        today = date.today()
        campaigns_qs = Campaign.objects.filter(tenant=tenant)

        # کمپین فعال: start date has passed, end date has not, AND
        # not soft-deleted (is_active still True — the same flag the
        # "حذف" button flips).
        active_campaigns = campaigns_qs.filter(
            is_active=True,
            campaign_start_date__lte=today,
            campaign_end_date__gte=today,
        ).count()

        # کمپین پایان‌یافته: end date has already passed. This is purely
        # date-based — a campaign can be "ended" whether or not it was
        # also soft-deleted, per spec ("a campaign whose end date has
        # already passed").
        ended_campaigns = campaigns_qs.filter(
            campaign_end_date__lt=today,
        ).count()

        # کمپین حذف‌شده: is_active == False. The حذف button never deletes
        # the row — it only flips this flag.
        deleted_campaigns = campaigns_qs.filter(
            is_active=False,
        ).count()

        total_campaigns = campaigns_qs.count()

        data["campaigns"] = {
            "total": total_campaigns,
            "active": active_campaigns,
            "ended": ended_campaigns,
            "deleted": deleted_campaigns,
        }

        # ── 2. Active / inactive customers ────────────────────────────────
        # مشتری فعال: rfm_segment IN (vip, new, active)
        # مشتری غیرفعال: rfm_segment IN (churned, at_risk)
        # Only rows with a non-null rfm_segment are counted, matching the
        # Reports page's ActiveUsersReportView so the two pages agree.
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT
                    COUNT(*) FILTER (
                        WHERE us.rfm_segment IN ('vip', 'new', 'active')
                    ) AS active_count,
                    COUNT(*) FILTER (
                        WHERE us.rfm_segment IN ('churned', 'at_risk')
                    ) AS inactive_count
                FROM public.user_summary us
                JOIN public.users u ON us.user_id = u.user_id
                WHERE u.tenant_id = %s
                  AND us.rfm_segment IS NOT NULL
                """,
                [tenant_id],
            )
            row = cursor.fetchone()

        active_customers = int(row[0] or 0)
        inactive_customers = int(row[1] or 0)
        total_customers = active_customers + inactive_customers

        data["customers"] = {
            "active": active_customers,
            "inactive": inactive_customers,
            "total": total_customers,
        }

        # ── 3. Monthly sales (فروش ماه) ───────────────────────────────────
        # Current Jalali month, in tomans.
        today_j = jdatetime.date.today()
        month_start, month_end = jalali_month_to_gregorian_range(
            today_j.year, today_j.month
        )
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT COALESCE(SUM(o.total_amount), 0)
                FROM public.orders o
                JOIN public.users u ON o.user_id = u.user_id
                WHERE u.tenant_id = %s
                  AND o.order_date >= %s
                  AND o.order_date < %s
                """,
                [tenant_id, month_start, month_end],
            )
            monthly_sales = float(cursor.fetchone()[0] or 0)

        data["monthly_sales"] = round(monthly_sales, 2)

        # ── 4. Top 4 best-selling products (پرفروش‌ترین محصولات/خدمات) ────
        # Ranked by TOTAL REVENUE (sum of order_items.subtotal), not
        # quantity. Y-axis value returned is that same total revenue,
        # in tomans.
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT
                    p.name                          AS product_name,
                    COALESCE(SUM(oi.subtotal), 0)   AS total_revenue
                FROM public.order_items oi
                JOIN public.orders o   ON oi.order_id   = o.order_id
                JOIN public.users  u   ON o.user_id     = u.user_id
                JOIN public.products p ON oi.product_id = p.product_id
                WHERE u.tenant_id = %s
                GROUP BY p.product_id, p.name
                ORDER BY total_revenue DESC
                LIMIT 4
                """,
                [tenant_id],
            )
            rows = cursor.fetchall()

        data["top_products"] = [
            {"name": row[0], "total_revenue": float(row[1])}
            for row in rows
        ]

        # ── 5. Yearly retention & churn ───────────────────────────────────
        # Uses the SAME function as the Reports page's RetentionReportView
        # (core.utils.analytics.get_yearly_retention) — no duplicated SQL,
        # no risk of the two pages disagreeing. Returns every computable
        # year (most recent year omitted, no padding to a fixed count).
        data["monthly_trends"] = get_yearly_retention(tenant_id)

        # ── 6. RFM segment distribution ───────────────────────────────────
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT
                    us.rfm_segment,
                    COUNT(*) AS count
                FROM public.user_summary us
                JOIN public.users u ON us.user_id = u.user_id
                WHERE u.tenant_id = %s
                  AND us.rfm_segment IS NOT NULL
                GROUP BY us.rfm_segment
                """,
                [tenant_id],
            )
            segment_rows = cursor.fetchall()

        segment_map = {row[0]: int(row[1]) for row in segment_rows}
        data["rfm_segments"] = {
            "vip": segment_map.get("vip", 0),
            "active": segment_map.get("active", 0),
            "new": segment_map.get("new", 0),
            "at_risk": segment_map.get("at_risk", 0),
            "churned": segment_map.get("churned", 0),
        }

        # ── 7. SMS balance ────────────────────────────────────────────────
        data["sms_balance"] = user.num_available_sms

        # ── 8. Notification unread count (dashboard badge) ────────────────
        # Replaces the old tickets.Thread.unread_count() — the tickets app
        # no longer exists. A tenant may have zero notifications at all,
        # which is not an error case (unlike the old Thread, which was
        # get_or_create'd lazily and could legitimately be missing).
        data["support_unread_count"] = Notification.objects.filter(
            tenant=tenant, is_read=False
        ).count()

        # ── Cache and return ──────────────────────────────────────────────
        set_cached_payload(tenant_id, DASHBOARD_CACHE_NAME, data)
        return Response(data, status=status.HTTP_200_OK)
