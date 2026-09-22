from unittest import TestCase
from unittest.mock import patch
import uuid

from core.models import Tenant, UploadJob
from core.services.sync_pipeline import ingest_product_rows, ingest_user_rows
from core.services.sync_runs import _payload_hash


class AppendOnlySyncPipelineTests(TestCase):
    @patch("core.services.sync_pipeline.transaction.atomic")
    @patch("core.services.sync_pipeline.UsersUnNormalizedDataStaging.objects.bulk_create")
    def test_user_rows_are_staged_for_the_run_job_with_unallocated_ids(
        self, bulk_create, atomic
    ):
        tenant = Tenant(id=7)
        job = UploadJob(
            id=uuid.uuid4(), tenant=tenant,
            upload_type=UploadJob.UploadType.CUSTOMERS,
            storage_key="sync://test/customers", mapping={},
        )
        row = {
            "internal_user_id": "U1",
            "first_name": "Ali",
            "last_name": "R",
            "gender": "m",
            "phone_number": "+989100000000",
            "internal_order_id": "O1",
            "order_date": "2026-01-01T00:00:00Z",
            "internal_product_id": "P1",
            "quantity": 2,
            "then_product_price": "100.00",
        }

        result = ingest_user_rows(tenant, job, [row])

        self.assertEqual(result.rows_accepted, 1)
        staged = bulk_create.call_args.args[0][0]
        self.assertIs(staged.upload_job, job)
        self.assertIsNone(staged.user_id)
        self.assertIsNone(staged.order_id)
        self.assertIsNone(staged.product_id)

    @patch("core.services.sync_pipeline.transaction.atomic")
    @patch("core.services.sync_pipeline.ProductsUnNormalizedDataStaging.objects.bulk_create")
    def test_product_rows_are_staged_for_the_run_job_with_unallocated_id(
        self, bulk_create, atomic
    ):
        tenant = Tenant(id=7)
        job = UploadJob(
            id=uuid.uuid4(), tenant=tenant,
            upload_type=UploadJob.UploadType.PRODUCTS,
            storage_key="sync://test/products", mapping={},
        )
        row = {
            "internal_product_id": "P1",
            "product_name": "Product",
            "category": "Category",
            "current_product_price": "100.00",
            "product_link": "https://example.test/p1",
            "first_product_attribute": "a",
            "second_product_attribute": "b",
        }

        result = ingest_product_rows(tenant, job, [row])

        self.assertEqual(result.rows_accepted, 1)
        staged = bulk_create.call_args.args[0][0]
        self.assertIs(staged.upload_job, job)
        self.assertIsNone(staged.product_id)


class SyncBatchHashTests(TestCase):
    def test_hash_is_stable_across_json_object_key_order(self):
        first = _payload_hash("user", 1, {"type": "integer", "value": "2"}, [{"b": 2, "a": 1}])
        second = _payload_hash("user", 1, {"value": "2", "type": "integer"}, [{"a": 1, "b": 2}])
        self.assertEqual(first, second)

    def test_hash_changes_when_batch_payload_changes(self):
        first = _payload_hash("product", 1, {"type": "integer", "value": "1"}, [{"id": "P1"}])
        second = _payload_hash("product", 1, {"type": "integer", "value": "2"}, [{"id": "P2"}])
        self.assertNotEqual(first, second)
