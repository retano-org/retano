ALTER TABLE public.core_manualuploadpair ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.core_sentcampaignmessage ENABLE ROW LEVEL SECURITY;

CREATE FUNCTION public.retano_archive_campaign_send()
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
            trigger_result_id, tenant_id, user_id, rule_id, sent_at,
            sms_message_id, delivered_at, snapshot
        ) VALUES (
            OLD.id, OLD.tenant_id, OLD.user_id, OLD.rule_id, OLD.sent_at,
            OLD.sms_message_id, OLD.delivered_at, to_jsonb(OLD)
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

CREATE TRIGGER retano_archive_campaign_send
BEFORE UPDATE OF sent_at, sms_message_id OR DELETE ON public.trigger_results
FOR EACH ROW EXECUTE FUNCTION public.retano_archive_campaign_send();


CREATE FUNCTION public.retano_replace_manual_upload_data(
    p_tenant_id bigint, p_replace_products boolean, p_expired_product_job uuid
) RETURNS void
LANGUAGE plpgsql
SET search_path = pg_catalog, public
AS $$
DECLARE
    affected_users text[];
BEGIN
    -- Match the existing allocator/flush lock so ETL can retain its own path.
    PERFORM pg_advisory_xact_lock(p_tenant_id);

    IF p_expired_product_job IS NULL THEN
        DELETE FROM public.users_unnormalized_data
        WHERE tenant_id = p_tenant_id
          AND column_mapping ? 'customers_file_mapping'
          AND column_mapping->>'source' IS DISTINCT FROM 'automated_sync';
    END IF;

    IF p_replace_products THEN
        DELETE FROM public.products_unnormalized_data
        WHERE tenant_id = p_tenant_id
          AND column_mapping ? 'products_file_mapping'
          AND column_mapping->>'source' IS DISTINCT FROM 'automated_sync'
          AND (
              p_expired_product_job IS NULL
              OR column_mapping->>'upload_job_id' = p_expired_product_job::text
          );
    END IF;

    -- The deployed AFTER DELETE triggers collect only the rows just removed.
    CREATE TEMP TABLE manual_upload_users ON COMMIT DROP AS
    SELECT DISTINCT user_id
    FROM public._retano_deleted_identity
    WHERE xid = txid_current() AND tenant_id = p_tenant_id AND user_id IS NOT NULL;

    CREATE TEMP TABLE manual_upload_orders ON COMMIT DROP AS
    SELECT DISTINCT order_id
    FROM public._retano_deleted_identity
    WHERE xid = txid_current() AND tenant_id = p_tenant_id AND order_id IS NOT NULL;

    CREATE TEMP TABLE manual_upload_products ON COMMIT DROP AS
    SELECT DISTINCT product_id
    FROM public._retano_deleted_identity
    WHERE xid = txid_current() AND tenant_id = p_tenant_id AND product_id IS NOT NULL;
    CREATE INDEX ON manual_upload_users (user_id);
    CREATE INDEX ON manual_upload_orders (order_id);
    CREATE INDEX ON manual_upload_products (product_id);

    DELETE FROM public._retano_deleted_identity
    WHERE xid = txid_current() AND tenant_id = p_tenant_id;

    IF NOT EXISTS (SELECT 1 FROM pg_temp.manual_upload_users)
       AND NOT EXISTS (SELECT 1 FROM pg_temp.manual_upload_products) THEN
        RETURN;
    END IF;

    CREATE TEMP TABLE manual_catalog_survivors ON COMMIT DROP AS
    SELECT DISTINCT ON (p.product_id)
           p.product_id::text AS product_id, p.product_name, p.product_category,
           p.current_product_price, p.product_link,
           p.first_product_attribute, p.second_product_attribute, p.created_at
    FROM public.products_unnormalized_data p
    JOIN pg_temp.manual_upload_products a ON p.product_id::text = a.product_id
    WHERE p.tenant_id = p_tenant_id
    ORDER BY p.product_id, p.created_at DESC, p.id DESC;

    IF p_replace_products THEN
        UPDATE public.products p
        SET name = public.normalize_text(s.product_name),
            category = public.normalize_text(s.product_category),
            price = s.current_product_price,
            product_link = public.normalize_product_link(s.product_link),
            first_product_attribute = s.first_product_attribute,
            second_product_attribute = s.second_product_attribute,
            created_at = s.created_at::timestamp
        FROM pg_temp.manual_catalog_survivors s
        WHERE p.tenant_id = p_tenant_id AND p.product_id = s.product_id;
    END IF;

    CREATE TEMP TABLE manual_customer_survivors ON COMMIT DROP AS
    SELECT u.*
    FROM public.users_unnormalized_data u
    WHERE u.tenant_id = p_tenant_id AND (
        EXISTS (SELECT 1 FROM pg_temp.manual_upload_users a WHERE a.user_id = u.user_id::text)
        OR EXISTS (SELECT 1 FROM pg_temp.manual_upload_orders a WHERE a.order_id = u.order_id::text)
        OR EXISTS (SELECT 1 FROM pg_temp.manual_upload_products a WHERE a.product_id = u.product_id::text)
    );
    CREATE INDEX ON manual_customer_survivors ((user_id::text));
    CREATE INDEX ON manual_customer_survivors ((order_id::text));
    CREATE INDEX ON manual_customer_survivors ((product_id::text));

    CREATE TEMP TABLE manual_orphan_users ON COMMIT DROP AS
    SELECT a.user_id
    FROM pg_temp.manual_upload_users a
    WHERE NOT EXISTS (
        SELECT 1 FROM pg_temp.manual_customer_survivors s WHERE s.user_id::text = a.user_id
    );

    -- Preserve campaign logs and old links. The trigger archives sent rows.
    DELETE FROM public.trigger_results tr
    USING pg_temp.manual_orphan_users a
    WHERE tr.tenant_id = p_tenant_id AND tr.user_id = a.user_id;

    -- Existing order items come from flat rows which still have a catalog entry.
    -- A surviving order can have both manual and sync lines.
    DELETE FROM public.order_items oi
    WHERE oi.tenant_id = p_tenant_id
      AND (
          EXISTS (SELECT 1 FROM pg_temp.manual_upload_orders a WHERE a.order_id = oi.order_id)
          OR EXISTS (SELECT 1 FROM pg_temp.manual_upload_products a WHERE a.product_id = oi.product_id)
      )
      AND NOT EXISTS (
          SELECT 1
          FROM pg_temp.manual_customer_survivors u
          JOIN public.products_unnormalized_data p
            ON p.tenant_id = p_tenant_id AND p.internal_product_id = u.internal_product_id
          WHERE u.order_id::text = oi.order_id AND p.product_id::text = oi.product_id
      );

    WITH remaining_lines AS (
        SELECT DISTINCT ON (u.order_id, p.product_id)
               u.order_id::text AS order_id, p.product_id::text AS product_id,
               u.quantity, u.quantity * u.then_product_price AS subtotal
        FROM pg_temp.manual_customer_survivors u
        JOIN public.products_unnormalized_data p
          ON p.tenant_id = p_tenant_id AND p.internal_product_id = u.internal_product_id
        ORDER BY u.order_id, p.product_id, u.created_at DESC, u.id DESC
    )
    UPDATE public.order_items oi
    SET quantity = r.quantity, price = p.price, subtotal = r.subtotal
    FROM remaining_lines r JOIN public.products p ON p.product_id = r.product_id
    WHERE oi.tenant_id = p_tenant_id AND oi.order_id = r.order_id
      AND oi.product_id = r.product_id
      AND (
          EXISTS (SELECT 1 FROM pg_temp.manual_upload_orders a WHERE a.order_id = oi.order_id)
          OR (p_replace_products AND EXISTS (
              SELECT 1 FROM pg_temp.manual_upload_products a WHERE a.product_id = oi.product_id
          ))
      );

    DELETE FROM public.orders o
    USING pg_temp.manual_upload_orders a
    WHERE o.tenant_id = p_tenant_id AND o.order_id = a.order_id
      AND NOT EXISTS (
          SELECT 1 FROM pg_temp.manual_customer_survivors s WHERE s.order_id::text = a.order_id
      );

    WITH totals AS (
        SELECT u.order_id::text AS order_id, sum(u.quantity) AS product_count,
               sum(u.quantity * u.then_product_price) AS total_amount,
               min(u.created_at) AS created_at
        FROM pg_temp.manual_customer_survivors u
        JOIN pg_temp.manual_upload_orders a ON a.order_id = u.order_id::text
        GROUP BY u.order_id
    ),
    latest AS (
        SELECT DISTINCT ON (u.order_id) u.order_id::text AS order_id,
               u.user_id::text AS user_id, u.order_date::date AS order_date
        FROM pg_temp.manual_customer_survivors u
        ORDER BY u.order_id, u.created_at DESC, u.id DESC
    )
    UPDATE public.orders o
    SET user_id = l.user_id, order_date = l.order_date,
        product_count = t.product_count, total_amount = t.total_amount, created_at = t.created_at
    FROM totals t JOIN latest l USING (order_id)
    WHERE o.tenant_id = p_tenant_id AND o.order_id = t.order_id;

    WITH latest AS (
        SELECT DISTINCT ON (u.user_id) u.*
        FROM pg_temp.manual_customer_survivors u
        JOIN pg_temp.manual_upload_users a ON a.user_id = u.user_id::text
        ORDER BY u.user_id, u.created_at DESC, u.id DESC
    )
    UPDATE public.users u
    SET first_name = left(public.normalize_text(l.first_name), 100),
        last_name = left(public.normalize_text(l.last_name), 100),
        gender = public.normalize_gender(l.gender),
        phone_number = public.normalize_phone(l.phone_number)
    FROM latest l
    WHERE u.tenant_id = p_tenant_id AND u.user_id = l.user_id::text;

    -- Legacy campaign history has foreign keys to user and product records.
    -- Keep the required identities and remove the old uploaded details.
    DELETE FROM public.user_summary s USING pg_temp.manual_orphan_users a
    WHERE s.tenant_id = p_tenant_id AND s.user_id = a.user_id;
    DELETE FROM public.users_summary_rfm s USING pg_temp.manual_orphan_users a
    WHERE s.tenant_id = p_tenant_id AND s.user_id = a.user_id;
    DELETE FROM public.users_summary_rfm_segmented s USING pg_temp.manual_orphan_users a
    WHERE s.tenant_id = p_tenant_id AND s.user_id = a.user_id;
    DELETE FROM public.retention_history h USING pg_temp.manual_orphan_users a
    WHERE h.tenant_id = p_tenant_id AND h.user_id = a.user_id;

    SELECT array_agg(a.user_id) INTO affected_users
    FROM (
        SELECT user_id FROM pg_temp.manual_upload_users
        UNION
        SELECT DISTINCT user_id::text FROM pg_temp.manual_customer_survivors
    ) a;

    DELETE FROM public.user_attribute_scores a
    WHERE a.tenant_id = p_tenant_id AND a.user_id = ANY(affected_users)
      AND NOT EXISTS (
          SELECT 1
          FROM public.orders o
          JOIN public.order_items oi ON oi.order_id = o.order_id
          JOIN public.products p ON p.product_id = oi.product_id
          WHERE o.tenant_id = p_tenant_id AND o.user_id = a.user_id
            AND CASE a.attribute_slot
                WHEN 'first' THEN p.first_product_attribute
                WHEN 'second' THEN p.second_product_attribute
            END <> ''
      );

    DELETE FROM public.users u
    USING pg_temp.manual_orphan_users a
    WHERE u.tenant_id = p_tenant_id AND u.user_id = a.user_id
      AND NOT EXISTS (SELECT 1 FROM public.campaign_logs h WHERE h.user_id = a.user_id);

    UPDATE public.users u
    SET first_name = NULL, last_name = NULL, gender = NULL,
        phone_number = NULL, semantic_profile = NULL
    FROM pg_temp.manual_orphan_users a
    WHERE u.tenant_id = p_tenant_id AND u.user_id = a.user_id;

    -- A customer-only import keeps the product catalog. A replaced catalog
    -- entry survives as an ID-only row if an order or legacy send needs it.
    DELETE FROM public.product_co_purchase c
    WHERE c.tenant_id = p_tenant_id
      AND EXISTS (
          SELECT 1 FROM pg_temp.manual_upload_products a
          WHERE a.product_id IN (c.base_product_id, c.related_product_id)
      )
      AND NOT EXISTS (
          SELECT 1
          FROM public.order_items base
          JOIN public.order_items related ON related.order_id = base.order_id
          WHERE base.tenant_id = p_tenant_id AND base.product_id = c.base_product_id
            AND related.product_id = c.related_product_id
      );

    DELETE FROM public.products p
    USING pg_temp.manual_upload_products a
    WHERE p.tenant_id = p_tenant_id AND p.product_id = a.product_id
      AND NOT EXISTS (SELECT 1 FROM pg_temp.manual_catalog_survivors s WHERE s.product_id = a.product_id)
      AND NOT EXISTS (
          SELECT 1 FROM pg_temp.manual_customer_survivors s WHERE s.product_id::text = a.product_id
      )
      AND NOT EXISTS (SELECT 1 FROM public.campaign_logs h WHERE h.product_id = a.product_id)
      AND NOT EXISTS (SELECT 1 FROM public.order_items oi WHERE oi.product_id = a.product_id);

    IF p_replace_products THEN
        UPDATE public.products p
        SET name = NULL, category = NULL, price = NULL, price_level = NULL,
            product_link = NULL, first_product_attribute = NULL, second_product_attribute = NULL,
            avg_purchase_interval_days = NULL, semantic_text = NULL
        FROM pg_temp.manual_upload_products a
        WHERE p.tenant_id = p_tenant_id AND p.product_id = a.product_id
          AND NOT EXISTS (
              SELECT 1 FROM pg_temp.manual_catalog_survivors s WHERE s.product_id = a.product_id
          );
    END IF;

    IF affected_users IS NOT NULL THEN
        PERFORM public.sync_attributes_batch_for_users(affected_users);
        PERFORM public.refresh_user_summary_batch_for_users(affected_users);
    END IF;
END;
$$;

REVOKE EXECUTE ON FUNCTION public.retano_replace_manual_upload_data(bigint, boolean, uuid)
FROM PUBLIC;

DO $$
DECLARE api_role text;
BEGIN
    FOR api_role IN SELECT rolname FROM pg_roles WHERE rolname IN ('anon', 'authenticated', 'service_role')
    LOOP
        EXECUTE format(
            'REVOKE EXECUTE ON FUNCTION public.retano_replace_manual_upload_data(bigint, boolean, uuid) FROM %I',
            api_role
        );
    END LOOP;
END;
$$;
