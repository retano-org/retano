-- Fix for the tenant cascade: replace the per-row deferred constraint trigger
-- with an explicit, once-per-transaction function call.
--
-- What was wrong
-- --------------
-- The cascade was a DEFERRABLE INITIALLY DEFERRED constraint trigger. Those
-- are FOR EACH ROW only, so deleting 486k rows queued 486k deferred events and
-- fired every one at COMMIT. The first did the real work and cleared the
-- collected ids; each of the remaining ~486k still ran its guard query against
-- a table that had just had 486k rows deleted from it -- rows not yet
-- vacuumable, because the transaction was still open. That is ~486k scans over
-- ~486k dead tuples, and it ran for over a day without finishing.
--
-- The cascade body itself was never the problem: it did 8 rows in 3.9s.
--
-- What this does
-- --------------
-- Drops both constraint triggers. The cascade is now cascade_orphaned_identity_now(),
-- called explicitly after a delete, in the same transaction:
--
--     BEGIN;
--     DELETE FROM users_unnormalized_data WHERE tenant_id = 9;
--     SELECT cascade_orphaned_identity_now();
--     COMMIT;
--
-- Running it in the same transaction preserves the property the deferral was
-- there for: the automated sync path (ingest_user_rows) deletes from the
-- permanent table and restages later in the same transaction, so an id that is
-- merely being updated is still visible in *_staging and is not treated as
-- orphaned. A cascade that ran mid-statement would get that wrong.
--
-- The collector triggers are kept as they are: statement-level, one set-based
-- insert per DELETE, cheap.
--
-- Apply with:
--   docker cp cascade_fix.sql supabase-db:/tmp/
--   docker exec -i supabase-db psql -U postgres -d postgres \
--     -v ON_ERROR_STOP=1 -f /tmp/cascade_fix.sql

BEGIN;

DROP TRIGGER IF EXISTS trg_cascade_orphaned_identity ON users_unnormalized_data;
DROP TRIGGER IF EXISTS trg_cascade_orphaned_identity ON products_unnormalized_data;
DROP FUNCTION IF EXISTS cascade_orphaned_identity();

-- Indexes matching what the orphan checks actually compare. The checks cast
-- the bigint id columns to text, which makes a plain index on the column
-- unusable; these are expression indexes on the cast, so the NOT EXISTS
-- probes become index lookups instead of sequential scans.
CREATE INDEX IF NOT EXISTS uud_tenant_user_text_idx
    ON users_unnormalized_data (tenant_id, (user_id::text));
CREATE INDEX IF NOT EXISTS uud_tenant_order_text_idx
    ON users_unnormalized_data (tenant_id, (order_id::text));
CREATE INDEX IF NOT EXISTS uud_tenant_product_text_idx
    ON users_unnormalized_data (tenant_id, (product_id::text));

CREATE INDEX IF NOT EXISTS uuds_tenant_user_text_idx
    ON users_unnormalized_data_staging (tenant_id, (user_id::text));
CREATE INDEX IF NOT EXISTS uuds_tenant_order_text_idx
    ON users_unnormalized_data_staging (tenant_id, (order_id::text));
CREATE INDEX IF NOT EXISTS uuds_tenant_product_text_idx
    ON users_unnormalized_data_staging (tenant_id, (product_id::text));

CREATE INDEX IF NOT EXISTS pud_tenant_product_text_idx
    ON products_unnormalized_data (tenant_id, (product_id::text));
CREATE INDEX IF NOT EXISTS puds_tenant_product_text_idx
    ON products_unnormalized_data_staging (tenant_id, (product_id::text));

