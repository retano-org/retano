from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase

from core.models import ManualUploadPair, UploadJob
from core.services import manual_uploads


class ManualUploadPairTests(SimpleTestCase):
    @patch.object(manual_uploads, "upload_fileobj_to_storage")
    @patch.object(manual_uploads, "lock_tenant")
    @patch.object(manual_uploads, "ManualUploadPair")
    def test_missing_or_expired_product_window_rejects_customer_before_storage(
        self, pair_model, _tenant_lock, store_file,
    ):
        pair_model.Status = ManualUploadPair.Status
        pairs = pair_model.objects.filter.return_value
        pairs.filter.return_value.exists.return_value = False
        pairs.filter.return_value.first.return_value = None

        with self.assertRaises(manual_uploads.ManualUploadConflict) as error:
            manual_uploads.queue_manual_upload.__wrapped__(
                SimpleNamespace(pk=1), UploadJob.UploadType.CUSTOMERS,
                SimpleNamespace(name="customers.xlsx"), {}, MagicMock(),
            )

        self.assertEqual(error.exception.code, "product_upload_required")
        self.assertIn("customers_due_at__gt", pairs.filter.call_args.kwargs)
        store_file.assert_not_called()

    @patch.object(manual_uploads, "upload_fileobj_to_storage")
    @patch.object(manual_uploads, "lock_tenant")
    @patch.object(manual_uploads, "ManualUploadPair")
    def test_second_file_is_rejected_while_the_tenants_upload_is_active(
        self, pair_model, _tenant_lock, store_file,
    ):
        pair_model.objects.filter.return_value.filter.return_value.exists.return_value = True

        with self.assertRaises(manual_uploads.ManualUploadConflict) as error:
            manual_uploads.queue_manual_upload.__wrapped__(
                SimpleNamespace(pk=1), UploadJob.UploadType.PRODUCTS,
                SimpleNamespace(name="products.xlsx"), {}, MagicMock(),
            )

        self.assertEqual(error.exception.code, "upload_in_progress")
        store_file.assert_not_called()

    @patch.object(manual_uploads, "timezone")
    @patch.object(manual_uploads, "ManualUploadPair")
    def test_product_completion_opens_the_window(self, pair_model, clock):
        pair_model.Status = ManualUploadPair.Status
        finished_at = datetime(2026, 11, 23, 10, tzinfo=timezone.utc)
        clock.now.return_value = finished_at
        pair = MagicMock(tenant_id=1, status=ManualUploadPair.Status.PRODUCTS_PROCESSING)

        manual_uploads.mark_pair_imported(pair)

        self.assertEqual(pair.customers_due_at, finished_at + timedelta(minutes=60))
        self.assertEqual(pair.status, ManualUploadPair.Status.AWAITING_CUSTOMERS)
        pair_model.objects.filter.return_value.update.assert_called_once_with(
            status=ManualUploadPair.Status.SUPERSEDED
        )

    @patch.object(manual_uploads, "connection")
    @patch.object(manual_uploads, "lock_tenant")
    @patch.object(manual_uploads, "ManualUploadPair")
    def test_batch_runs_serially_only_after_both_staging_checks(
        self, pair_model, _tenant_lock, connection,
    ):
        pair_model.Status = ManualUploadPair.Status
        cursor = connection.cursor.return_value.__enter__.return_value
        pair = MagicMock(status=ManualUploadPair.Status.READY)
        pair_model.objects.filter.return_value.first.return_value = pair

        cursor.fetchone.side_effect = [(True,), (True,)]
        self.assertFalse(manual_uploads.run_post_upload_sql.__wrapped__(7, 1, 1000))
        pair.save.assert_not_called()
        self.assertEqual(len(cursor.execute.call_args_list), 3)

        cursor.reset_mock()
        cursor.fetchone.side_effect = [(True,), (False,), (True,)]
        self.assertTrue(manual_uploads.run_post_upload_sql.__wrapped__(7, 1, 1000))
        self.assertEqual(
            [call.args[0] for call in cursor.execute.call_args_list[-10:]],
            [
                "SELECT public.refresh_user_summary_rfm_metrics()",
                "SELECT public.refresh_buying_power()",
                "SELECT public.refresh_rfm_materialized_view()",
                "SELECT public.refresh_rfm_scores()",
                "SELECT public.refresh_product_co_purchase()",
                "SELECT public.update_user_summary_recommendations()",
                "SELECT public.update_user_top_products()",
                "SELECT public.process_campaign_eligibility()",
                "SELECT public.process_campaign_send_dates()",
                "SELECT public.fill_final_messages()",
            ],
        )
        self.assertEqual(pair.status, ManualUploadPair.Status.COMPLETE)

        cursor.reset_mock()
        cursor.fetchone.side_effect = [(True,)]
        pair_model.objects.filter.return_value.first.return_value = None
        self.assertFalse(manual_uploads.run_post_upload_sql.__wrapped__(7, 1, 1000))
        cursor.execute.assert_called_once()

    @patch.object(manual_uploads, "connection")
    @patch.object(manual_uploads, "lock_tenant")
    @patch.object(manual_uploads, "ManualUploadPair")
    def test_a_sql_error_stops_the_batch_and_leaves_it_ready_for_retry(
        self, pair_model, _tenant_lock, connection,
    ):
        pair_model.Status = ManualUploadPair.Status
        pair = MagicMock(status=ManualUploadPair.Status.READY)
        pair_model.objects.filter.return_value.first.return_value = pair
        cursor = connection.cursor.return_value.__enter__.return_value
        cursor.fetchone.side_effect = [(True,), (False,), (True,)]

        def execute(sql, params=None):
            if sql == "SELECT public.refresh_buying_power()":
                raise RuntimeError("test database failure")

        cursor.execute.side_effect = execute
        with self.assertRaisesRegex(RuntimeError, "test database failure"):
            manual_uploads.run_post_upload_sql.__wrapped__(7, 1, 1000)

        self.assertEqual(cursor.execute.call_args.args[0], "SELECT public.refresh_buying_power()")
        self.assertEqual(pair.status, ManualUploadPair.Status.READY)
        pair.save.assert_not_called()
