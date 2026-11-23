# core/tasks/campaign_sms.py
"""
Campaign SMS send + delivery-check pipeline.

Two Celery beat tasks:

    send_due_campaign_sms()
        Finds trigger_results rows that are ready to send (final_message
        already populated by the existing pg_cron job running
        fill_final_messages(), send_sms_date already populated by the
        existing pg_cron job running process_campaign_send_dates(), and
        sent_at still NULL), sends them via sms.ir's send_like_to_like,
        and stamps sent_at + sms_message_id.

    check_campaign_sms_delivery()
        Finds trigger_results rows that have been sent but not yet
        confirmed delivered, looks up each one's delivery status via
        sms.ir's report_message, and stamps delivered_at on confirmed
        deliveries.

Both tasks are idempotent-safe to re-run: send only picks up rows with
sent_at IS NULL, so a message is never sent twice by this task alone
(a genuine double-send could still happen if the task crashes AFTER
sms.ir accepts the batch but BEFORE the DB write commits — see the
per-row try/except below, which stamps sent_at defensively as soon as
each individual send is confirmed rather than batching all DB writes
until the very end).

Neither task touches fill_final_messages() or process_campaign_send_dates()
— those remain pg_cron jobs in Supabase, untouched by this phase.
"""

import logging
from datetime import timedelta
from itertools import chain

from celery import shared_task
from django.db import connection
from django.utils import timezone

from core.models import SentCampaignMessage
from core.models_supabase import TriggerResult
from core.services.sms_campaign_client import (
    CampaignSmsError,
    get_default_campaign_client,
)

logger = logging.getLogger("retano.tasks.campaign_sms")

# Batches of this size per sms.ir send_like_to_like call. sms.ir (like most
# SMS gateways) has a practical per-request limit; 200 is a conservative
# default that keeps individual requests fast and failures cheap to retry.
# Adjust once real-world batch sizes are observed in production.
SEND_BATCH_SIZE = 200

# How far back to still bother checking delivery. sms.ir delivery reports
# don't arrive instantly, but if a message was sent more than this long ago
# and still isn't confirmed delivered, further polling has very low value —
# stop checking so the pending-delivery query doesn't grow unbounded forever.
DELIVERY_CHECK_MAX_AGE = timedelta(days=2)


# ─────────────────────────────────────────────────────────────────────────────
# Send
# ─────────────────────────────────────────────────────────────────────────────

@shared_task(bind=True, name="core.tasks.campaign_sms.send_due_campaign_sms")
def send_due_campaign_sms(self):
    """
    Celery beat task — runs on the interval configured in
    CELERY_BEAT_SCHEDULE (see the settings guidance provided separately).

    Picks up trigger_results rows where:
        - final_message IS NOT NULL   (fill_final_messages() already ran)
        - send_sms_date <= today       (process_campaign_send_dates() already ran)
        - sent_at IS NULL               (not sent yet)
        - phone_number IS NOT NULL      (nothing to send to otherwise)

    Groups by tenant so each tenant's sends can (in principle) use a
    different line number in the future; for now all tenants share the
    single configured SMSIR_CAMPAIGN_LINE_NUMBER, but grouping by tenant
    keeps this task forward-compatible with per-tenant lines without a
    structural change later.
    """
    today = timezone.localdate()

    due_qs = (
        TriggerResult.objects.filter(
            final_message__isnull=False,
            send_sms_date__lte=today,
            sent_at__isnull=True,
            phone_number__isnull=False,
        )
        .exclude(phone_number="")
        .order_by("tenant_id", "id")
    )

    tenant_ids = due_qs.values_list("tenant_id", flat=True).distinct()

    total_sent = 0
    total_failed = 0

    for tenant_id in tenant_ids:
        rows = list(due_qs.filter(tenant_id=tenant_id))
        for batch_start in range(0, len(rows), SEND_BATCH_SIZE):
            batch = rows[batch_start : batch_start + SEND_BATCH_SIZE]
            sent, failed = _send_batch(batch)
            total_sent += sent
            total_failed += failed

    logger.info(
        "send_due_campaign_sms: sent=%s failed=%s", total_sent, total_failed
    )
    return {"sent": total_sent, "failed": total_failed}


