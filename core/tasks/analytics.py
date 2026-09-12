"""Background refresh of tenant dashboard and analytics snapshots."""

from types import SimpleNamespace

from celery import shared_task

from core.models import Tenant
from core.utils.analytics_cache import release_analytics_refresh_lock


@shared_task(name="core.tasks.analytics.refresh_tenant_analytics_cache")
def refresh_tenant_analytics_cache(tenant_id: int):
    """Recompute every cached analytics response without blocking a request."""

    try:
        try:
            tenant = Tenant.objects.select_related("owner").get(pk=tenant_id)
        except Tenant.DoesNotExist:
            return

        # Imports stay local to avoid loading API schema/view modules during
        # Celery task discovery.
        from core.views_dashboard import DashboardView
        from core.views_reports import (
            ActiveUsersReportView,
            RetentionReportView,
            SalesRangeReportView,
            SegmentsReportView,
            TrendsReportView,
        )

        requests_and_views = [
            ({}, DashboardView()),
            ({"granularity": "year"}, TrendsReportView()),
            ({"granularity": "month"}, TrendsReportView()),
            ({}, SalesRangeReportView()),
            ({}, SegmentsReportView()),
            ({}, ActiveUsersReportView()),
            ({}, RetentionReportView()),
        ]

        for query_params, view in requests_and_views:
            request = SimpleNamespace(
                user=tenant.owner,
                query_params=query_params,
                _analytics_force_refresh=True,
            )
            response = view.get(request)
            if response.status_code != 200:
                raise RuntimeError(
                    f"Analytics refresh failed for {view.__class__.__name__}: "
                    f"HTTP {response.status_code}"
                )
    finally:
        release_analytics_refresh_lock(tenant_id)
