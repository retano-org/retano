# core/serializers_sync.py
"""
Serializers for:
    1. The "تنظیم API" page backend (SyncFieldMapping CRUD + تولید API)
    2. The ETL-facing sync endpoints (config fetch, data ingest, reporting)
"""

from rest_framework import serializers

from core.models import SyncConfig, SyncFieldMapping, SyncRun
from core.sync.field_registry import get_field_specs


# ─────────────────────────────────────────────────────────────────────────────
# API-Conf page (human-facing, JWT-authenticated)
# ─────────────────────────────────────────────────────────────────────────────


class SyncFieldMappingSerializer(serializers.ModelSerializer):
    class Meta:
        model = SyncFieldMapping
        fields = ["entity", "field_name", "client_table", "client_column"]

    def validate(self, attrs):
        entity = attrs.get("entity")
        field_name = attrs.get("field_name")
        valid_names = {spec.field_name for spec in get_field_specs(entity)} if entity else set()
        if entity and field_name not in valid_names:
            raise serializers.ValidationError(
                {"field_name": f"'{field_name}' is not a valid field for entity '{entity}'."}
            )
        return attrs


class SyncConfigMappingBulkSerializer(serializers.Serializer):
    """
    Body for PUT /api/v1/sync-conf/mapping/

    {
        "mappings": [
            {"entity": "user", "field_name": "first_name",
             "client_table": "customers", "client_column": "First_Name"},
            ...
        ]
    }

    All fields for BOTH entities must be present in a single call — this
    endpoint replaces the tenant's entire mapping atomically so the UI's
    "save everything on the page at once" interaction maps directly onto
    one request. Partial saves (e.g. only the user rows) are rejected,
    since تولید API needs to validate completeness across both entities
    together and a partial save would leave the mapping in a state that
    is ambiguous to reason about between requests.
    """

    mappings = SyncFieldMappingSerializer(many=True)
    user_cursor_column = serializers.CharField(
        max_length=255, required=False, allow_blank=False, trim_whitespace=True
    )
    product_cursor_column = serializers.CharField(
        max_length=255, required=False, allow_blank=False, trim_whitespace=True
    )

    def validate_mappings(self, value):
        if not value:
            raise serializers.ValidationError("mappings cannot be empty.")

        seen = set()
        for item in value:
            key = (item["entity"], item["field_name"])
            if key in seen:
                raise serializers.ValidationError(
                    f"Duplicate mapping entry for {key[0]}.{key[1]}."
                )
            seen.add(key)

        # Ensure every registry field for both entities is represented —
        # even if client_table/client_column are blank, the row must exist
        # so تولید API has a single flat list to check for completeness.
        for entity in ("user", "product"):
            required = {spec.field_name for spec in get_field_specs(entity)}
            provided = {k[1] for k in seen if k[0] == entity}
            missing = required - provided
            if missing:
                raise serializers.ValidationError(
                    f"Missing mapping rows for {entity} fields: {sorted(missing)}"
                )
        return value


class SyncFieldMappingReadSerializer(serializers.ModelSerializer):
    is_filled = serializers.BooleanField(read_only=True)

    class Meta:
        model = SyncFieldMapping
        fields = ["entity", "field_name", "client_table", "client_column", "is_filled"]


class SyncConfigStatusSerializer(serializers.ModelSerializer):
    """Read-only status shown at the top of the تنظیم API page."""

    latest_run_status = serializers.SerializerMethodField()
    latest_run_message = serializers.SerializerMethodField()

    class Meta:
        model = SyncConfig
        fields = [
            "is_enabled",
            "api_key_prefix",
            "api_key_generated_at",
            "batch_size",
            "user_cursor_column",
            "product_cursor_column",
            "user_cursor_value",
            "product_cursor_value",
            "latest_run_status",
            "latest_run_message",
        ]

    def get_latest_run_status(self, obj) -> str | None:
        run = obj.tenant.sync_runs.first()
        return run.status if run else None

    def get_latest_run_message(self, obj) -> str | None:
        run = obj.tenant.sync_runs.first()
        return run.user_facing_message if run else None


