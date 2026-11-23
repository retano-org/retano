from datetime import datetime, time, timezone
from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase
from django.urls import reverse

from core.models import Campaign, InstantCampaign, enqueue_instant_campaign
from core.tasks import instant_campaigns


class InstantCampaignTests(SimpleTestCase):
    def test_fields_and_creation_time(self):
        removed_fields = {
            "campaign_start_date", "campaign_end_date", "send_sms_time",
            "activation_base", "comparison_type", "comparison_value", "value_unit",
        }
        expected = {
            field.name: field.deconstruct()[1:]
            for field in Campaign._meta.fields
            if field.name not in removed_fields
        }
        actual = {field.name: field.deconstruct()[1:] for field in InstantCampaign._meta.fields}
        self.assertEqual(actual, expected)
        self.assertEqual(reverse("instant-campaign-list"), "/api/v1/instant-campaigns/")

        campaign = InstantCampaign(
            created_at=datetime(2026, 11, 23, 20, 45, 16, 123456, tzinfo=timezone.utc)
        )
        self.assertEqual(campaign.send_sms_time, time(0, 15, 16, 123456))

    @patch.object(instant_campaigns.prepare_instant_campaign, "delay")
    @patch("core.models.transaction.on_commit")
    def test_only_creation_schedules_preparation_after_commit(self, on_commit, delay):
        campaign = InstantCampaign(pk=12)
        for created, raw in [(False, False), (True, True)]:
            enqueue_instant_campaign(InstantCampaign, campaign, created, raw, "default")
        on_commit.assert_not_called()
        enqueue_instant_campaign(InstantCampaign, campaign, True, False, "default")
        delay.assert_not_called()
        on_commit.call_args.args[0]()
        delay.assert_called_once_with(12)

    @patch.object(instant_campaigns, "connection")
    @patch.object(instant_campaigns, "InstantCampaignPreparation")
    def test_preparation_runs_in_order_and_skips_completed_work(self, model, connection):
        preparation = MagicMock()
        pending = model.objects.select_for_update.return_value.filter.return_value
        pending.first.return_value = preparation
        cursor = connection.cursor.return_value.__enter__.return_value
        cursor.fetchone.return_value = (True,)
        run = instant_campaigns.prepare_instant_campaign.run.__wrapped__

        run(12)

        self.assertEqual(
            [call.args for call in cursor.execute.call_args_list[1:]],
            [
                ("SELECT public.process_instant_campaign_eligibility(%s)", [12]),
                ("SELECT public.fill_final_messages_for_instant_campaign(%s)", [12]),
            ],
        )
        preparation.save.assert_called_once_with(update_fields=["completed_at"])

        cursor.reset_mock()
        pending.first.return_value = None
        run(12)
        cursor.execute.assert_called_once()