CREATE OR REPLACE FUNCTION cascade_orphaned_identity_now()
RETURNS void
LANGUAGE plpgsql
AS $$
DECLARE
    v_count integer;
    v_total integer := 0;
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM _retano_deleted_identity WHERE xid = txid_current()
    ) THEN
        RAISE NOTICE 'cascade: nothing collected in this transaction, skipping';
        RETURN;
    END IF;

    -- A user or order is orphaned when nothing references it any more, in
    -- either the permanent flat table or staging. A product must additionally
    -- be absent from the product-side pair, because customer uploads and
    -- product uploads share one product identity.
    CREATE TEMP TABLE _retano_orphans ON COMMIT DROP AS
    WITH touched AS (
        SELECT DISTINCT tenant_id, user_id, order_id, product_id
        FROM _retano_deleted_identity
        WHERE xid = txid_current()
    ),
    orphan_users AS (
        SELECT DISTINCT t.tenant_id, t.user_id
        FROM touched t
        WHERE t.user_id IS NOT NULL
          AND NOT EXISTS (
              SELECT 1 FROM users_unnormalized_data u
              WHERE u.tenant_id = t.tenant_id AND u.user_id::text = t.user_id
          )
          AND NOT EXISTS (
              SELECT 1 FROM users_unnormalized_data_staging s
              WHERE s.tenant_id = t.tenant_id AND s.user_id::text = t.user_id
          )
    ),
    orphan_orders AS (
        SELECT DISTINCT t.tenant_id, t.order_id
        FROM touched t
        WHERE t.order_id IS NOT NULL
          AND NOT EXISTS (
              SELECT 1 FROM users_unnormalized_data u
              WHERE u.tenant_id = t.tenant_id AND u.order_id::text = t.order_id
          )
          AND NOT EXISTS (
              SELECT 1 FROM users_unnormalized_data_staging s
              WHERE s.tenant_id = t.tenant_id AND s.order_id::text = t.order_id
          )
    ),
    orphan_products AS (
        SELECT DISTINCT t.tenant_id, t.product_id
        FROM touched t
        WHERE t.product_id IS NOT NULL
          AND NOT EXISTS (
              SELECT 1 FROM users_unnormalized_data u
              WHERE u.tenant_id = t.tenant_id AND u.product_id::text = t.product_id
          )
          AND NOT EXISTS (
              SELECT 1 FROM users_unnormalized_data_staging s
              WHERE s.tenant_id = t.tenant_id AND s.product_id::text = t.product_id
          )
          AND NOT EXISTS (
              SELECT 1 FROM products_unnormalized_data p
              WHERE p.tenant_id = t.tenant_id AND p.product_id::text = t.product_id
          )
          AND NOT EXISTS (
              SELECT 1 FROM products_unnormalized_data_staging ps
              WHERE ps.tenant_id = t.tenant_id AND ps.product_id::text = t.product_id
          )
    )
    SELECT 'user' AS kind, tenant_id, user_id AS id FROM orphan_users
    UNION ALL
    SELECT 'order', tenant_id, order_id FROM orphan_orders
    UNION ALL
    SELECT 'product', tenant_id, product_id FROM orphan_products;

    CREATE INDEX ON _retano_orphans (kind, tenant_id, id);
    ANALYZE _retano_orphans;

    SELECT count(*) INTO v_count FROM _retano_orphans;
    RAISE NOTICE 'cascade: % orphaned identities resolved', v_count;

    -- FK-safe order. users_summary_rfm, users_summary_rfm_segmented and
    -- retention_history are absent deliberately: they already cascade off users.
    DELETE FROM trigger_results tr
    USING _retano_orphans o
    WHERE o.kind = 'user' AND tr.tenant_id = o.tenant_id AND tr.user_id = o.id;
    GET DIAGNOSTICS v_count = ROW_COUNT; v_total := v_total + v_count;
    IF v_count > 0 THEN RAISE NOTICE 'cascade: % trigger_results', v_count; END IF;

    DELETE FROM order_items oi
    USING _retano_orphans o
    WHERE oi.tenant_id = o.tenant_id
      AND ((o.kind = 'order' AND oi.order_id = o.id)
        OR (o.kind = 'product' AND oi.product_id = o.id));
    GET DIAGNOSTICS v_count = ROW_COUNT; v_total := v_total + v_count;
    IF v_count > 0 THEN RAISE NOTICE 'cascade: % order_items', v_count; END IF;

    DELETE FROM orders ord
    USING _retano_orphans o
    WHERE ord.tenant_id = o.tenant_id
      AND ((o.kind = 'order' AND ord.order_id = o.id)
        OR (o.kind = 'user'  AND ord.user_id  = o.id));
    GET DIAGNOSTICS v_count = ROW_COUNT; v_total := v_total + v_count;
    IF v_count > 0 THEN RAISE NOTICE 'cascade: % orders', v_count; END IF;

    DELETE FROM user_attribute_scores uas
    USING _retano_orphans o
    WHERE o.kind = 'user' AND uas.tenant_id = o.tenant_id AND uas.user_id = o.id;
    GET DIAGNOSTICS v_count = ROW_COUNT; v_total := v_total + v_count;
    IF v_count > 0 THEN RAISE NOTICE 'cascade: % user_attribute_scores', v_count; END IF;

    DELETE FROM user_summary us
    USING _retano_orphans o
    WHERE o.kind = 'user' AND us.tenant_id = o.tenant_id AND us.user_id = o.id;
    GET DIAGNOSTICS v_count = ROW_COUNT; v_total := v_total + v_count;
    IF v_count > 0 THEN RAISE NOTICE 'cascade: % user_summary', v_count; END IF;

    DELETE FROM product_co_purchase pcp
    USING _retano_orphans o
    WHERE o.kind = 'product' AND pcp.tenant_id = o.tenant_id
      AND (pcp.base_product_id = o.id OR pcp.related_product_id = o.id);
    GET DIAGNOSTICS v_count = ROW_COUNT; v_total := v_total + v_count;
    IF v_count > 0 THEN RAISE NOTICE 'cascade: % product_co_purchase', v_count; END IF;

    DELETE FROM products p
    USING _retano_orphans o
    WHERE o.kind = 'product' AND p.tenant_id = o.tenant_id AND p.product_id = o.id;
    GET DIAGNOSTICS v_count = ROW_COUNT; v_total := v_total + v_count;
    IF v_count > 0 THEN RAISE NOTICE 'cascade: % products', v_count; END IF;

    -- Cascades to users_summary_rfm, users_summary_rfm_segmented and
    -- retention_history through their existing ON DELETE CASCADE.
    DELETE FROM users u
    USING _retano_orphans o
    WHERE o.kind = 'user' AND u.tenant_id = o.tenant_id AND u.user_id = o.id;
    GET DIAGNOSTICS v_count = ROW_COUNT; v_total := v_total + v_count;
    IF v_count > 0 THEN RAISE NOTICE 'cascade: % users', v_count; END IF;

    -- The identity registries keep their rows on purpose: an id that has been
    -- allocated must never be reissued to a different internal id later.

    DELETE FROM _retano_deleted_identity WHERE xid = txid_current();
    RAISE NOTICE 'cascade: % rows deleted in total', v_total;
END;
$$;

COMMIT;
