CREATE FUNCTION public.retano_fill_campaign_messages(p_instant_campaign_id bigint) RETURNS void
    LANGUAGE plpgsql
    SET search_path = pg_catalog, public
    AS $$

DECLARE
    rec                     RECORD;
    v_first_name            TEXT;
    v_display_name          TEXT;
    v_coupon_code           TEXT;
    v_coupon_id             BIGINT;
    v_message               TEXT;

    v_last_order_id         TEXT;

    -- Shared top_1/top_2/top_3 resolution (one chain, three rotations)
    v_id_top1                TEXT;
    v_id_top2                TEXT;
    v_id_top3                TEXT;

    -- Independent rep_topN resolution
    v_id_rep1                TEXT;
    v_id_rep2                TEXT;
    v_id_rep3                TEXT;

    -- Resolved product names
    v_name_top1               TEXT;
    v_name_top2               TEXT;
    v_name_top3               TEXT;
    v_name_rep1                TEXT;
    v_name_rep2                TEXT;
    v_name_rep3                TEXT;

    -- Resolved product links
    v_link_top1                TEXT;
    v_link_top2                TEXT;
    v_link_top3                TEXT;
    v_link_rep1                 TEXT;
    v_link_rep2                 TEXT;
    v_link_rep3                 TEXT;

    v_blocked_top1            BOOLEAN;
    v_blocked_top2            BOOLEAN;
    v_blocked_top3            BOOLEAN;

    -- SHORT URL GENERATION state ------------------------------------------
    v_short_base              CONSTANT TEXT := 'https://retano.ir/r/';
    v_alphabet                CONSTANT TEXT := '0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz';

    -- Owning tenant of each resolved product (taken from products.tenant_id)
    v_tenant_top1             BIGINT;
    v_tenant_top2             BIGINT;
    v_tenant_top3             BIGINT;
    v_tenant_rep1             BIGINT;
    v_tenant_rep2             BIGINT;
    v_tenant_rep3             BIGINT;

    v_slot_pid                TEXT[];     -- product ids, slot order: top1..3, rep1..3
    v_slot_tid                BIGINT[];   -- tenant ids, same order
    v_slot_url                TEXT[];     -- urls, same order (replaced by short urls)
    v_i                       INT;
    v_j                       INT;
    v_url                     TEXT;
    v_key                     TEXT;
    v_cached                  JSONB;
    v_cache                   JSONB := '{}'::jsonb;  -- per-call: tenant:product -> {u: original, s: short}
    v_short                   TEXT;
    v_row_id                  UUID;
    v_existing_code           TEXT;
    v_existing_url            TEXT;
    v_code                    TEXT;
    v_len                     INT;
    v_attempt                 INT;
    v_inserted                TEXT;
    v_bytes                   BYTEA;
    v_byte                    INT;

