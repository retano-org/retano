# core/tests/test_reports_trends.py
"""
Tests for GET /api/v1/reports/trends/

Same two-layer strategy as segments:
    Layer 1 — contract tests (this file): auth, shape, validation.
    Layer 2 — integration tests: seed orders, assert actual year buckets.
"""

from datetime import date
from unittest import mock

import jdatetime
from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase
from rest_framework import status
from rest_framework.test import APIClient

User = get_user_model()

TRENDS_URL = "/api/v1/reports/trends/"
EXPECTED_YEAR_KEYS = {"jalali_year", "customer_count", "revenue", "clv"}


class TrendsReportAuthTests(TestCase):

    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(
            phone_number="+989121111111", password="testpass123"
        )

    def test_unauthenticated_returns_401(self):
        response = self.client.get(TRENDS_URL)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_authenticated_returns_200(self):
        self.client.force_authenticate(user=self.user)
        response = self.client.get(TRENDS_URL)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_post_not_allowed(self):
        self.client.force_authenticate(user=self.user)
        response = self.client.post(TRENDS_URL, {})
        self.assertEqual(response.status_code, status.HTTP_405_METHOD_NOT_ALLOWED)


class TrendsReportShapeTests(TestCase):

    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(
            phone_number="+989121111111", password="testpass123"
        )
        self.client.force_authenticate(user=self.user)

    def test_top_level_keys(self):
        data = self.client.get(TRENDS_URL).json()
        self.assertEqual(data["granularity"], "year")
        self.assertIsInstance(data["data"], list)

    def test_empty_tenant_falls_back_to_last_four_zero_filled_years(self):
        # No orders seeded: the chart still gets an axis of 4 zeroed years.
        data = self.client.get(TRENDS_URL).json()["data"]
        self.assertEqual(len(data), 4)
        for entry in data:
            self.assertEqual(entry["customer_count"], 0)
            self.assertEqual(entry["revenue"], 0)
            self.assertEqual(entry["clv"], 0)

    def test_each_year_entry_has_required_keys(self):
        data = self.client.get(TRENDS_URL).json()
        for entry in data["data"]:
            self.assertEqual(set(entry.keys()), EXPECTED_YEAR_KEYS)

    def test_years_sorted_ascending(self):
        data = self.client.get(TRENDS_URL).json()
        years = [e["jalali_year"] for e in data["data"]]
        self.assertEqual(years, sorted(years))

    def test_invalid_granularity_returns_400(self):
        response = self.client.get(TRENDS_URL, {"granularity": "week"})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)


class TrendYearSpanTests(SimpleTestCase):
    """get_trend_years covers the tenant's full history, not a fixed window."""

    def _years(self, bounds, today_j_year=1405):
        from core.utils import analytics

        fake_connection = mock.MagicMock()
        fake_connection.cursor.return_value.__enter__.return_value.fetchone.return_value = bounds
        with mock.patch.object(
            analytics, "connection", fake_connection
        ), mock.patch.object(
            analytics, "current_jalali_year", return_value=today_j_year
        ):
            return analytics.get_trend_years(tenant_id=1)

    def test_spans_earliest_order_year_to_current_year(self):
        first = jdatetime.date(1400, 3, 5).togregorian()
        last = jdatetime.date(1402, 12, 20).togregorian()
        self.assertEqual(
            self._years((first, last)), [1400, 1401, 1402, 1403, 1404, 1405]
        )

    def test_accepts_iso_strings(self):
        self.assertEqual(
            self._years(("2021-06-01", "2023-06-01")),
            [1400, 1401, 1402, 1403, 1404, 1405],
        )

    def test_no_orders_falls_back_to_last_four_years(self):
        with mock.patch(
            "core.utils.analytics.last_n_jalali_years",
            return_value=[1402, 1403, 1404, 1405],
        ):
            self.assertEqual(self._years((None, None)), [1402, 1403, 1404, 1405])

    def test_latest_order_after_current_year_is_included(self):
        first = jdatetime.date(1404, 1, 1).togregorian()
        last = jdatetime.date(1406, 1, 1).togregorian()
        self.assertEqual(self._years((first, last)), [1404, 1405, 1406])


class FlexibleDateParserJalaliTests(SimpleTestCase):
    """Jalali dates in uploads must be stored as the right Gregorian date."""

    def parse(self, value):
        from core.utils.date_parser import FlexibleDateParser

        return FlexibleDateParser.parse_date(value)

    def expected(self, y, m, d):
        return jdatetime.date(y, m, d).togregorian().strftime("%Y-%m-%d")

    def test_slash_dash_and_dot_separators(self):
        for text in ("1401/05/12", "1401-05-12", "1401.5.12"):
            self.assertEqual(self.parse(text), self.expected(1401, 5, 12), text)

    def test_persian_and_arabic_digits(self):
        self.assertEqual(self.parse("۱۴۰۰/۰۱/۰۱"), self.expected(1400, 1, 1))
        self.assertEqual(self.parse("١٤٠٢/١٢/٢٩"), self.expected(1402, 12, 29))

    def test_trailing_time_is_ignored(self):
        self.assertEqual(
            self.parse("1402/08/12 10:30"), self.expected(1402, 8, 12)
        )

    def test_result_lands_in_the_same_jalali_year(self):
        from core.utils.jalali import gregorian_to_jalali_year

        for y in (1400, 1401, 1402):
            iso = self.parse(f"{y}/07/15")
            self.assertEqual(gregorian_to_jalali_year(date.fromisoformat(iso)), y)

    def test_invalid_jalali_date_raises(self):
        with self.assertRaises(ValueError):
            self.parse("1402/13/01")

    def test_gregorian_strings_are_untouched(self):
        self.assertEqual(self.parse("2022-08-03"), "2022-08-03")


class TrendsReportTenantIsolationTests(TestCase):

    def setUp(self):
        self.client_a = APIClient()
        self.client_b = APIClient()
        self.user_a = User.objects.create_user(
            phone_number="+989121111111", password="testpass123"
        )
        self.user_b = User.objects.create_user(
            phone_number="+989122222222", password="testpass123"
        )
        self.client_a.force_authenticate(user=self.user_a)
        self.client_b.force_authenticate(user=self.user_b)

    def test_both_tenants_receive_200(self):
        self.assertEqual(self.client_a.get(TRENDS_URL).status_code, 200)
        self.assertEqual(self.client_b.get(TRENDS_URL).status_code, 200)

    def test_tenants_have_independent_ids(self):
        self.assertNotEqual(self.user_a.tenant.id, self.user_b.tenant.id)