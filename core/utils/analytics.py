# core/utils/analytics.py
"""
Shared BI computation helpers.

These are the single source of truth for:
    - yearly customer-count / revenue / CLV trends
    - yearly retention / churn rates

Both core/views_reports.py (Reports page) and core/views_dashboard.py
(Dashboard page) call these same functions so the two pages can never
silently disagree on a number. Nothing here is Django-ORM based — all
of it queries the Supabase-managed `public.*` tables directly via raw
SQL, exactly like the rest of the analytics layer.
"""

from datetime import date

from django.db import connection

from core.utils.jalali import (
    current_jalali_year,
    gregorian_to_jalali_year,
    jalali_year_to_gregorian_range,
    last_n_jalali_years,
)


# ─────────────────────────────────────────────────────────────────────────
# Yearly trends: customer count, revenue, CLV
# ─────────────────────────────────────────────────────────────────────────

_TRENDS_QUERY = """
    SELECT
        o.order_date,
        o.user_id,
        o.total_amount
    FROM public.orders o
    JOIN public.users u ON o.user_id = u.user_id
    WHERE u.tenant_id = %s
      AND o.order_date IS NOT NULL
      AND o.total_amount IS NOT NULL
"""

_TRENDS_QUERY_RANGE = """
    SELECT
        o.order_date,
        o.user_id,
        o.total_amount
    FROM public.orders o
    JOIN public.users u ON o.user_id = u.user_id
    WHERE u.tenant_id = %s
      AND o.order_date IS NOT NULL
      AND o.total_amount IS NOT NULL
      AND o.order_date >= %s
      AND o.order_date < %s
"""


_ORDER_DATE_BOUNDS_QUERY = """
    SELECT MIN(o.order_date), MAX(o.order_date)
    FROM public.orders o
    JOIN public.users u ON o.user_id = u.user_id
    WHERE u.tenant_id = %s
      AND o.order_date IS NOT NULL
      AND o.total_amount IS NOT NULL
"""


def get_trend_years(tenant_id: int) -> list[int]:
    """
    Jalali years the yearly trends chart should cover for this tenant:
    every year from the tenant's earliest order up to the current Jalali
    year (or the latest order year, if that is somehow later), oldest
    first, so years without orders in between are zero-filled.

    A tenant with no orders yet falls back to the last 4 Jalali years so
    the chart still has an axis to render.
    """
    with connection.cursor() as cursor:
        cursor.execute(_ORDER_DATE_BOUNDS_QUERY, [tenant_id])
        first, last = cursor.fetchone() or (None, None)

    if first is None or last is None:
        return last_n_jalali_years(4)

    if isinstance(first, str):
        first = date.fromisoformat(first[:10])
    if isinstance(last, str):
        last = date.fromisoformat(last[:10])

    first_year = gregorian_to_jalali_year(first)
    last_year = max(gregorian_to_jalali_year(last), current_jalali_year())
    return list(range(first_year, last_year + 1))


def get_yearly_trends(tenant_id: int, years: list[int] | None = None) -> dict[int, dict]:
    """
    Returns {jalali_year: {"customer_count": int, "revenue": float, "clv": float}}
    for every year in `years`. Years with no orders are zero-filled.

    If `years` is None, defaults to get_trend_years(tenant_id): the
    tenant's full history, from its earliest order year to the current
    Jalali year.
    """
    if years is None:
        years = get_trend_years(tenant_id)

    # Fetch only rows inside the requested year span, to avoid pulling a
    # tenant's entire order history when only 4 years are needed.
    min_year, max_year = min(years), max(years)
    span_start, _ = jalali_year_to_gregorian_range(min_year)
    _, span_end = jalali_year_to_gregorian_range(max_year)

    with connection.cursor() as cursor:
        cursor.execute(_TRENDS_QUERY_RANGE, [tenant_id, span_start, span_end])
        rows = cursor.fetchall()

    buckets: dict[int, dict] = {y: {"revenue": 0.0, "users": set()} for y in years}

    for order_date, user_id, total_amount in rows:
        if isinstance(order_date, str):
            order_date = date.fromisoformat(order_date)
        jyear = gregorian_to_jalali_year(order_date)
        if jyear not in buckets:
            continue  # outside requested years, ignore
        buckets[jyear]["revenue"] += float(total_amount)
        buckets[jyear]["users"].add(user_id)

    result = {}
    for y in years:
        b = buckets[y]
        customer_count = len(b["users"])
        revenue = round(b["revenue"], 2)
        clv = round(revenue / customer_count, 2) if customer_count > 0 else 0.0
        result[y] = {
            "customer_count": customer_count,
            "revenue": revenue,
            "clv": clv,
        }
    return result


