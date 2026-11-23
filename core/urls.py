# core/urls.py  (DRF API v1 routes)

from django.urls import include, path

from rest_framework.routers import DefaultRouter

from .views import CampaignMetaView, CampaignViewSet, InstantCampaignViewSet

from core.views_reports import (
    SegmentsReportView,
    TrendsReportView,
    SalesRangeReportView,
    ActiveUsersReportView,
    RetentionReportView,
)

from core.views_campaign_stats import CampaignDetailStatsView
from core.views_uploads import (
    CustomerUploadView,
    ProductUploadView,
    CouponUploadView,
    SampleFilesView,
    UploadHistoryView,
    UploadJobDownloadView,
    UploadJobStatusView,
)
from core.views_dashboard import DashboardView

from core.views_sync_conf import (
      SyncConfigStatusView,
      SyncFieldMappingView,
      SyncApiKeyGenerateView,
  )
from core.views_sync import (
      SyncConfigFetchView,
      UserSyncIngestView,
      ProductSyncIngestView,
      SyncReportView,
)


# ─────────────────────────────────────────────────────────────────────────────
# DefaultRouter — registers ViewSets
# ─────────────────────────────────────────────────────────────────────────────

router = DefaultRouter()
router.register(r"campaigns", CampaignViewSet, basename="campaign")
router.register(r"instant-campaigns", InstantCampaignViewSet, basename="instant-campaign")

urlpatterns = [
    # ── Campaign meta (Phase 4) ───────────────────────────────────────────
    path("campaigns/meta/", CampaignMetaView.as_view(), name="campaign-meta"),
    path("campaigns/<int:pk>/stats/", CampaignDetailStatsView.as_view(), name="campaign-stats"),

    # Router-generated URLs
    path("", include(router.urls)),

    # ── Dashboard  ───────────────────────────────────────────────
    path("dashboard/", DashboardView.as_view(), name="api-dashboard"),

    # ── Reports  ─────────────────────────────────────────────────
    path("reports/trends/", TrendsReportView.as_view(), name="report-trends"),
    path("reports/segments/", SegmentsReportView.as_view(), name="reports-segments"),
    path(
        "reports/sales-ranges/",
        SalesRangeReportView.as_view(),
        name="reports-sales-ranges",
    ),
    path(
        "reports/active-users/",
        ActiveUsersReportView.as_view(),
        name="reports-active-users",
    ),
    path(
        "reports/retention/",
        RetentionReportView.as_view(),
        name="reports-retention",
    ),

    # ── File uploads (async — returns 202 + job_id) ────────────────────────
    path("uploads/customers/", CustomerUploadView.as_view(), name="upload-customers"),
    path("uploads/products/", ProductUploadView.as_view(), name="upload-products"),
    path("uploads/coupons/", CouponUploadView.as_view(), name="upload-coupons"),
    path("uploads/sample-files/", SampleFilesView.as_view(), name="upload-sample-files"),
    path("uploads/history/", UploadHistoryView.as_view(), name="upload-history"),

    path(
        "sync-conf/status/",
        SyncConfigStatusView.as_view(),
        name="sync-conf-status",
    ),
    path(
        "sync-conf/mapping/",
        SyncFieldMappingView.as_view(),
        name="sync-conf-mapping",
    ),
    path(
        "sync-conf/generate-key/",
        SyncApiKeyGenerateView.as_view(),
        name="sync-conf-generate-key",
    ),

    # ── ETL-facing sync API (Bearer API-key authenticated) ────────────────
    path("sync/config/", SyncConfigFetchView.as_view(), name="sync-config-fetch"),
    path("sync/data/users/", UserSyncIngestView.as_view(), name="sync-data-users"),
    path(
        "sync/data/products/",
        ProductSyncIngestView.as_view(),
        name="sync-data-products",
    ),
    path("sync/report/", SyncReportView.as_view(), name="sync-report"),


    # ── Upload job status polling (NEW) ─────────────────────────────────────
    path(
        "uploads/jobs/<uuid:job_id>/",
        UploadJobStatusView.as_view(),
        name="upload-job-status",
    ),
    path(
        "uploads/jobs/<uuid:job_id>/download/",
        UploadJobDownloadView.as_view(),
        name="upload-job-download",
    ),
]