class SyncApiKeyGeneratedSerializer(serializers.Serializer):
    """
    Response body for POST /api/v1/sync-conf/generate-key/

    api_key is returned EXACTLY ONCE — the raw value is never retrievable
    again after this response. The frontend must show/copy it immediately
    (matches the second screenshot's "دریافت API" / copy-to-clipboard UI).
    """

    api_key = serializers.CharField()
    api_key_prefix = serializers.CharField()
    generated_at = serializers.DateTimeField()


# ─────────────────────────────────────────────────────────────────────────────
# ETL-facing (Bearer API-key authenticated)
# ─────────────────────────────────────────────────────────────────────────────


class SyncConfigFetchSerializer(serializers.Serializer):
    """
    Response for GET /api/v1/sync/config/

    Shape the ETL consumes to build its queries:
        {
          "batch_size": 1000,
          "mapping": {
            "user": {
              "internal_user_id":   {"table": "customers", "column": "cust_id"},
              "first_name":         {"table": "customers", "column": "First_Name"},
              ...
            },
            "product": {
              "internal_product_id": {"table": "products", "column": "id"},
              ...
            }
          },
          "nullable_fields": {
            "product": ["first_product_attribute", "second_product_attribute"]
          }
        }

    nullable_fields tells the ETL which fields it is allowed to treat as
    "column not found → send null for this field, keep going" during its
    own pre-flight schema check, instead of aborting the whole run.
    """

    batch_size = serializers.IntegerField()
    mapping = serializers.DictField()
    nullable_fields = serializers.DictField()
    cursors = serializers.DictField()
    lease_expires_at = serializers.DateTimeField()


class SyncDataRowsSerializer(serializers.Serializer):
    """
    Body for POST /api/v1/sync/data/users/  and  .../data/products/

    {
        "rows": [ {"internal_user_id": "123", "first_name": "Ali", ...}, ... ]
    }

    Deliberately NOT a ListSerializer of per-field-typed serializers: the
    whole point of core/sync/coercion.py is that raw values arrive
    untyped/loosely-typed from heterogeneous client DB engines, and we
    want our own explicit, per-field coercion with per-row rejection
    rather than DRF's default all-or-nothing field validation, which
    would reject an entire row (or worse, error confusingly) on the
    first bad field rather than reporting all issues per field/row and
    proceeding with everything else.
    """

    rows = serializers.ListField(
        child=serializers.DictField(), allow_empty=True, max_length=20000
    )
    batch_number = serializers.IntegerField(min_value=1)
    cursor_after = serializers.JSONField(required=False, allow_null=True)


class SyncReportSerializer(serializers.Serializer):
    """
    Body for POST /api/v1/sync/report/

    Two shapes depending on what the ETL is reporting:

    Pre-flight schema failure (ETL never called the data endpoints at all):
        {
            "status": "failed",
            "failure_stage": "schema_table" | "schema_column",
            "failure_detail": "table 'customres' not found"
        }

    Final run outcome (after calling the data endpoints, success or
    partial success — rows_* figures already known to the ETL from the
    data endpoints' own responses, sent back here purely for the audit
    log / تنظیم API status display):
        {
            "status": "success" | "partial",
            "users_rows_received": 5000, "users_rows_accepted": 4998, "users_rows_rejected": 2,
            "products_rows_received": 300, "products_rows_accepted": 300, "products_rows_rejected": 0
        }
    """

    status = serializers.ChoiceField(choices=["success", "partial", "failed"])
    failure_stage = serializers.ChoiceField(
        choices=[c[0] for c in SyncRun.FAILURE_STAGE_CHOICES],
        required=False,
        allow_null=True,
    )
    failure_detail = serializers.CharField(required=False, allow_blank=True, default="")

    users_rows_received = serializers.IntegerField(required=False, default=0)
    users_rows_accepted = serializers.IntegerField(required=False, default=0)
    users_rows_rejected = serializers.IntegerField(required=False, default=0)

    products_rows_received = serializers.IntegerField(required=False, default=0)
    products_rows_accepted = serializers.IntegerField(required=False, default=0)
    products_rows_rejected = serializers.IntegerField(required=False, default=0)

    def validate(self, attrs):
        if attrs["status"] == "failed" and not attrs.get("failure_stage"):
            raise serializers.ValidationError(
                {"failure_stage": "Required when status is 'failed'."}
            )
        return attrs
