from celery import shared_task
from django.db import connection, transaction
from django.utils import timezone

from core.models import InstantCampaignPreparation


@shared_task
@transaction.atomic
def prepare_instant_campaign(campaign_id):
    with connection.cursor() as cursor:
        # Keep message preparation separate from the manual upload pipeline.
        cursor.execute("SELECT pg_try_advisory_xact_lock(873421, 1)")
        if not cursor.fetchone()[0]:
            return

        preparation = InstantCampaignPreparation.objects.select_for_update().filter(
            pk=campaign_id, completed_at__isnull=True,
        ).first()
        if preparation is None:
            return

        cursor.execute("SELECT public.process_instant_campaign_eligibility(%s)", [campaign_id])
        cursor.execute("SELECT public.fill_final_messages_for_instant_campaign(%s)", [campaign_id])

    preparation.completed_at = timezone.now()
    preparation.save(update_fields=["completed_at"])


@shared_task
def schedule_pending_instant_campaigns():
    campaign_ids = InstantCampaignPreparation.objects.filter(
        completed_at__isnull=True,
    ).order_by("campaign_id").values_list("campaign_id", flat=True)
    for campaign_id in campaign_ids.iterator():
        prepare_instant_campaign.delay(campaign_id)
