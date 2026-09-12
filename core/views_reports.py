# core/views_reports.py
"""
Reports page endpoints (گزارش‌ها).

Six charts on that page, and the endpoints that back them:

    تعداد مشتریان / میزان فروش / CLV   -> GET /api/v1/reports/trends/
    بازه‌های فروش                        -> GET /api/v1/reports/sales-ranges/
    دسته‌بندی RFM                        -> GET /api/v1/reports/segments/
    درصد کاربران فعال                    -> GET /api/v1/reports/active-users/

Retention/Churn is NOT one of the six report-page charts (it lives on
the dashboard page), but the shared calculation lives in
core/utils/analytics.py and is exposed here too for completeness /
future reuse — see RetentionReportView.

All views are tenant-scoped via request.user.tenant. None of these
return raw model instances; they are thin aggregation layers over
public.* Supabase tables (or, for segments/active-users, over
public.user_summary).
"""

from rest_framework import permissions, status
from rest_framework.response import Response
from rest_framework.views import APIView

from django.conf import settings
from django.db import connection
from core.schema import TRENDS_REPORT_SCHEMA, SALES_RANGE_REPORT_SCHEMA, SEGMENTS_REPORT_SCHEMA, ACTIVE_USERS_REPORT_SCHEMA, RETENTION_REPORT_SCHEMA
from core.utils.jalali import last_n_jalali_years
from core.utils.analytics import (
    get_yearly_trends,
    get_yearly_trends_for_month,
    get_yearly_retention,
)
from core.utils.analytics_cache import (
    get_cached_payload,
    schedule_analytics_refresh,
    set_cached_payload,
)


def _get_tenant_id(request):
    """
    Returns (tenant_id, error_response). error_response is None on success.
    Mirrors the guard the original code had for superusers created without
    the Tenant-creation signal firing.
    """
    try:
        return request.user.tenant.id, None
    except AttributeError:
        return None, Response(
            {"detail": "Tenant record not found for this user."},
            status=status.HTTP_403_FORBIDDEN,
        )


def _cached_report_response(request, tenant_id: int, report: str):
    if getattr(request, "_analytics_force_refresh", False):
        return None

    payload, is_stale = get_cached_payload(
        tenant_id,
        report,
        fresh_seconds=settings.ANALYTICS_REPORT_FRESH_SECONDS,
    )
    if payload is None:
        return None
    if is_stale:
        schedule_analytics_refresh(tenant_id)
    return Response(payload, status=status.HTTP_200_OK)


def _report_response(tenant_id: int, report: str, payload: dict):
    set_cached_payload(tenant_id, report, payload)
    return Response(payload, status=status.HTTP_200_OK)


# ─────────────────────────────────────────────────────────────────────────────
# Segment label mapping
# ─────────────────────────────────────────────────────────────────────────────

SEGMENT_LABEL_MAP: dict[str, str] = {
    "new": "تازه وارد",
    "active": "فعال",
    "vip": "ویژه",
    "at_risk": "در خطر ریزش",
    "churned": "از دست رفته",
}

# Canonical display order — stable shape for the frontend regardless of
# which segments actually have users. The frontend always receives all 5.
SEGMENT_ORDER = ["vip", "active", "new", "at_risk", "churned"]

# Segments counted as "active" for the درصد کاربران فعال chart.
ACTIVE_SEGMENTS = {"vip", "new", "active"}


# ─────────────────────────────────────────────────────────────────────────────
# CLV
# ─────────────────────────────────────────────────────────────────────────────

