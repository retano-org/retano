# core/views_campaign_stats.py
"""
Campaign detail stats endpoint (صفحه جزئیات کمپین / گزارش کمپین).
    GET /api/v1/campaigns/{id}/stats/
"""

from django.db import connection
from django.shortcuts import get_object_or_404
from rest_framework import permissions, status
from rest_framework.response import Response
from rest_framework.views import APIView

from core.models import Campaign
from core.models_supabase import TriggerResult
from core.schema import CAMPAIGN_DETAIL_STATS_SCHEMA   

# How many whole calendar days approximate the "exactly 72 complete hours"
# window, per the product decision to approximate using orders.order_date
# (DATE only, no time-of-day). 3 days covers the send day plus the three
# days that follow it, which is the closest whole-day approximation of a
# 72-hour window measured from an arbitrary time-of-day.
SALES_WINDOW_DAYS = 3


@CAMPAIGN_DETAIL_STATS_SCHEMA
class CampaignDetailStatsView(APIView):
    """GET /api/v1/campaigns/{id}/stats/"""

    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, pk=None):
        campaign = get_object_or_404(
            Campaign.objects.filter(tenant__owner=request.user), pk=pk
        )

        trigger_qs = TriggerResult.objects.filter(rule_id=campaign.id)

        targeted_users = trigger_qs.count()

        sms_sent, sms_delivered, customer_count, order_count, sales_amount = _campaign_results(
            campaign.id
        )

        sms_delivery_rate_percent = (
            round(sms_delivered / sms_sent * 100, 1) if sms_sent > 0 else 0.0
        )
        conversion_rate_percent = (
            round(customer_count / sms_delivered * 100, 1)
            if sms_delivered > 0
            else 0.0
        )

        return Response(
            {
                "campaign_id": campaign.id,
                "targeted_users": targeted_users,
                "customer_count": customer_count,
                "order_count": order_count,
                "sales_amount": sales_amount,
                "sms_sent": sms_sent,
                "sms_delivered": sms_delivered,
                "sms_delivery_rate_percent": sms_delivery_rate_percent,
                "conversion_rate_percent": conversion_rate_percent,
            },
            status=status.HTTP_200_OK,
        )


_CAMPAIGN_RESULTS_QUERY = """
    WITH sends AS (
        SELECT user_id, sent_at, delivered_at FROM trigger_results
        WHERE rule_id = %s AND sent_at IS NOT NULL
        UNION ALL
        SELECT user_id, sent_at, delivered_at FROM core_sentcampaignmessage
        WHERE rule_id = %s
    ),
    matched_orders AS (
        SELECT DISTINCT o.order_id, o.user_id, o.total_amount
        FROM sends s
        JOIN orders o
            ON o.user_id = s.user_id
            AND o.order_date >= s.sent_at::date
            AND o.order_date <= s.sent_at::date + %s
    )
    SELECT
        (SELECT COUNT(*) FROM sends),
        (SELECT COUNT(delivered_at) FROM sends),
        COUNT(DISTINCT user_id)      AS customer_count,
        COUNT(order_id)             AS order_count,
        COALESCE(SUM(total_amount), 0) AS sales_amount
    FROM matched_orders
"""


def _campaign_results(campaign_id: int) -> tuple[int, int, int, int, float]:
    """
    Returns send counts and (customer_count, order_count, sales_amount) for this campaign's
    targeted users, each evaluated against that specific user's OWN send
    window [DATE(sent_at), DATE(sent_at) + SALES_WINDOW_DAYS].

    Single set-based query — the per-row comparison `o.order_date >=
    tr.sent_at::date` is exactly what SQL joins are for; there is no
    per-user loop here, so this scales the same way whether a campaign
    targets 50 users or 50,000. Raw SQL (not the ORM) because this is a
    cross-table join entirely within Supabase-native tables
    (trigger_results, orders) with a per-row correlated date range that
    the ORM has no natural expression for — the same reason
    core/views_reports.py and core/utils/analytics.py use raw SQL for
    their own cross-schema joins rather than forcing it through Django's
    query builder.

    tenant scoping is implicit: trigger_results.rule_id is already scoped
    to one campaign (and the view resolves that campaign against
    request.user's own tenant before this is ever called), and
    orders.user_id only ever contains users belonging to that same
    tenant's uploaded customer data — there is no cross-tenant leakage
    risk from joining on user_id alone here.
    """
    with connection.cursor() as cursor:
        cursor.execute(_CAMPAIGN_RESULTS_QUERY, [campaign_id, campaign_id, SALES_WINDOW_DAYS])
        sent, delivered, customer_count, order_count, sales_amount = cursor.fetchone()

    return (
        int(sent),
        int(delivered),
        int(customer_count or 0),
        int(order_count or 0),
        round(float(sales_amount or 0), 2),
    )