def get_yearly_trends_for_month(
    tenant_id: int, jalali_year: int, jalali_month: int
) -> dict:
    """
    Same shape as one entry of get_yearly_trends(), but scoped to a single
    Jalali month instead of a full year. Used when the frontend's سال
    dropdown has a specific month selected.
    """
    from core.utils.jalali import jalali_month_to_gregorian_range

    start, end = jalali_month_to_gregorian_range(jalali_year, jalali_month)

    with connection.cursor() as cursor:
        cursor.execute(_TRENDS_QUERY_RANGE, [tenant_id, start, end])
        rows = cursor.fetchall()

    revenue = 0.0
    users = set()
    for _, user_id, total_amount in rows:
        revenue += float(total_amount)
        users.add(user_id)

    customer_count = len(users)
    revenue = round(revenue, 2)
    clv = round(revenue / customer_count, 2) if customer_count > 0 else 0.0

    return {
        "customer_count": customer_count,
        "revenue": revenue,
        "clv": clv,
    }


# ─────────────────────────────────────────────────────────────────────────
# Yearly retention / churn
# ─────────────────────────────────────────────────────────────────────────

_DISTINCT_CUSTOMERS_PER_YEAR_QUERY = """
    SELECT o.order_date, o.user_id
    FROM public.orders o
    JOIN public.users u ON o.user_id = u.user_id
    WHERE u.tenant_id = %s
      AND o.order_date IS NOT NULL
"""


def get_yearly_retention(
    tenant_id: int, years: list[int] | None = None
) -> list[dict]:
    """
    Implements the exact retention/churn algorithm the product owner
    specified:

        For Jalali year Y:
            customers_Y = distinct user_ids with >=1 order in year Y
            customers_Y+1 = distinct user_ids with >=1 order in year Y+1
            retained = customers_Y ∩ customers_Y+1
            retention_rate = len(retained) / len(customers_Y) * 100
            churn_rate = 100 - retention_rate

    The most recent year in the dataset has no "next year" yet, so it is
    OMITTED from the result entirely (per product decision) — there is
    no null-filled placeholder for it.

    If `years` is None, computes over every Jalali year that has at least
    one order for this tenant (full history), which is what the Reports
    page needs. Callers that want a bounded window (e.g. last 4 years)
    should pass `years` explicitly.

    Returns a list of dicts, oldest year first:
        [{"jalali_year": 1402, "customers": 1000, "retained": 300,
          "retention_rate_percent": 30.0, "churn_rate_percent": 70.0}, ...]
    """
    with connection.cursor() as cursor:
        cursor.execute(_DISTINCT_CUSTOMERS_PER_YEAR_QUERY, [tenant_id])
        rows = cursor.fetchall()

    # Group distinct user_ids per Jalali year.
    users_by_year: dict[int, set] = {}
    for order_date, user_id in rows:
        if isinstance(order_date, str):
            order_date = date.fromisoformat(order_date)
        jyear = gregorian_to_jalali_year(order_date)
        users_by_year.setdefault(jyear, set()).add(user_id)

    if not users_by_year:
        return []

    if years is None:
        candidate_years = sorted(users_by_year.keys())
    else:
        candidate_years = sorted(years)

    result = []
    for y in candidate_years:
        customers_y = users_by_year.get(y, set())
        customers_next = users_by_year.get(y + 1)

        # No data at all for year y+1 in this tenant's history yet ->
        # not computable, omit (covers both "most recent year" and any
        # gap year with zero orders in y+1).
        if customers_next is None or not customers_y:
            continue

        retained = customers_y & customers_next
        denom = len(customers_y)
        retention_rate = round(len(retained) / denom * 100, 1) if denom else 0.0
        churn_rate = round(100.0 - retention_rate, 1)

        result.append({
            "jalali_year": y,
            "customers": denom,
            "retained": len(retained),
            "retention_rate_percent": retention_rate,
            "churn_rate_percent": churn_rate,
        })

    return result