def _send_batch(batch: list[TriggerResult]) -> tuple[int, int]:
    """
    Sends one batch via sms.ir send_like_to_like, then writes back
    sent_at/sms_message_id per row that actually succeeded. Returns
    (sent_count, failed_count).
    """
    numbers = [row.phone_number for row in batch]
    messages = [row.final_message for row in batch]

    try:
        client = get_default_campaign_client()
        results = client.send_like_to_like(numbers, messages)
    except CampaignSmsError:
        logger.exception(
            "send_due_campaign_sms: batch of %s rows failed entirely", len(batch)
        )
        # Nothing in this batch is marked sent — the next beat tick will
        # retry all of them, since sent_at is still NULL for every row.
        return 0, len(batch)

    now = timezone.now()
    sent_count = 0
    failed_count = 0

    for row, result in zip(batch, results):
        if not result.succeeded:
            failed_count += 1
            logger.warning(
                "send_due_campaign_sms: row id=%s to %s not accepted by sms.ir",
                row.id,
                row.phone_number,
            )
            continue
        TriggerResult.objects.filter(pk=row.pk, sent_at__isnull=True).update(
            sent_at=now, sms_message_id=result.message_id
        )
        sent_count += 1

    return sent_count, failed_count


# ─────────────────────────────────────────────────────────────────────────────
# Delivery check
# ─────────────────────────────────────────────────────────────────────────────

@shared_task(bind=True, name="core.tasks.campaign_sms.check_campaign_sms_delivery")
def check_campaign_sms_delivery(self):
    """
    Celery beat task — polls sms.ir for delivery confirmation on messages
    that have been sent but not yet confirmed delivered.

    Only considers rows sent within DELIVERY_CHECK_MAX_AGE, to avoid this
    query (and the resulting API calls) growing unbounded for messages
    that will simply never confirm as delivered.
    """
    cutoff = timezone.now() - DELIVERY_CHECK_MAX_AGE

    pending_qs = TriggerResult.objects.filter(
        sent_at__isnull=False,
        sent_at__gte=cutoff,
        delivered_at__isnull=True,
        sms_message_id__isnull=False,
    ).exclude(sms_message_id="")
    archived_qs = SentCampaignMessage.objects.filter(
        sent_at__gte=cutoff,
        delivered_at__isnull=True,
        sms_message_id__isnull=False,
    ).exclude(sms_message_id="")

    client = get_default_campaign_client()

    total_checked = 0
    total_delivered = 0

    for row in chain(pending_qs.iterator(), archived_qs.iterator()):
        total_checked += 1
        try:
            report = client.report_message(row.sms_message_id)
        except CampaignSmsError:
            logger.exception(
                "check_campaign_sms_delivery: report lookup failed for row id=%s "
                "(message_id=%s)",
                row.id,
                row.sms_message_id,
            )
            continue

        if report.delivered:
            now = timezone.now()
            if isinstance(row, SentCampaignMessage):
                trigger_result_id = row.trigger_result_id
            else:
                trigger_result_id = row.pk
                updated = TriggerResult.objects.filter(
                    pk=row.pk, sent_at=row.sent_at, sms_message_id=row.sms_message_id,
                    delivered_at__isnull=True,
                ).update(delivered_at=now)
                if updated:
                    total_delivered += updated
                    continue
            # An eligibility run may have moved the send since the lookup began.
            total_delivered += SentCampaignMessage.objects.filter(
                trigger_result_id=trigger_result_id, sent_at=row.sent_at,
                delivered_at__isnull=True,
            ).update(delivered_at=now)

    logger.info(
        "check_campaign_sms_delivery: checked=%s newly_delivered=%s",
        total_checked,
        total_delivered,
    )
    return {"checked": total_checked, "newly_delivered": total_delivered}