BEGIN
    PERFORM pg_advisory_xact_lock(873421, 2);

    FOR rec IN
        SELECT
            tr.id                           AS tr_id,
            tr.user_id                      AS tr_user_id,
            tr.tenant_id                    AS tr_tenant_id,
            coalesce(ic.id, cc.id) AS campaign_id,
            coalesce(ic.message_pattern, cc.message_pattern) AS message_pattern,
            coalesce(ic.coupon_discount_percentage, cc.coupon_discount_percentage) AS coupon_discount_pct,
            p_instant_campaign_id IS NOT NULL AS is_instant,
            us.top_1,
            us.top_2,
            us.top_3,
            us.rep_top1,
            us.rep_top2,
            us.rep_top3,
            us.last_purchase_product_id     AS last_purchase_product_id
        FROM trigger_results tr
        LEFT JOIN core_campaign cc
            ON cc.id = tr.rule_id AND cc.tenant_id = tr.tenant_id
        LEFT JOIN core_instantcampaign ic
            ON ic.id = tr.instant_campaign_id AND ic.tenant_id = tr.tenant_id
        JOIN user_summary us
            ON us.user_id = tr.user_id
        WHERE tr.sent_at IS NULL
          AND (
              (p_instant_campaign_id IS NULL AND cc.id IS NOT NULL)
              OR ic.id = p_instant_campaign_id
          )
    LOOP

        -- ----------------------------------------------------------------
        -- FIRST NAME RESOLUTION (unchanged)
        -- ----------------------------------------------------------------
        SELECT first_name
        INTO v_first_name
        FROM users
        WHERE user_id = rec.tr_user_id
        LIMIT 1;

        IF v_first_name IS NULL
            OR TRIM(v_first_name) = ''
            OR v_first_name ~ '[^\u0600-\u06FF\s]'
        THEN
            v_display_name := 'کاربر عزیز';
        ELSE
            v_display_name := v_first_name;
        END IF;

        -- ----------------------------------------------------------------
        -- LAST ORDER (used for blocking checks)
        -- ----------------------------------------------------------------
        SELECT o.order_id
        INTO v_last_order_id
        FROM orders o
        WHERE o.user_id = rec.tr_user_id
        ORDER BY o.order_date DESC
        LIMIT 1;

        -- ----------------------------------------------------------------
        -- Precompute "is this candidate blocked" once per candidate
        -- (blocked = present in the user's most recent order)
        -- ----------------------------------------------------------------
        v_blocked_top1 := (
            rec.top_1 IS NOT NULL
            AND v_last_order_id IS NOT NULL
            AND EXISTS (
                SELECT 1 FROM order_items oi
                WHERE oi.order_id = v_last_order_id AND oi.product_id = rec.top_1
            )
        );
        v_blocked_top2 := (
            rec.top_2 IS NOT NULL
            AND v_last_order_id IS NOT NULL
            AND EXISTS (
                SELECT 1 FROM order_items oi
                WHERE oi.order_id = v_last_order_id AND oi.product_id = rec.top_2
            )
        );
        v_blocked_top3 := (
            rec.top_3 IS NOT NULL
            AND v_last_order_id IS NOT NULL
            AND EXISTS (
                SELECT 1 FROM order_items oi
                WHERE oi.order_id = v_last_order_id AND oi.product_id = rec.top_3
            )
        );

        -- ----------------------------------------------------------------
        -- SINGLE SHARED CHAIN RESOLUTION for top_1 / top_2 / top_3
        -- {top_1} -> top_1 -> top_2 -> top_3
        -- {top_2} -> top_2 -> top_1 -> top_3
        -- {top_3} -> top_3 -> top_1 -> top_2
        -- A candidate is skipped if NULL or blocked.
        -- If every candidate in a given rotation is NULL-or-blocked,
        -- that placeholder resolves to NULL (empty string later).
        -- If ALL THREE underlying values (top_1, top_2, top_3) are
        -- NULL-or-blocked, the user is removed from trigger_results
        -- and skipped entirely (this check happens once, shared).
        -- ----------------------------------------------------------------

        -- {top_1} rotation: top_1 -> top_2 -> top_3
        IF NOT v_blocked_top1 AND rec.top_1 IS NOT NULL THEN
            v_id_top1 := rec.top_1;
        ELSIF NOT v_blocked_top2 AND rec.top_2 IS NOT NULL THEN
            v_id_top1 := rec.top_2;
        ELSIF NOT v_blocked_top3 AND rec.top_3 IS NOT NULL THEN
            v_id_top1 := rec.top_3;
        ELSE
            v_id_top1 := NULL;
        END IF;

        -- {top_2} rotation: top_2 -> top_1 -> top_3
        IF NOT v_blocked_top2 AND rec.top_2 IS NOT NULL THEN
            v_id_top2 := rec.top_2;
        ELSIF NOT v_blocked_top1 AND rec.top_1 IS NOT NULL THEN
            v_id_top2 := rec.top_1;
        ELSIF NOT v_blocked_top3 AND rec.top_3 IS NOT NULL THEN
            v_id_top2 := rec.top_3;
        ELSE
            v_id_top2 := NULL;
        END IF;

        -- {top_3} rotation: top_3 -> top_1 -> top_2
        IF NOT v_blocked_top3 AND rec.top_3 IS NOT NULL THEN
            v_id_top3 := rec.top_3;
        ELSIF NOT v_blocked_top1 AND rec.top_1 IS NOT NULL THEN
            v_id_top3 := rec.top_1;
        ELSIF NOT v_blocked_top2 AND rec.top_2 IS NOT NULL THEN
            v_id_top3 := rec.top_2;
        ELSE
            v_id_top3 := NULL;
        END IF;

        -- Shared exhaustion check: all three underlying values NULL-or-blocked
        IF (rec.top_1 IS NULL OR v_blocked_top1)
           AND (rec.top_2 IS NULL OR v_blocked_top2)
           AND (rec.top_3 IS NULL OR v_blocked_top3)
        THEN
            DELETE FROM trigger_results WHERE id = rec.tr_id;
            CONTINUE;
        END IF;

        -- ----------------------------------------------------------------
        -- REPEATED-PURCHASE CHAIN — each placeholder resolved independently
        -- {rep_top1}: rep_top1 -> last_purchase_product_id
        -- {rep_top2}: rep_top2 -> last_purchase_product_id
        -- {rep_top3}: rep_top3 -> last_purchase_product_id
        -- last_purchase_product_id is never blocked, never deletes.
        -- ----------------------------------------------------------------

        -- {rep_top1}
        IF rec.rep_top1 IS NOT NULL AND NOT (
            v_last_order_id IS NOT NULL AND EXISTS (
                SELECT 1 FROM order_items oi
                WHERE oi.order_id = v_last_order_id AND oi.product_id = rec.rep_top1
            )
        ) THEN
            v_id_rep1 := rec.rep_top1;
        ELSE
            v_id_rep1 := rec.last_purchase_product_id;
        END IF;

        -- {rep_top2}
        IF rec.rep_top2 IS NOT NULL AND NOT (
            v_last_order_id IS NOT NULL AND EXISTS (
                SELECT 1 FROM order_items oi
                WHERE oi.order_id = v_last_order_id AND oi.product_id = rec.rep_top2
            )
        ) THEN
            v_id_rep2 := rec.rep_top2;
        ELSE
            v_id_rep2 := rec.last_purchase_product_id;
        END IF;

        -- {rep_top3}
        IF rec.rep_top3 IS NOT NULL AND NOT (
            v_last_order_id IS NOT NULL AND EXISTS (
                SELECT 1 FROM order_items oi
                WHERE oi.order_id = v_last_order_id AND oi.product_id = rec.rep_top3
            )
        ) THEN
            v_id_rep3 := rec.rep_top3;
        ELSE
            v_id_rep3 := rec.last_purchase_product_id;
        END IF;

        -- ----------------------------------------------------------------
        -- RESOLVE PRODUCT NAMES + LINKS (NULL id -> NULL -> empty string later)
        -- ----------------------------------------------------------------
        v_name_top1 := NULL; v_link_top1 := NULL; v_tenant_top1 := NULL;
        v_name_top2 := NULL; v_link_top2 := NULL; v_tenant_top2 := NULL;
        v_name_top3 := NULL; v_link_top3 := NULL; v_tenant_top3 := NULL;
        v_name_rep1 := NULL; v_link_rep1 := NULL; v_tenant_rep1 := NULL;
        v_name_rep2 := NULL; v_link_rep2 := NULL; v_tenant_rep2 := NULL;
        v_name_rep3 := NULL; v_link_rep3 := NULL; v_tenant_rep3 := NULL;

        IF v_id_top1 IS NOT NULL THEN
            SELECT p.name, p.product_link, p.tenant_id INTO v_name_top1, v_link_top1, v_tenant_top1
            FROM products p WHERE p.product_id = v_id_top1 LIMIT 1;
        END IF;

        IF v_id_top2 IS NOT NULL THEN
            SELECT p.name, p.product_link, p.tenant_id INTO v_name_top2, v_link_top2, v_tenant_top2
            FROM products p WHERE p.product_id = v_id_top2 LIMIT 1;
        END IF;

        IF v_id_top3 IS NOT NULL THEN
            SELECT p.name, p.product_link, p.tenant_id INTO v_name_top3, v_link_top3, v_tenant_top3
            FROM products p WHERE p.product_id = v_id_top3 LIMIT 1;
        END IF;

        IF v_id_rep1 IS NOT NULL THEN
            SELECT p.name, p.product_link, p.tenant_id INTO v_name_rep1, v_link_rep1, v_tenant_rep1
            FROM products p WHERE p.product_id = v_id_rep1 LIMIT 1;
        END IF;

        IF v_id_rep2 IS NOT NULL THEN
            SELECT p.name, p.product_link, p.tenant_id INTO v_name_rep2, v_link_rep2, v_tenant_rep2
            FROM products p WHERE p.product_id = v_id_rep2 LIMIT 1;
        END IF;

        IF v_id_rep3 IS NOT NULL THEN
            SELECT p.name, p.product_link, p.tenant_id INTO v_name_rep3, v_link_rep3, v_tenant_rep3
            FROM products p WHERE p.product_id = v_id_rep3 LIMIT 1;
        END IF;

        -- ----------------------------------------------------------------
        -- SHORT URL GENERATION
        -- Replace each non-NULL http(s) product link with a tracked short link
        --   https://retano.ir/r/{short_code}
        -- Mapping is canonical per (tenant_id, product_id) in
        -- product_link_tracking. Anything that is not a usable http(s) URL
        -- (NULL, blank, other scheme) is left exactly as before.
        -- Concurrency: creation relies on the UNIQUE constraints plus
        -- INSERT ... ON CONFLICT DO NOTHING; no read-then-write race is possible.
        -- ----------------------------------------------------------------
        v_slot_pid := ARRAY[v_id_top1,     v_id_top2,     v_id_top3,     v_id_rep1,     v_id_rep2,     v_id_rep3];
        v_slot_tid := ARRAY[v_tenant_top1, v_tenant_top2, v_tenant_top3, v_tenant_rep1, v_tenant_rep2, v_tenant_rep3];
        v_slot_url := ARRAY[v_link_top1,   v_link_top2,   v_link_top3,   v_link_rep1,   v_link_rep2,   v_link_rep3];

        FOR v_i IN 1..6 LOOP
            v_url := btrim(v_slot_url[v_i]);

            -- NULL product / tenant / url, blank url, or non-http(s) url: leave untouched.
            IF v_slot_pid[v_i] IS NULL
               OR v_slot_tid[v_i] IS NULL
               OR v_url IS NULL
               OR v_url = ''
               OR v_url !~* '^https?://'
            THEN
                CONTINUE;
            END IF;

            -- Same product already handled earlier in this call (e.g. top_1 == rep_top1,
            -- or the same product for many users): no query needed.
            v_key    := v_slot_tid[v_i]::TEXT || ':' || v_slot_pid[v_i];
            v_cached := v_cache -> v_key;
            IF v_cached IS NOT NULL AND v_cached ->> 'u' = v_url THEN
                v_slot_url[v_i] := v_cached ->> 's';
                CONTINUE;
            END IF;

            v_short := NULL;

            -- 1) Existing mapping for (tenant, product)?
            SELECT t.id, t.short_code, t.original_url
            INTO v_row_id, v_existing_code, v_existing_url
            FROM product_link_tracking t
            WHERE t.tenant_id = v_slot_tid[v_i]
              AND t.product_id = v_slot_pid[v_i];

            IF FOUND THEN
                -- Destination changed: update the existing mapping, keep the short code.
                IF v_existing_url IS DISTINCT FROM v_url THEN
                    UPDATE product_link_tracking
                    SET original_url = v_url, updated_at = NOW()
                    WHERE id = v_row_id;
                END IF;
                v_short := v_short_base || v_existing_code;
            ELSE
                -- 2) Create: random Base62 code, 6 chars (grows if collisions persist).
                v_attempt := 0;
                v_len     := 6;
                LOOP
                    v_attempt := v_attempt + 1;

                    -- Base62 with rejection sampling (bytes >= 248 are discarded) so
                    -- every character is uniformly distributed. Randomness comes from
                    -- gen_random_uuid() (CSPRNG, built in); bytes 6 and 8 carry the
                    -- fixed UUID version/variant bits and are skipped.
                    v_code := '';
                    WHILE length(v_code) < v_len LOOP
                        v_bytes := uuid_send(gen_random_uuid());
                        FOR v_j IN 0..15 LOOP
                            CONTINUE WHEN v_j IN (6, 8);
                            v_byte := get_byte(v_bytes, v_j);
                            IF v_byte < 248 THEN
                                v_code := v_code || substr(v_alphabet, (v_byte % 62) + 1, 1);
                                EXIT WHEN length(v_code) >= v_len;
                            END IF;
                        END LOOP;
                    END LOOP;

                    INSERT INTO product_link_tracking
                        (tenant_id, product_id, original_url, short_url, short_code)
                    VALUES
                        (v_slot_tid[v_i], v_slot_pid[v_i], v_url, v_short_base || v_code, v_code)
                    ON CONFLICT DO NOTHING
                    RETURNING short_code INTO v_inserted;

                    IF v_inserted IS NOT NULL THEN
                        v_short := v_short_base || v_inserted;
                        EXIT;
                    END IF;

                    -- Nothing inserted: either a concurrent run created the same
                    -- (tenant, product) mapping, or the random code collided.
                    SELECT t.id, t.short_code, t.original_url
                    INTO v_row_id, v_existing_code, v_existing_url
                    FROM product_link_tracking t
                    WHERE t.tenant_id = v_slot_tid[v_i]
                      AND t.product_id = v_slot_pid[v_i];

                    IF FOUND THEN
                        -- Concurrent creator won: reuse its code.
                        IF v_existing_url IS DISTINCT FROM v_url THEN
                            UPDATE product_link_tracking
                            SET original_url = v_url, updated_at = NOW()
                            WHERE id = v_row_id;
                        END IF;
                        v_short := v_short_base || v_existing_code;
                        EXIT;
                    END IF;

                    -- Short-code collision: retry with a fresh code.
                    IF v_attempt >= 20 THEN
                        RAISE WARNING
                            'SHORT_URL_FAILED: could not allocate a unique short code after % attempts | tenant_id=% | product_id=% | trigger_results_id=%. Original URL left in message.',
                            v_attempt, v_slot_tid[v_i], v_slot_pid[v_i], rec.tr_id;
                        EXIT;
                    END IF;
                    IF v_attempt % 5 = 0 THEN
                        v_len := LEAST(v_len + 1, 12);
                    END IF;
                END LOOP;
            END IF;

            IF v_short IS NOT NULL THEN
                v_slot_url[v_i] := v_short;
                v_cache := v_cache || jsonb_build_object(v_key, jsonb_build_object('u', v_url, 's', v_short));
            END IF;
        END LOOP;

        v_link_top1 := v_slot_url[1];
        v_link_top2 := v_slot_url[2];
        v_link_top3 := v_slot_url[3];
        v_link_rep1 := v_slot_url[4];
        v_link_rep2 := v_slot_url[5];
        v_link_rep3 := v_slot_url[6];

        -- ----------------------------------------------------------------
        -- COUPON RESOLUTION (unchanged)
        -- ----------------------------------------------------------------
        v_coupon_code := NULL;
        v_coupon_id   := NULL;

        IF rec.coupon_discount_pct IS NOT NULL AND rec.coupon_discount_pct > 0 THEN

            SELECT c.id, c.coupon_code
            INTO v_coupon_id, v_coupon_code
            FROM core_coupon c
            WHERE c.tenant_id            = rec.tr_tenant_id
              AND c.discount_percentage  = rec.coupon_discount_pct
              AND c.status               = 'available'
            ORDER BY c.id ASC
            LIMIT 1;

            IF v_coupon_id IS NULL THEN

                RAISE WARNING
                    'NO_COUPON_FOUND: No available coupon with discount % found for tenant_id=% | campaign_id=% | trigger_results_id=%',
                    rec.coupon_discount_pct,
                    rec.tr_tenant_id,
                    rec.campaign_id,
                    rec.tr_id;

                INSERT INTO core_errorlog (
                    tenant_id,
                    source,
                    severity,
                    error_code,
                    message,
                    context,
                    resolved,
                    created_at,
                    updated_at
                ) VALUES (
                    rec.tr_tenant_id,
                    'coupon',
                    'error',
                    'NO_COUPON_FOUND',
                    'هیچ کوپنی با درصد تخفیف ' || rec.coupon_discount_pct ||
                    '% برای این کمپین موجود نیست. لطفاً درصد تخفیف دیگری انتخاب کنید.',
                    jsonb_build_object(
                        'campaign_id',         rec.campaign_id,
                        'is_instant_campaign', rec.is_instant,
                        'trigger_result_id',   rec.tr_id,
                        'tenant_id',           rec.tr_tenant_id,
                        'discount_percentage', rec.coupon_discount_pct
                    ),
                    false,
                    NOW(),
                    NOW()
                );

                CONTINUE;
            END IF;

        END IF;

        -- ----------------------------------------------------------------
        -- MESSAGE ASSEMBLY
        -- ----------------------------------------------------------------
        v_message := rec.message_pattern;

        v_message := REPLACE(v_message, '{first_name}',      v_display_name);
        v_message := REPLACE(v_message, '{top_1}',           COALESCE(v_name_top1, ''));
        v_message := REPLACE(v_message, '{top_2}',           COALESCE(v_name_top2, ''));
        v_message := REPLACE(v_message, '{top_3}',           COALESCE(v_name_top3, ''));
        v_message := REPLACE(v_message, '{rep_top1}',        COALESCE(v_name_rep1, ''));
        v_message := REPLACE(v_message, '{rep_top2}',        COALESCE(v_name_rep2, ''));
        v_message := REPLACE(v_message, '{rep_top3}',        COALESCE(v_name_rep3, ''));
        v_message := REPLACE(v_message, '{top_1_link}',      COALESCE(v_link_top1, ''));
        v_message := REPLACE(v_message, '{top_2_link}',      COALESCE(v_link_top2, ''));
        v_message := REPLACE(v_message, '{top_3_link}',      COALESCE(v_link_top3, ''));
        v_message := REPLACE(v_message, '{rep_top1_link}',   COALESCE(v_link_rep1, ''));
        v_message := REPLACE(v_message, '{rep_top2_link}',   COALESCE(v_link_rep2, ''));
        v_message := REPLACE(v_message, '{rep_top3_link}',   COALESCE(v_link_rep3, ''));
        v_message := REPLACE(v_message, '{coupon}',          COALESCE(v_coupon_code, ''));

        UPDATE trigger_results
        SET final_message = v_message
        WHERE id = rec.tr_id;

        IF v_coupon_id IS NOT NULL THEN
            UPDATE core_coupon
            SET status = 'reserved'
            WHERE id = v_coupon_id;
        END IF;

    END LOOP;

