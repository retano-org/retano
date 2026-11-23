# core/views.py

from django_filters.rest_framework import DjangoFilterBackend
from drf_spectacular.utils import extend_schema

from rest_framework import filters, permissions, status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.views import APIView
from core.schema import CAMPAIGN_VIEWSET_SCHEMA, CAMPAIGN_TOGGLE_SCHEMA, CAMPAIGN_META_SCHEMA
from .models import (
    Campaign,
    InstantCampaign,
)


from .serializers import (
    CampaignListSerializer,
    CampaignSerializer,
    CampaignToggleSerializer,
    InstantCampaignListSerializer,
    InstantCampaignSerializer,
    InstantCampaignToggleSerializer,
)





# ─────────────────────────────────────────────────────────────────────────────
# Campaigns
# ─────────────────────────────────────────────────────────────────────────────



class CampaignViewSetBase(viewsets.ModelViewSet):
    """
    /api/v1/campaigns/                 — list, create
    /api/v1/campaigns/{id}/            — retrieve, update, partial_update, destroy
    /api/v1/campaigns/{id}/toggle/     — PATCH, flips/sets is_active

    Tenant isolation: every queryset is scoped to the requesting user's
    own tenant. A campaign belonging to another tenant is invisible —
    not "403 Forbidden", just a 404, since DRF's get_object() raises
    Http404 when the filtered queryset doesn't contain the pk.
    """

    permission_classes = [permissions.IsAuthenticated]
    http_method_names = ["get", "post", "put", "patch", "head", "options"]
    
    filter_backends = [
        DjangoFilterBackend,
        filters.SearchFilter,
        filters.OrderingFilter,
    ]
    filterset_fields = ["is_active"]
    search_fields = ["name"]
    ordering_fields = ["created_at", "name", "rule_number"]
    ordering = ["-created_at"]

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            return self.campaign_model.objects.none()
        return self.campaign_model.objects.filter(tenant__owner=self.request.user)

    def get_serializer_class(self):
        if self.action == "list":
            return self.list_serializer_class
        if self.action == "toggle":
            return self.toggle_serializer_class
        return self.serializer_class

    def perform_create(self, serializer):
        # tenant is never trusted from the client — derived from the
        # authenticated user's own Tenant (created via signal at registration).
        serializer.save(tenant=self.request.user.tenant)


    @CAMPAIGN_TOGGLE_SCHEMA    
    @action(detail=True, methods=["patch"])
    def toggle(self, request, pk=None):
        """
        PATCH /api/v1/campaigns/{id}/toggle/

        Body {} or omitted        → flips is_active.
        Body {"is_active": true}  → sets it explicitly.
        """
        campaign = self.get_object()

        if "is_active" in request.data:
            serializer = self.get_serializer(
                campaign, data=request.data, partial=True
            )
            serializer.is_valid(raise_exception=True)
            serializer.save()
        else:
            campaign.is_active = not campaign.is_active
            campaign.save(update_fields=["is_active"])
            serializer = self.get_serializer(campaign)

        return Response(serializer.data, status=status.HTTP_200_OK)


@CAMPAIGN_VIEWSET_SCHEMA
class CampaignViewSet(CampaignViewSetBase):
    campaign_model = Campaign
    serializer_class = CampaignSerializer
    list_serializer_class = CampaignListSerializer
    toggle_serializer_class = CampaignToggleSerializer


@extend_schema(tags=["Instant campaigns"])
class InstantCampaignViewSet(CampaignViewSetBase):
    campaign_model = InstantCampaign
    serializer_class = InstantCampaignSerializer
    list_serializer_class = InstantCampaignListSerializer
    toggle_serializer_class = InstantCampaignToggleSerializer




@CAMPAIGN_META_SCHEMA
class CampaignMetaView(APIView):
    """
    GET /api/v1/campaigns/meta/

    Returns every choice-field's available options so the frontend can
    build selects/dropdowns without hardcoding Persian labels.

    Shape:
        {
            "activation_base": [{"value": "...", "label": "..."}, ...],
            "comparison_type": [...],
            ...
        }
    """

    permission_classes = [permissions.IsAuthenticated]

    #: Campaign fields whose `choices` should be exposed. Listed explicitly
    #: rather than introspected so the response shape is stable even if
    #: unrelated choice fields get added to the model later.
    CHOICE_FIELDS = [
        "coupon_discount_percentage",
        "activation_base",
        "comparison_type",
        "value_unit",
        "gender",
        "buying_power",
        "priority",
        "customer_type",
    ]

    def get(self, request):
        data = {}
        for field_name in self.CHOICE_FIELDS:
            field = Campaign._meta.get_field(field_name)
            choices = field.choices or []
            data[field_name] = [
                {"value": value, "label": label} for value, label in choices
            ]
        return Response(data, status=status.HTTP_200_OK)