@TRENDS_REPORT_SCHEMA
class TrendsReportView(APIView):
    """
    GET /api/v1/reports/trends/

    Backs all three of: تعداد مشتریان, میزان فروش, CLV.
    The frontend renders the same response into three different charts by
    picking a different field per bar (customer_count / revenue / clv) —
    there is intentionally one endpoint, not three, since all three are
    the exact same year/month bucketing over the exact same order rows.

    Two modes, selected by query params:

    1) Yearly (default — سال dropdown left unselected):
        GET /api/v1/reports/trends/
        Returns the last 4 Jalali years ending at the current Jalali year
        (e.g. 1402, 1403, 1404, 1405), zero-filled for years with no
        orders. This is a fixed window — NOT "all years in the DB".

    2) Monthly (سال dropdown has a specific month selected):
        GET /api/v1/reports/trends/?granularity=month
        Returns the last 6 Jalali months (including the current month)
        as separate data points — i.e. selecting "month mode" re-renders
        the whole chart as 6 monthly bars rather than returning a single
        value, per product decision.

    Response shape (yearly):
        {
            "granularity": "year",
            "data": [
                {"jalali_year": 1402, "customer_count": 312,
                 "revenue": 4820000000.0, "clv": 15448717.95},
                ...
            ]
        }

    Response shape (monthly):
        {
            "granularity": "month",
            "data": [
                {"jalali_year": 1405, "jalali_month": 2, "month_name": "اردیبهشت",
                 "customer_count": 40, "revenue": 120000000.0, "clv": 3000000.0},
                ...
            ]
        }
    """

    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        tenant_id, error = _get_tenant_id(request)
        if error:
            return error

        granularity = request.query_params.get("granularity", "year")

        if granularity not in ("year", "month"):
            return Response(
                {"detail": "granularity must be 'year' or 'month'."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        cache_name = f"trends:{granularity}"
        cached = _cached_report_response(request, tenant_id, cache_name)
        if cached is not None:
            return cached

        if granularity == "year":
            years = last_n_jalali_years(4)
            trends = get_yearly_trends(tenant_id, years=years)
            data = [
                {"jalali_year": y, **trends[y]}
                for y in years
            ]
            return _report_response(
                tenant_id,
                cache_name,
                {"granularity": "year", "data": data},
            )

        # granularity == "month"
        from core.utils.jalali import last_n_jalali_months, JALALI_MONTH_NAMES

        months = last_n_jalali_months(6)
        data = []
        for j_year, j_month, _, _ in months:
            point = get_yearly_trends_for_month(tenant_id, j_year, j_month)
            data.append({
                "jalali_year": j_year,
                "jalali_month": j_month,
                "month_name": JALALI_MONTH_NAMES[j_month - 1],
                **point,
            })

        return _report_response(
            tenant_id,
            cache_name,
            {"granularity": "month", "data": data},
        )


# ─────────────────────────────────────────────────────────────────────────────
# بازه‌های فروش  (order-value histogram)
# ─────────────────────────────────────────────────────────────────────────────

# Bucket boundaries in TOMANS (all money in this DB is already in tomans).
# Matches the mockup's ۰ تا ۰٫۵ / ۰٫۵ تا ۱ / ۱ تا ۳ / بالای ۳ (millions).
SALES_RANGE_BUCKETS = [
    ("0_to_0.5m", "۰ تا ۰٫۵", 0, 500_000),
    ("0.5_to_1m", "۰٫۵ تا ۱", 500_000, 1_000_000),
    ("1_to_3m", "۱ تا ۳", 1_000_000, 3_000_000),
    ("above_3m", "بالای ۳", 3_000_000, None),  # None = no upper bound
]


@SALES_RANGE_REPORT_SCHEMA
class SalesRangeReportView(APIView):
    """
    GET /api/v1/reports/sales-ranges/

    باز‌های فروش — histogram of individual order totals (orders.total_amount,
    in tomans) across this tenant's ENTIRE order history, bucketed into
    4 fixed ranges. Each order counts exactly once, in the bucket matching
    its total_amount.

    Buckets (tomans):
        0        <= total_amount < 500,000        -> "۰ تا ۰٫۵"
        500,000  <= total_amount < 1,000,000       -> "۰٫۵ تا ۱"
        1,000,000 <= total_amount < 3,000,000      -> "۱ تا ۳"
        total_amount >= 3,000,000                  -> "بالای ۳"

    Response shape:
        {
            "buckets": [
                {"key": "0_to_0.5m", "label": "۰ تا ۰٫۵", "order_count": 4},
                {"key": "0.5_to_1m", "label": "۰٫۵ تا ۱", "order_count": 6},
                {"key": "1_to_3m", "label": "۱ تا ۳", "order_count": 3},
                {"key": "above_3m", "label": "بالای ۳", "order_count": 1}
            ]
        }
    """

    permission_classes = [permissions.IsAuthenticated]

    _QUERY = """
        SELECT
            COUNT(*) FILTER (
                WHERE o.total_amount >= 0 AND o.total_amount < 500000
            ) AS bucket_0,
            COUNT(*) FILTER (
                WHERE o.total_amount >= 500000 AND o.total_amount < 1000000
            ) AS bucket_1,
            COUNT(*) FILTER (
                WHERE o.total_amount >= 1000000 AND o.total_amount < 3000000
            ) AS bucket_2,
            COUNT(*) FILTER (
                WHERE o.total_amount >= 3000000
            ) AS bucket_3
        FROM public.orders o
        JOIN public.users u ON o.user_id = u.user_id
        WHERE u.tenant_id = %s
          AND o.total_amount IS NOT NULL
    """

    def get(self, request):
        tenant_id, error = _get_tenant_id(request)
        if error:
            return error

        cache_name = "sales-ranges"
        cached = _cached_report_response(request, tenant_id, cache_name)
        if cached is not None:
            return cached

        with connection.cursor() as cursor:
            cursor.execute(self._QUERY, [tenant_id])
            row = cursor.fetchone()

        counts = [int(c or 0) for c in row]

        buckets = [
            {"key": key, "label": label, "order_count": count}
            for (key, label, _, _), count in zip(SALES_RANGE_BUCKETS, counts)
        ]

        return _report_response(
            tenant_id,
            cache_name,
            {"buckets": buckets},
        )


# ─────────────────────────────────────────────────────────────────────────────
# RFM
# ─────────────────────────────────────────────────────────────────────────────

@SEGMENTS_REPORT_SCHEMA
class SegmentsReportView(APIView):
    """
    GET /api/v1/reports/segments/

    Returns the RFM segment distribution for the authenticated tenant.

    Data source:
        public.user_summary.rfm_segment, joined to public.users for
        tenant scoping. (NOT public.the_users_summary_rfm_segmented,
        which does not exist in this schema — user_summary IS the
        RFM-segmented table.)

    Response shape:
        {
            "total_users": 20,
            "segments": [
                {"segment": "vip", "label": "ویژه", "count": 5,
                 "percentage": 25.0},
                ... (always 5 entries, zero-filled for empty segments)
            ]
        }
    """

    permission_classes = [permissions.IsAuthenticated]

    _QUERY = """
        SELECT
            us.rfm_segment,
            COUNT(*) AS count
        FROM public.user_summary us
        JOIN public.users u ON us.user_id = u.user_id
        WHERE u.tenant_id = %s
          AND us.rfm_segment IS NOT NULL
        GROUP BY us.rfm_segment
    """

    def get(self, request):
        tenant_id, error = _get_tenant_id(request)
        if error:
            return error

        cache_name = "segments"
        cached = _cached_report_response(request, tenant_id, cache_name)
        if cached is not None:
            return cached

        with connection.cursor() as cursor:
            cursor.execute(self._QUERY, [tenant_id])
            rows = cursor.fetchall()

        counts_by_segment = {row[0]: int(row[1]) for row in rows}
        total_users = sum(counts_by_segment.values())

        segments = []
        for seg_key in SEGMENT_ORDER:
            count = counts_by_segment.get(seg_key, 0)
            percentage = (
                round(count / total_users * 100, 2) if total_users > 0 else 0.0
            )
            segments.append({
                "segment": seg_key,
                "label": SEGMENT_LABEL_MAP[seg_key],
                "count": count,
                "percentage": percentage,
            })

        return _report_response(
            tenant_id,
            cache_name,
            {"total_users": total_users, "segments": segments},
        )


# ─────────────────────────────────────────────────────────────────────────────
# درصد کاربران فعال
# ─────────────────────────────────────────────────────────────────────────────

@ACTIVE_USERS_REPORT_SCHEMA
class ActiveUsersReportView(APIView):
    """
    GET /api/v1/reports/active-users/

    درصد کاربران فعال — percentage of this tenant's customers considered
    "active" vs not, based on RFM segment:

        active   = rfm_segment IN ('vip', 'new', 'active')
        inactive = rfm_segment IN ('churned', 'at_risk')

    Only users that have an rfm_segment at all are counted (mirrors
    SegmentsReportView's WHERE clause, so the two charts' totals agree).

    Response shape:
        {
            "total_users": 20,
            "active_count": 15,
            "inactive_count": 5,
            "active_percent": 75.0,
            "inactive_percent": 25.0
        }
    """

    permission_classes = [permissions.IsAuthenticated]

    _QUERY = """
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
    """

    def get(self, request):
        tenant_id, error = _get_tenant_id(request)
        if error:
            return error

        cache_name = "active-users"
        cached = _cached_report_response(request, tenant_id, cache_name)
        if cached is not None:
            return cached

        with connection.cursor() as cursor:
            cursor.execute(self._QUERY, [tenant_id])
            row = cursor.fetchone()

        active_count = int(row[0] or 0)
        inactive_count = int(row[1] or 0)
        total = active_count + inactive_count

        active_percent = round(active_count / total * 100, 1) if total > 0 else 0.0
        inactive_percent = round(100.0 - active_percent, 1) if total > 0 else 0.0

        return _report_response(
            tenant_id,
            cache_name,
            {
                "total_users": total,
                "active_count": active_count,
                "inactive_count": inactive_count,
                "active_percent": active_percent,
                "inactive_percent": inactive_percent,
            },
        )


# ─────────────────────────────────────────────────────────────────────────────
# نرخ نگهداری / نرخ ریزش  (yearly retention / churn — shared logic)
# ─────────────────────────────────────────────────────────────────────────────

@RETENTION_REPORT_SCHEMA
class RetentionReportView(APIView):
    """
    GET /api/v1/reports/retention/

    Yearly retention/churn, computed via the shared
    core.utils.analytics.get_yearly_retention() helper — the exact same
    function the Dashboard page's retention chart calls, so the two
    pages can never disagree.

    For Jalali year Y:
        customers_Y  = distinct customers with >=1 order in year Y
        retained     = customers_Y also present in year Y+1
        retention_rate = retained / customers_Y * 100
        churn_rate     = 100 - retention_rate

    The most recent year is OMITTED (no year Y+1 data exists yet to
    measure retention against) — there is no null placeholder for it.

    Response shape:
        {
            "years": [
                {"jalali_year": 1402, "customers": 1000, "retained": 300,
                 "retention_rate_percent": 30.0, "churn_rate_percent": 70.0},
                ...
            ]
        }
    """

    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        tenant_id, error = _get_tenant_id(request)
        if error:
            return error

        cache_name = "retention"
        cached = _cached_report_response(request, tenant_id, cache_name)
        if cached is not None:
            return cached

        years_data = get_yearly_retention(tenant_id)
        return _report_response(
            tenant_id,
            cache_name,
            {"years": years_data},
        )
