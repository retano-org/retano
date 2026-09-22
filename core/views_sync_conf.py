# core/views_sync_conf.py
"""
Backend for the "تنظیم API" page.

Two responsibilities:
    1. Read/write the tenant's field mapping (the 16+ rectangles in the UI).
    2. Generate the tenant's sync API key ("تولید API" button) — blocked
       until every mapping row is completely filled in.

Standard JWT auth, same as the rest of the human-facing API — this is NOT
the ETL-facing surface (see core/views_sync.py / core/sync/authentication.py
for that).
"""

from django.db import transaction
from django.utils import timezone
from rest_framework import permissions, status
from rest_framework.response import Response
from rest_framework.views import APIView

from core.schema import SYNC_CONFIG_STATUS_SCHEMA, SYNC_FIELD_MAPPING_GET_SCHEMA, SYNC_FIELD_MAPPING_PUT_SCHEMA, SYNC_API_KEY_GENERATE_SCHEMA
from core.models import SyncConfig, SyncFieldMapping
from core.serializers_sync import (
    SyncApiKeyGeneratedSerializer,
    SyncConfigMappingBulkSerializer,
    SyncConfigStatusSerializer,
    SyncFieldMappingReadSerializer,
)
from core.sync.field_registry import get_field_specs


def _tenant(request):
    return request.user.tenant


def _get_or_create_sync_config(tenant) -> SyncConfig:
    sync_config, _ = SyncConfig.objects.get_or_create(tenant=tenant)
    return sync_config


@SYNC_CONFIG_STATUS_SCHEMA 
class SyncConfigStatusView(APIView):
    """
    GET /api/v1/sync-conf/status/

    Top-of-page status: is sync enabled, key prefix (masked), last run
    outcome. Does not include the mapping rows themselves — see
    SyncFieldMappingView for those.
    """

    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        sync_config = _get_or_create_sync_config(_tenant(request))
        serializer = SyncConfigStatusSerializer(sync_config)
        return Response(serializer.data, status=status.HTTP_200_OK)


class SyncFieldMappingView(APIView):
    """
    GET /api/v1/sync-conf/mapping/
        Returns every mapping row for both entities (including empty ones,
        so the frontend can render every rectangle — filled or not — on
        page load).

    PUT /api/v1/sync-conf/mapping/
        Replaces the tenant's entire mapping atomically. See
        SyncConfigMappingBulkSerializer for the required body shape:
        every field for both entities must be present in a single call.
    """

    permission_classes = [permissions.IsAuthenticated]


    @SYNC_FIELD_MAPPING_GET_SCHEMA
    def get(self, request):
        tenant = _tenant(request)
        sync_config = _get_or_create_sync_config(tenant)
        existing = {
            (m.entity, m.field_name): m
            for m in SyncFieldMapping.objects.filter(tenant=tenant)
        }

        rows = []
        for entity in ("user", "product"):
            for spec in get_field_specs(entity):
                mapping = existing.get((entity, spec.field_name))
                if mapping is None:
                    # Not yet saved — represent as an empty row so the
                    # frontend can still render the rectangle.
                    rows.append(
                        {
                            "entity": entity,
                            "field_name": spec.field_name,
                            "client_table": "",
                            "client_column": "",
                            "is_filled": False,
                        }
                    )
                else:
                    rows.append(SyncFieldMappingReadSerializer(mapping).data)

        return Response(
            {
                "mappings": rows,
                "user_cursor_column": sync_config.user_cursor_column,
                "product_cursor_column": sync_config.product_cursor_column,
            },
            status=status.HTTP_200_OK,
        )
    

    @SYNC_FIELD_MAPPING_PUT_SCHEMA
    def put(self, request):
        tenant = _tenant(request)
        serializer = SyncConfigMappingBulkSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        with transaction.atomic():
            sync_config = _get_or_create_sync_config(tenant)
            existing_tables = {
                entity: set(
                    SyncFieldMapping.objects.filter(
                        tenant=tenant, entity=entity
                    ).exclude(client_table="").values_list("client_table", flat=True)
                )
                for entity in ("user", "product")
            }
            incoming_tables = {
                entity: {
                    item.get("client_table", "").strip()
                    for item in serializer.validated_data["mappings"]
                    if item["entity"] == entity and item.get("client_table", "").strip()
                }
                for entity in ("user", "product")
            }
            for entity in ("user", "product"):
                checkpoint = getattr(sync_config, f"{entity}_cursor_value")
                if (
                    checkpoint is not None
                    and existing_tables[entity]
                    and incoming_tables[entity] != existing_tables[entity]
                ):
                    return Response(
                        {
                            "status": "error",
                            "error_type": "source_table_change_requires_reset",
                            "message": (
                                f"Cannot change the {entity} source table after "
                                "synchronization has started without an explicit "
                                "checkpoint reset."
                            ),
                        },
                        status=status.HTTP_409_CONFLICT,
                    )
            for entity in ("user", "product"):
                field = f"{entity}_cursor_column"
                if field not in serializer.validated_data:
                    continue
                new_column = serializer.validated_data[field].strip()
                old_column = getattr(sync_config, field)
                checkpoint = getattr(sync_config, f"{entity}_cursor_value")
                if checkpoint is not None and old_column != new_column:
                    return Response(
                        {
                            "status": "error",
                            "error_type": "cursor_change_requires_reset",
                            "message": (
                                f"Cannot change {field} after synchronization has "
                                "started without an explicit checkpoint reset."
                            ),
                        },
                        status=status.HTTP_409_CONFLICT,
                    )
                setattr(sync_config, field, new_column)

            for item in serializer.validated_data["mappings"]:
                SyncFieldMapping.objects.update_or_create(
                    tenant=tenant,
                    entity=item["entity"],
                    field_name=item["field_name"],
                    defaults={
                        "client_table": item.get("client_table", "").strip(),
                        "client_column": item.get("client_column", "").strip(),
                    },
                )
            sync_config.save(update_fields=[
                "user_cursor_column", "product_cursor_column", "updated_at"
            ])

        return Response(
            {"message": "نگاشت ستون‌ها با موفقیت ذخیره شد."},
            status=status.HTTP_200_OK,
        )

