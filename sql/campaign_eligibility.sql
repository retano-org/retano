CREATE OR REPLACE FUNCTION public.process_campaign_eligibility() RETURNS void
    LANGUAGE plpgsql
    SET search_path = pg_catalog, public
    AS $$

DECLARE
    v_campaign RECORD;
BEGIN
    PERFORM pg_advisory_xact_lock(873421, 2);

    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'uq_trigger_results_user_tenant'
    ) THEN
        ALTER TABLE trigger_results
        ADD CONSTRAINT uq_trigger_results_user_tenant
        UNIQUE (user_id, tenant_id);
    END IF;

    FOR v_campaign IN
        SELECT
            c.id, c.tenant_id, c.rule_number,
            c.activation_base, c.comparison_type, c.comparison_value,
            c.value_unit, c.priority, c.buying_power, c.customer_type,
            c.gender, c.campaign_start_date, c.campaign_end_date,
            c.first_product_attribute,
            c.second_product_attribute
        FROM core_campaign c
        WHERE c.is_active = TRUE
    LOOP

        INSERT INTO trigger_results (
            tenant_id, user_id, rule_id, rule_priority, detected_at,
            status, processed, retry_count, sent_at, error_message, is_selected
        )

        WITH priority_rank AS (
            SELECT public.retano_campaign_priority(v_campaign.priority) AS prio_num
        ),

        gender_filter AS (
            SELECT CASE v_campaign.gender
                WHEN 'آقایان' THEN 'male'
                WHEN 'بانوان' THEN 'female'
                ELSE NULL
            END AS eng_gender
        ),

        customer_type_filter AS (
            SELECT CASE v_campaign.customer_type
                WHEN 'ویژه'         THEN 'vip'
                WHEN 'فعال'         THEN 'active'
                WHEN 'تازه وارد'    THEN 'new'
                WHEN 'در خطر ریزش' THEN 'at_risk'
                WHEN 'از دست رفته' THEN 'churned'
                ELSE NULL
            END AS eng_customer_type
        ),

        buying_power_filter AS (
            SELECT CASE v_campaign.buying_power
                WHEN 'خیلی بالا'  THEN 'Very High'
                WHEN 'بالا'       THEN 'High'
                WHEN 'متوسط'      THEN 'Medium'
                WHEN 'پایین'      THEN 'Low'
                WHEN 'خیلی پایین' THEN 'Very Low'
                ELSE NULL
            END AS eng_buying_power
        ),

        tenant_users AS (
            SELECT
                u.user_id,
                us.gender,
                us.rfm_segment,
                us.buying_power,
                us.last_purchase_date,
                us.total_spent,
                us.next_predicted_purchase_date
            FROM users u
            INNER JOIN user_summary us ON us.user_id = u.user_id
            WHERE u.tenant_id = v_campaign.tenant_id
        ),

        activation_filtered AS (

            SELECT tu.*
            FROM tenant_users tu
            WHERE v_campaign.activation_base = 'همیشه'

            UNION ALL

            SELECT tu.*
            FROM tenant_users tu
            WHERE v_campaign.activation_base = 'یادآوری خرید بعدی'
            AND tu.next_predicted_purchase_date IS NOT NULL
            AND CASE v_campaign.comparison_type
                    WHEN 'کوچکتر از' THEN
                    (
                        tu.next_predicted_purchase_date >=
                            COALESCE(v_campaign.campaign_start_date, CURRENT_DATE)
                        AND
                        tu.next_predicted_purchase_date <
                            COALESCE(v_campaign.campaign_start_date, CURRENT_DATE)
                            + (v_campaign.comparison_value * INTERVAL '1 day')
                    )
                    WHEN 'بزرگتر از' THEN
                    (
                        tu.next_predicted_purchase_date >
                            COALESCE(v_campaign.campaign_start_date, CURRENT_DATE)
                            + (v_campaign.comparison_value * INTERVAL '1 day')
                    )
                    WHEN 'برابر با' THEN
                    (
                        tu.next_predicted_purchase_date =
                            COALESCE(v_campaign.campaign_start_date, CURRENT_DATE)
                            + (v_campaign.comparison_value * INTERVAL '1 day')
                    )
                    ELSE FALSE
                END
            AND (
                    v_campaign.campaign_end_date IS NULL
                    OR
                    tu.next_predicted_purchase_date <= v_campaign.campaign_end_date
                )

            UNION ALL
            SELECT tu.*
            FROM tenant_users tu
            WHERE v_campaign.activation_base = 'قدرت خرید'

            UNION ALL
            SELECT tu.*
            FROM tenant_users tu
            WHERE v_campaign.activation_base = 'آخرین خرید'
              AND tu.last_purchase_date IS NOT NULL
              AND CASE v_campaign.comparison_type
                    WHEN 'بزرگتر از' THEN (CURRENT_DATE - tu.last_purchase_date) > v_campaign.comparison_value
                    WHEN 'کوچکتر از' THEN (CURRENT_DATE - tu.last_purchase_date) < v_campaign.comparison_value
                    WHEN 'برابر با'  THEN (CURRENT_DATE - tu.last_purchase_date) = v_campaign.comparison_value
                    ELSE FALSE
                  END

            UNION ALL
            SELECT tu.*
            FROM tenant_users tu
            INNER JOIN (
                SELECT o.user_id, MIN(o.order_date) AS first_order_date
                FROM orders o
                INNER JOIN users u2 ON u2.user_id = o.user_id
                WHERE u2.tenant_id = v_campaign.tenant_id
                GROUP BY o.user_id
                HAVING COUNT(o.order_id) = 1
            ) first_buyers ON first_buyers.user_id = tu.user_id
            WHERE v_campaign.activation_base = 'اولین خرید'
              AND CASE v_campaign.comparison_type
                    WHEN 'بزرگتر از' THEN (CURRENT_DATE - first_buyers.first_order_date) > v_campaign.comparison_value
                    WHEN 'کوچکتر از' THEN (CURRENT_DATE - first_buyers.first_order_date) < v_campaign.comparison_value
                    WHEN 'برابر با'  THEN (CURRENT_DATE - first_buyers.first_order_date) = v_campaign.comparison_value
                    ELSE FALSE
                  END
        ),

        gender_filtered AS (
            SELECT af.*
            FROM activation_filtered af
            CROSS JOIN gender_filter gf
            WHERE gf.eng_gender IS NULL
               OR af.gender = gf.eng_gender
        ),

        customer_type_filtered AS (
            SELECT gf.*
            FROM gender_filtered gf
            CROSS JOIN customer_type_filter ctf
            WHERE ctf.eng_customer_type IS NULL
               OR gf.rfm_segment = ctf.eng_customer_type
        ),

        buying_power_filtered AS (
            SELECT ctf.*
            FROM customer_type_filtered ctf
            CROSS JOIN buying_power_filter bpf
            WHERE bpf.eng_buying_power IS NULL
               OR ctf.buying_power = bpf.eng_buying_power
        ),

        spending_filtered AS (
            SELECT bpf.*
            FROM buying_power_filtered bpf
            WHERE v_campaign.value_unit IS NULL
               OR v_campaign.value_unit = 'روز'
               OR (
                    v_campaign.value_unit = 'تومان'
                    AND CASE v_campaign.comparison_type
                            WHEN 'بزرگتر از' THEN bpf.total_spent > v_campaign.comparison_value
                            WHEN 'کوچکتر از' THEN bpf.total_spent < v_campaign.comparison_value
                            WHEN 'برابر با'  THEN bpf.total_spent = v_campaign.comparison_value
                            ELSE FALSE
                        END
               )
        ),

        attribute_filtered AS (
            SELECT sf.*
            FROM spending_filtered sf
            WHERE
                (
                    v_campaign.first_product_attribute IN ('all', 'همه')
                    OR EXISTS (
                        SELECT 1
                        FROM user_attribute_scores uas
                        WHERE uas.user_id = sf.user_id
                          AND uas.attribute_value = v_campaign.first_product_attribute
                    )
                )
                AND
                (
                    v_campaign.second_product_attribute IN ('all', 'همه')
                    OR EXISTS (
                        SELECT 1
                        FROM user_attribute_scores uas
                        WHERE uas.user_id = sf.user_id
                          AND uas.attribute_value = v_campaign.second_product_attribute
                    )
                )
        ),

        priority_resolved AS (
            SELECT af.user_id
            FROM attribute_filtered af
            CROSS JOIN priority_rank pr
            WHERE NOT EXISTS (
                SELECT 1
                FROM core_campaign c2
                WHERE c2.tenant_id  = v_campaign.tenant_id
                  AND c2.id        != v_campaign.id
                  AND c2.is_active  = TRUE
                  AND (c2.campaign_start_date IS NULL OR c2.campaign_start_date <= CURRENT_DATE)
                  AND (c2.campaign_end_date   IS NULL OR c2.campaign_end_date   >= CURRENT_DATE)
                  AND public.retano_campaign_priority(c2.priority) < pr.prio_num
                  AND EXISTS (
                      SELECT 1
                      FROM user_summary us2
                      WHERE us2.user_id = af.user_id

                        AND (
                            c2.gender = 'همه'
                            OR (c2.gender = 'آقایان' AND us2.gender = 'male')
                            OR (c2.gender = 'بانوان' AND us2.gender = 'female')
                        )
                        AND (
                            c2.customer_type = 'همه'
                            OR (c2.customer_type = 'ویژه'         AND us2.rfm_segment = 'vip')
                            OR (c2.customer_type = 'فعال'         AND us2.rfm_segment = 'active')
                            OR (c2.customer_type = 'تازه وارد'    AND us2.rfm_segment = 'new')
                            OR (c2.customer_type = 'در خطر ریزش' AND us2.rfm_segment = 'at_risk')
                            OR (c2.customer_type = 'از دست رفته' AND us2.rfm_segment = 'churned')
                        )
                        AND (
                            c2.buying_power = 'همه'
                            OR (c2.buying_power = 'خیلی بالا'  AND us2.buying_power = 'Very High')
                            OR (c2.buying_power = 'بالا'        AND us2.buying_power = 'High')
                            OR (c2.buying_power = 'متوسط'       AND us2.buying_power = 'Medium')
                            OR (c2.buying_power = 'پایین'       AND us2.buying_power = 'Low')
                            OR (c2.buying_power = 'خیلی پایین'  AND us2.buying_power = 'Very Low')
                        )
                        AND (
                            CASE c2.activation_base
                                WHEN 'همیشه'             THEN TRUE
                                WHEN 'یادآوری خرید بعدی' THEN (
                                    us2.next_predicted_purchase_date IS NOT NULL
                                    AND CASE c2.comparison_type
                                            WHEN 'کوچکتر از' THEN (
                                                us2.next_predicted_purchase_date >= CURRENT_DATE
                                                AND us2.next_predicted_purchase_date <
                                                    COALESCE(c2.campaign_start_date, CURRENT_DATE)
                                            )
                                            WHEN 'بزرگتر از' THEN (
                                                us2.next_predicted_purchase_date >
                                                    COALESCE(c2.campaign_start_date, CURRENT_DATE)
                                            )
                                            WHEN 'برابر با' THEN (
                                                us2.next_predicted_purchase_date =
                                                    COALESCE(c2.campaign_start_date, CURRENT_DATE)
                                            )
                                            ELSE FALSE
                                        END
                                    AND (
                                        c2.campaign_end_date IS NULL
                                        OR us2.next_predicted_purchase_date <= c2.campaign_end_date
                                    )
                                )
                                WHEN 'قدرت خرید'         THEN TRUE
                                WHEN 'آخرین خرید' THEN (
                                    us2.last_purchase_date IS NOT NULL
                                    AND CASE c2.comparison_type
                                            WHEN 'بزرگتر از' THEN (CURRENT_DATE - us2.last_purchase_date) > c2.comparison_value
                                            WHEN 'کوچکتر از' THEN (CURRENT_DATE - us2.last_purchase_date) < c2.comparison_value
                                            WHEN 'برابر با'  THEN (CURRENT_DATE - us2.last_purchase_date) = c2.comparison_value
                                            ELSE FALSE
                                        END
                                )
                                WHEN 'اولین خرید' THEN (
                                    EXISTS (
                                        SELECT 1 FROM orders o2
                                        WHERE o2.user_id = af.user_id
                                        GROUP BY o2.user_id
                                        HAVING COUNT(o2.order_id) = 1
                                    )
                                    AND CASE c2.comparison_type
                                            WHEN 'بزرگتر از' THEN (CURRENT_DATE - us2.last_purchase_date) > c2.comparison_value
                                            WHEN 'کوچکتر از' THEN (CURRENT_DATE - us2.last_purchase_date) < c2.comparison_value
                                            WHEN 'برابر با'  THEN (CURRENT_DATE - us2.last_purchase_date) = c2.comparison_value
                                            ELSE FALSE
                                        END
                                )
                                ELSE FALSE
                            END
                        )
                        AND (
                            c2.value_unit IS NULL
                            OR c2.value_unit = 'روز'
                            OR (
                                c2.value_unit = 'تومان'
                                AND CASE c2.comparison_type
                                        WHEN 'بزرگتر از' THEN us2.monetary > c2.comparison_value
                                        WHEN 'کوچکتر از' THEN us2.monetary < c2.comparison_value
                                        WHEN 'برابر با'  THEN us2.monetary = c2.comparison_value
                                        ELSE FALSE
                                    END
                            )
                        )
                        AND (
                            c2.first_product_attribute IN ('all', 'همه')
                            OR EXISTS (
                                SELECT 1 FROM user_attribute_scores uas
                                WHERE uas.user_id = af.user_id
                                  AND uas.attribute_value = c2.first_product_attribute
                            )
                        )
                        AND (
                            c2.second_product_attribute IN ('all', 'همه')
                            OR EXISTS (
                                SELECT 1 FROM user_attribute_scores uas
                                WHERE uas.user_id = af.user_id
                                  AND uas.attribute_value = c2.second_product_attribute
                            )
                        )
                  )
            )
        )

        SELECT
            v_campaign.tenant_id,
            pr.user_id,
            v_campaign.id,
            v_campaign.priority,
            NOW(),
            NULL,
            FALSE,
            0,
            NULL,
            NULL,
            FALSE
        FROM priority_resolved pr

        ON CONFLICT (user_id, tenant_id)
        DO UPDATE SET
            rule_id = EXCLUDED.rule_id,
            instant_campaign_id = NULL,
            rule_priority = EXCLUDED.rule_priority,
            detected_at = EXCLUDED.detected_at,
            status = NULL,
            processed = FALSE,
            retry_count = 0,
            error_message = NULL,
            is_selected = FALSE,
            sent_at = NULL,
            send_sms_date = NULL,
            final_message = NULL,
            sms_message_id = NULL,
            delivered_at = NULL
        -- A pending instant campaign owns the slot. A sent result can be
        -- archived when a subsequent campaign takes that slot.
        WHERE trigger_results.sent_at IS NOT NULL
          OR (
              trigger_results.instant_campaign_id IS NULL
              AND public.retano_campaign_priority(EXCLUDED.rule_priority)
                 < public.retano_campaign_priority((
                     SELECT current_campaign.priority
                     FROM public.core_campaign current_campaign
                     WHERE current_campaign.id = trigger_results.rule_id
                 ))
          );

    END LOOP;

END;
$$;
