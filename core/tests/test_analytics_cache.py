from types import SimpleNamespace
from unittest.mock import patch

from django.test import override_settings

from core.utils.analytics_cache import get_cached_payload, set_cached_payload
from core.views_reports import SalesRangeReportView

LOCMEM_CACHE = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": "analytics-cache-tests",
    }
}


@override_settings(CACHES=LOCMEM_CACHE, ANALYTICS_CACHE_TTL_SECONDS=3600)
def test_cached_payload_has_a_freshness_window():
    with patch("core.utils.analytics_cache.time.time", return_value=1000):
        set_cached_payload(7, "segments", {"value": 1})

    with patch("core.utils.analytics_cache.time.time", return_value=1059):
        payload, is_stale = get_cached_payload(7, "segments", fresh_seconds=60)
    assert payload == {"value": 1}
    assert is_stale is False

    with patch("core.utils.analytics_cache.time.time", return_value=1060):
        payload, is_stale = get_cached_payload(7, "segments", fresh_seconds=60)
    assert payload == {"value": 1}
    assert is_stale is True


@override_settings(
    CACHES=LOCMEM_CACHE,
    ANALYTICS_CACHE_TTL_SECONDS=3600,
    ANALYTICS_REPORT_FRESH_SECONDS=60,
)
def test_report_view_returns_cached_payload_without_querying_database():
    payload = {"buckets": [{"key": "cached", "order_count": 42}]}
    set_cached_payload(9, "sales-ranges", payload)
    request = SimpleNamespace(
        user=SimpleNamespace(tenant=SimpleNamespace(id=9)),
        query_params={},
    )

    with patch("core.views_reports.connection.cursor") as cursor:
        response = SalesRangeReportView().get(request)

    cursor.assert_not_called()
    assert response.data == payload