@SYNC_API_KEY_GENERATE_SCHEMA 
class SyncApiKeyGenerateView(APIView):
    """
    POST /api/v1/sync-conf/generate-key/  ("تولید API" button)

    Blocked (400) unless EVERY mapping row for BOTH entities has both
    client_table and client_column non-blank. Per explicit product
    decision, this includes first_product_attribute/second_product_attribute
    — those two are only forgiving at RUNTIME if the configured
    table/column turns out not to exist in the tenant's actual database;
    at configuration time, the tenant must still declare something for
    every field, same as any other.

    On success:
        - Generates a new API key (rotating any previous one).
        - Enables the SyncConfig (is_enabled = True) so the ETL's key
          lookup (SyncConfig.resolve_from_raw_key) will accept it.
        - Returns the raw key ONCE. The frontend must copy/display it
          immediately (matches the "دریافت API" copy button in the UI) —
          it is never retrievable again.
    """

    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        tenant = _tenant(request)

        mappings = list(SyncFieldMapping.objects.filter(tenant=tenant))
        by_key = {(m.entity, m.field_name): m for m in mappings}

        incomplete = []
        for entity in ("user", "product"):
            for spec in get_field_specs(entity):
                mapping = by_key.get((entity, spec.field_name))
                if mapping is None or not mapping.is_filled:
                    incomplete.append(f"{entity}.{spec.field_name}")

        if incomplete:
            return Response(
                {
                    "status": "error",
                    "error_type": "mapping_incomplete",
                    "message": (
                        "لطفاً همه فیلدهای نگاشت ستون را قبل از تولید API تکمیل کنید."
                    ),
                    "missing_fields": incomplete,
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        sync_config = _get_or_create_sync_config(tenant)
        missing_cursors = [
            name for name in ("user_cursor_column", "product_cursor_column")
            if not getattr(sync_config, name).strip()
        ]
        if missing_cursors:
            return Response(
                {
                    "status": "error",
                    "error_type": "cursor_incomplete",
                    "message": "Configure an immutable incremental cursor column for both entities.",
                    "missing_fields": missing_cursors,
                },
                status=status.HTTP_400_BAD_REQUEST,
            )
        raw_key = sync_config.generate_new_api_key()
        sync_config.is_enabled = True
        sync_config.save(update_fields=["is_enabled"])

        payload = SyncApiKeyGeneratedSerializer(
            {
                "api_key": raw_key,
                "api_key_prefix": sync_config.api_key_prefix,
                "generated_at": sync_config.api_key_generated_at,
            }
        ).data
        return Response(payload, status=status.HTTP_201_CREATED)
