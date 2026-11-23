ALTER TABLE public.core_instantcampaign ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.core_instantcampaignpreparation ENABLE ROW LEVEL SECURITY;

ALTER TABLE public.trigger_results
    ALTER COLUMN rule_id DROP NOT NULL,
    ADD COLUMN instant_campaign_id bigint REFERENCES public.core_instantcampaign(id),
    ADD CONSTRAINT trigger_results_one_campaign
        CHECK ((rule_id IS NOT NULL) <> (instant_campaign_id IS NOT NULL));

CREATE INDEX trigger_results_instant_campaign_idx
    ON public.trigger_results (instant_campaign_id)
    WHERE instant_campaign_id IS NOT NULL;

CREATE FUNCTION public.retano_enqueue_instant_campaign()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, public
AS $$
BEGIN
    INSERT INTO public.core_instantcampaignpreparation (campaign_id) VALUES (NEW.id);
    RETURN NEW;
END;
$$;

CREATE TRIGGER retano_enqueue_instant_campaign
AFTER INSERT ON public.core_instantcampaign
FOR EACH ROW EXECUTE FUNCTION public.retano_enqueue_instant_campaign();

CREATE OR REPLACE FUNCTION public.retano_archive_campaign_send()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
BEGIN
    IF OLD.sent_at IS NOT NULL AND (
        TG_OP = 'DELETE'
        OR NEW.sent_at IS DISTINCT FROM OLD.sent_at
        OR NEW.sms_message_id IS DISTINCT FROM OLD.sms_message_id
    ) THEN
        INSERT INTO public.core_sentcampaignmessage (
            trigger_result_id, tenant_id, user_id, rule_id, instant_campaign_id,
            sent_at, sms_message_id, delivered_at, snapshot
        ) VALUES (
            OLD.id, OLD.tenant_id, OLD.user_id, OLD.rule_id, OLD.instant_campaign_id,
            OLD.sent_at, OLD.sms_message_id, OLD.delivered_at, to_jsonb(OLD)
        )
        ON CONFLICT (trigger_result_id, sent_at) DO NOTHING;

        IF TG_OP = 'UPDATE' AND NEW.sent_at IS NULL THEN
            NEW.sms_message_id := NULL;
            NEW.delivered_at := NULL;
        END IF;
    END IF;

    IF TG_OP = 'DELETE' THEN
        RETURN OLD;
    END IF;
    RETURN NEW;
END;
$$;

CREATE FUNCTION public.retano_campaign_priority(priority text)
RETURNS integer
LANGUAGE sql IMMUTABLE PARALLEL SAFE
AS $$
    SELECT CASE priority
        WHEN 'خیلی بالا' THEN 1
        WHEN 'بالا' THEN 2
        WHEN 'متوسط' THEN 3
        WHEN 'پایین' THEN 4
        WHEN 'خیلی پایین' THEN 5
        ELSE 6
    END;
$$;

CREATE FUNCTION public.retano_instant_campaign_matches(
    campaign public.core_instantcampaign, customer public.user_summary
)
RETURNS boolean
LANGUAGE sql STABLE
SET search_path = pg_catalog, public
AS $$
    SELECT
        CASE campaign.gender
            WHEN 'آقایان' THEN customer.gender = 'male'
            WHEN 'بانوان' THEN customer.gender = 'female'
            ELSE TRUE
        END
        AND CASE campaign.customer_type
            WHEN 'ویژه'         THEN customer.rfm_segment = 'vip'
            WHEN 'فعال'         THEN customer.rfm_segment = 'active'
            WHEN 'تازه وارد'    THEN customer.rfm_segment = 'new'
            WHEN 'در خطر ریزش' THEN customer.rfm_segment = 'at_risk'
            WHEN 'از دست رفته' THEN customer.rfm_segment = 'churned'
            ELSE TRUE
        END
        AND CASE campaign.buying_power
            WHEN 'خیلی بالا'  THEN customer.buying_power = 'Very High'
            WHEN 'بالا'       THEN customer.buying_power = 'High'
            WHEN 'متوسط'      THEN customer.buying_power = 'Medium'
            WHEN 'پایین'      THEN customer.buying_power = 'Low'
            WHEN 'خیلی پایین' THEN customer.buying_power = 'Very Low'
            ELSE TRUE
        END
        AND (
            campaign.first_product_attribute IN ('all', 'همه')
            OR EXISTS (
                SELECT 1 FROM public.user_attribute_scores a
                WHERE a.user_id = customer.user_id AND a.tenant_id = campaign.tenant_id
                  AND a.attribute_value = campaign.first_product_attribute
            )
        )
        AND (
            campaign.second_product_attribute IN ('all', 'همه')
            OR EXISTS (
                SELECT 1 FROM public.user_attribute_scores a
                WHERE a.user_id = customer.user_id AND a.tenant_id = campaign.tenant_id
                  AND a.attribute_value = campaign.second_product_attribute
            )
        );
$$;

CREATE FUNCTION public.process_instant_campaign_eligibility(p_campaign_id bigint)
RETURNS void
LANGUAGE plpgsql
SET search_path = pg_catalog, public
AS $$
DECLARE
    campaign public.core_instantcampaign%ROWTYPE;
BEGIN
    PERFORM pg_advisory_xact_lock(873421, 2);

    SELECT c.* INTO campaign
    FROM public.core_instantcampaign c
    WHERE c.id = p_campaign_id AND c.is_active;
    IF NOT FOUND THEN
        RETURN;
    END IF;

    CREATE TEMP TABLE instant_campaign_candidates ON COMMIT DROP AS
    SELECT u.user_id
    FROM public.users u
    JOIN public.user_summary us ON us.user_id = u.user_id AND us.tenant_id = u.tenant_id
    WHERE u.tenant_id = campaign.tenant_id
      AND public.retano_instant_campaign_matches(campaign, us)
      AND NOT EXISTS (
          SELECT 1
          FROM public.core_instantcampaign competing
          WHERE competing.tenant_id = campaign.tenant_id
            AND competing.id <> campaign.id
            AND competing.is_active
            AND public.retano_campaign_priority(competing.priority)
                < public.retano_campaign_priority(campaign.priority)
            AND public.retano_instant_campaign_matches(competing, us)
      );

    -- A replacement gets a fresh trigger_results row and clean delivery state.
    -- The archive trigger retains any send associated with the old row.
    DELETE FROM public.trigger_results tr
    USING pg_temp.instant_campaign_candidates eligible
    WHERE tr.tenant_id = campaign.tenant_id AND tr.user_id = eligible.user_id
      AND (
          tr.instant_campaign_id IS NULL
          OR (
              tr.instant_campaign_id <> campaign.id
              AND (
                  tr.sent_at IS NOT NULL
                  OR public.retano_campaign_priority(campaign.priority)
                     < public.retano_campaign_priority((
                         SELECT current_campaign.priority
                         FROM public.core_instantcampaign current_campaign
                         WHERE current_campaign.id = tr.instant_campaign_id
                     ))
              )
          )
      );

    INSERT INTO public.trigger_results (
        tenant_id, user_id, instant_campaign_id, rule_priority, detected_at,
        processed, retry_count, is_selected, send_sms_date
    )
    SELECT campaign.tenant_id, eligible.user_id, campaign.id, campaign.priority, now(),
           FALSE, 0, FALSE, (campaign.created_at AT TIME ZONE 'Asia/Tehran')::date
    FROM pg_temp.instant_campaign_candidates eligible
    ON CONFLICT (user_id, tenant_id) DO NOTHING;

    DROP TABLE pg_temp.instant_campaign_candidates;
END;
$$;