END;
$$;

CREATE OR REPLACE FUNCTION public.fill_final_messages()
RETURNS void
LANGUAGE sql
AS $$
    SELECT public.retano_fill_campaign_messages(NULL);
$$;

CREATE FUNCTION public.fill_final_messages_for_instant_campaign(p_campaign_id bigint)
RETURNS void
LANGUAGE sql
AS $$
    SELECT public.retano_fill_campaign_messages(p_campaign_id) WHERE p_campaign_id IS NOT NULL;
$$;

-- These invoker functions are called by Django's database role.
DO $$
DECLARE
    function_signature text;
    api_role text;
BEGIN
    FOREACH function_signature IN ARRAY ARRAY[
        'public.process_instant_campaign_eligibility(bigint)',
        'public.fill_final_messages_for_instant_campaign(bigint)',
        'public.retano_fill_campaign_messages(bigint)',
        'public.retano_instant_campaign_matches(public.core_instantcampaign, public.user_summary)'
    ]
    LOOP
        EXECUTE format('REVOKE EXECUTE ON FUNCTION %s FROM PUBLIC', function_signature);
        FOR api_role IN SELECT rolname FROM pg_roles WHERE rolname IN ('anon', 'authenticated', 'service_role')
        LOOP
            EXECUTE format('REVOKE EXECUTE ON FUNCTION %s FROM %I', function_signature, api_role);
        END LOOP;
    END LOOP;
END;
$$;
