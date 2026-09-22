# core/services/sync_pipeline.py
"""
Core ingest logic for the automated ETL synchronization system.

This module is isolated from the manual Excel upload implementation. It
coerces incoming rows and appends them to the existing job-scoped staging
tables. It never reads, updates, or deletes permanent business rows. Global
IDs and permanent/normalized writes are performed only by the existing
allocate_upload_job_ids()/flush_*_upload_job() database functions when the
sync coordinator atomically finalizes the run.
"""

from dataclasses import dataclass, field
from typing import Any, Literal

from django.db import transaction

from core.models import (
    ProductsUnNormalizedDataStaging,
    UsersUnNormalizedDataStaging,
)
from core.sync.coercion import CoercionError, coerce_field
from core.sync.field_registry import get_field_specs

Entity = Literal["user", "product"]


@dataclass
class RowRejection:
    index: int
    internal_id: str | None
    field_name: str
    reason: str


@dataclass
class IngestResult:
    rows_received: int = 0
    rows_accepted: int = 0
    rows_rejected: int = 0
    rejections: list[RowRejection] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "rows_received": self.rows_received,
            "rows_accepted": self.rows_accepted,
            "rows_rejected": self.rows_rejected,
            "rejections": [
                {
                    "index": r.index,
                    "internal_id": r.internal_id,
                    "field": r.field_name,
                    "reason": r.reason,
                }
                for r in self.rejections
            ],
        }


def _coerce_row(
    entity: Entity, raw_row: dict, row_index: int, result: IngestResult
) -> dict | None:
    """
    Coerces every field in a single raw row per the field registry.
    Returns the cleaned dict, or None if the row must be rejected
    (a non-nullable field failed coercion or was missing entirely).
    """
    specs = get_field_specs(entity)
    cleaned: dict[str, Any] = {}
    internal_id_field = (
        "internal_user_id" if entity == "user" else "internal_product_id"
    )
    internal_id_value = raw_row.get(internal_id_field)

    for spec in specs:
        raw_value = raw_row.get(spec.field_name)
        try:
            cleaned[spec.field_name] = coerce_field(
                spec.field_name, raw_value, spec.coercion, spec.max_length
            )
        except CoercionError as exc:
            if spec.nullable_on_schema_miss:
                # first_product_attribute / second_product_attribute:
                # a bad or missing value degrades to NULL, row proceeds.
                cleaned[spec.field_name] = None
                continue
            # Any other field: reject the whole row, not the batch.
            result.rejections.append(
                RowRejection(
                    index=row_index,
                    internal_id=str(internal_id_value) if internal_id_value else None,
                    field_name=exc.field_name,
                    reason=exc.reason,
                )
            )
            return None

    return cleaned


# ─────────────────────────────────────────────────────────────────────────────
# USERS entity
# ─────────────────────────────────────────────────────────────────────────────


def ingest_user_rows(tenant, upload_job, raw_rows: list[dict]) -> IngestResult:
    result = IngestResult(rows_received=len(raw_rows))
    staging_objects: list[UsersUnNormalizedDataStaging] = []

    with transaction.atomic():
        for idx, raw_row in enumerate(raw_rows):
            cleaned = _coerce_row("user", raw_row, idx, result)
            if cleaned is None:
                result.rows_rejected += 1
                continue

            internal_user_id = cleaned["internal_user_id"]
            internal_order_id = cleaned["internal_order_id"]

            if not internal_user_id or not internal_order_id:
                result.rejections.append(
                    RowRejection(
                        index=idx,
                        internal_id=internal_user_id,
                        field_name="internal_user_id/internal_order_id",
                        reason="both internal_user_id and internal_order_id are required",
                    )
                )
                result.rows_rejected += 1
                continue

            staging_objects.append(
                UsersUnNormalizedDataStaging(
                    tenant=tenant,
                    upload_job=upload_job,
                    internal_user_id=internal_user_id,
                    user_id=None,
                    first_name=cleaned.get("first_name") or "",
                    last_name=cleaned.get("last_name"),
                    gender=cleaned.get("gender"),
                    phone_number=cleaned.get("phone_number"),
                    internal_order_id=internal_order_id,
                    order_id=None,
                    order_date=cleaned.get("order_date"),
                    internal_product_id=cleaned.get("internal_product_id") or "null",
                    product_id=None,
                    then_product_price=cleaned.get("then_product_price") or 0,
                    quantity=cleaned.get("quantity") or 0,
                    subtotal=None,
                    column_mapping={"source": "automated_sync"},
                )
            )
            result.rows_accepted += 1

        if staging_objects:
            UsersUnNormalizedDataStaging.objects.bulk_create(
                staging_objects, batch_size=1000
            )

    return result


# ─────────────────────────────────────────────────────────────────────────────
# PRODUCTS entity
# ─────────────────────────────────────────────────────────────────────────────


def ingest_product_rows(tenant, upload_job, raw_rows: list[dict]) -> IngestResult:
    result = IngestResult(rows_received=len(raw_rows))
    staging_objects: list[ProductsUnNormalizedDataStaging] = []

    with transaction.atomic():
        for idx, raw_row in enumerate(raw_rows):
            cleaned = _coerce_row("product", raw_row, idx, result)
            if cleaned is None:
                result.rows_rejected += 1
                continue

            internal_product_id = cleaned["internal_product_id"]
            if not internal_product_id:
                result.rejections.append(
                    RowRejection(
                        index=idx,
                        internal_id=None,
                        field_name="internal_product_id",
                        reason="internal_product_id is required",
                    )
                )
                result.rows_rejected += 1
                continue

            staging_objects.append(
                ProductsUnNormalizedDataStaging(
                    tenant=tenant,
                    upload_job=upload_job,
                    internal_product_id=internal_product_id,
                    product_id=None,
                    product_name=cleaned.get("product_name") or "",
                    product_category=cleaned.get("category") or "",
                    current_product_price=cleaned.get("current_product_price") or 0,
                    product_link=cleaned.get("product_link") or "",
                    first_product_attribute=cleaned.get("first_product_attribute"),
                    second_product_attribute=cleaned.get("second_product_attribute"),
                    column_mapping={"source": "automated_sync"},
                )
            )
            result.rows_accepted += 1

        if staging_objects:
            ProductsUnNormalizedDataStaging.objects.bulk_create(
                staging_objects, batch_size=1000
            )

    return result
