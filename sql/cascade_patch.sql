-- Patch for tenant_scoped_cascade.sql section 5 (cascade machinery only).
--
-- Fixes two defects in the first version:
--   1. The collector was FOR EACH ROW and ran CREATE TEMP TABLE IF NOT EXISTS
--      on every row, costing a catalog lookup and a NOTICE per row. It is now
--      statement-level with a transition table: one set-based insert per
--      DELETE statement.
--   2. The scratch table was a temp table with ON COMMIT DROP, created inside
--      a trigger. A deferred constraint trigger runs during commit processing,
--      so that table could be dropped before the cascade read it, silently
--      skipping the cascade. It is now a permanent UNLOGGED table keyed by
--      txid_current().
--
-- Safe to re-run. Apply with:
--   docker cp cascade_patch.sql supabase-db:/tmp/
--   docker exec -i supabase-db psql -U postgres -d postgres --     -v ON_ERROR_STOP=1 -f /tmp/cascade_patch.sql

BEGIN;

-- The scratch table is a permanent UNLOGGED table, not a temp one. A temp
-- table created inside a trigger with ON COMMIT DROP can be dropped before the
-- deferred trigger runs during commit processing, which would silently skip
-- the whole cascade. UNLOGGED keeps it cheap (no WAL) while guaranteeing it is
-- visible at commit time. Rows are scoped by xid so concurrent transactions
-- never see each other's ids, and each transaction clears its own rows.
CREATE TABLE IF NOT EXISTS _retano_deleted_identity (
    xid        bigint NOT NULL,
    tenant_id  bigint,
    user_id    text,
    order_id   text,
    product_id text
);

DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM pg_class
        WHERE relname = '_retano_deleted_identity' AND relpersistence <> 'u'
    ) THEN
        ALTER TABLE _retano_deleted_identity SET UNLOGGED;
    END IF;
END;
$$;

CREATE INDEX IF NOT EXISTS _retano_deleted_identity_xid_idx
    ON _retano_deleted_identity (xid);

-- Statement-level: one set-based insert per DELETE statement rather than one
-- per row. Deleting half a million rows costs a single INSERT ... SELECT here.
CREATE OR REPLACE FUNCTION collect_deleted_identity()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF TG_TABLE_NAME = 'users_unnormalized_data' THEN
        INSERT INTO _retano_deleted_identity (xid, tenant_id, user_id, order_id, product_id)
        SELECT DISTINCT txid_current(), tenant_id, user_id::text, order_id::text,
               product_id::text
        FROM deleted_rows;
    ELSE
        INSERT INTO _retano_deleted_identity (xid, tenant_id, user_id, order_id, product_id)
        SELECT DISTINCT txid_current(), tenant_id, NULL, NULL, product_id::text
        FROM deleted_rows;
    END IF;

    RETURN NULL;
END;
$$;

CREATE OR REPLACE FUNCTION cascade_orphaned_identity()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    v_count integer;
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM _retano_deleted_identity WHERE xid = txid_current()
    ) THEN
        RETURN NULL;
    END IF;

    -- A user/order is orphaned when nothing references it any more, in either
    -- the permanent flat table or staging. A product must additionally be
    -- absent from the product-side pair, because customer uploads and product
    -- uploads share one product identity.
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

    -- FK-safe order. users_summary_rfm, users_summary_rfm_segmented and
    -- retention_history are not listed: they already cascade off users.
    DELETE FROM trigger_results tr
    USING _retano_orphans o
    WHERE o.kind = 'user' AND tr.tenant_id = o.tenant_id AND tr.user_id = o.id;
    GET DIAGNOSTICS v_count = ROW_COUNT;
    IF v_count > 0 THEN RAISE NOTICE 'cascade: % trigger_results', v_count; END IF;

    DELETE FROM order_items oi
    USING _retano_orphans o
    WHERE oi.tenant_id = o.tenant_id
      AND ((o.kind = 'order' AND oi.order_id = o.id)
        OR (o.kind = 'product' AND oi.product_id = o.id));
    GET DIAGNOSTICS v_count = ROW_COUNT;
    IF v_count > 0 THEN RAISE NOTICE 'cascade: % order_items', v_count; END IF;

    DELETE FROM orders ord
    USING _retano_orphans o
    WHERE ord.tenant_id = o.tenant_id
      AND ((o.kind = 'order' AND ord.order_id = o.id)
        OR (o.kind = 'user'  AND ord.user_id  = o.id));
    GET DIAGNOSTICS v_count = ROW_COUNT;
    IF v_count > 0 THEN RAISE NOTICE 'cascade: % orders', v_count; END IF;

    DELETE FROM user_attribute_scores uas
    USING _retano_orphans o
    WHERE o.kind = 'user' AND uas.tenant_id = o.tenant_id AND uas.user_id = o.id;
    GET DIAGNOSTICS v_count = ROW_COUNT;
    IF v_count > 0 THEN RAISE NOTICE 'cascade: % user_attribute_scores', v_count; END IF;

    DELETE FROM user_summary us
    USING _retano_orphans o
    WHERE o.kind = 'user' AND us.tenant_id = o.tenant_id AND us.user_id = o.id;
    GET DIAGNOSTICS v_count = ROW_COUNT;
    IF v_count > 0 THEN RAISE NOTICE 'cascade: % user_summary', v_count; END IF;

    DELETE FROM product_co_purchase pcp
    USING _retano_orphans o
    WHERE o.kind = 'product' AND pcp.tenant_id = o.tenant_id
      AND (pcp.base_product_id = o.id OR pcp.related_product_id = o.id);
    GET DIAGNOSTICS v_count = ROW_COUNT;
    IF v_count > 0 THEN RAISE NOTICE 'cascade: % product_co_purchase', v_count; END IF;

    DELETE FROM products p
    USING _retano_orphans o
    WHERE o.kind = 'product' AND p.tenant_id = o.tenant_id AND p.product_id = o.id;
    GET DIAGNOSTICS v_count = ROW_COUNT;
    IF v_count > 0 THEN RAISE NOTICE 'cascade: % products', v_count; END IF;

    -- Cascades to users_summary_rfm, users_summary_rfm_segmented,
    -- retention_history via their existing ON DELETE CASCADE.
    DELETE FROM users u
    USING _retano_orphans o
    WHERE o.kind = 'user' AND u.tenant_id = o.tenant_id AND u.user_id = o.id;
    GET DIAGNOSTICS v_count = ROW_COUNT;
    IF v_count > 0 THEN RAISE NOTICE 'cascade: % users', v_count; END IF;

    -- The identity registries keep their rows: an id that was allocated must
    -- never be reissued to a different internal id, even after its business
    -- data is gone.

    DELETE FROM _retano_deleted_identity WHERE xid = txid_current();
    DROP TABLE IF EXISTS _retano_orphans;
    RETURN NULL;
END;
$$;

DROP TRIGGER IF EXISTS trg_collect_deleted_identity ON users_unnormalized_data;
CREATE TRIGGER trg_collect_deleted_identity
AFTER DELETE ON users_unnormalized_data
REFERENCING OLD TABLE AS deleted_rows
FOR EACH STATEMENT EXECUTE FUNCTION collect_deleted_identity();

DROP TRIGGER IF EXISTS trg_collect_deleted_identity ON products_unnormalized_data;
CREATE TRIGGER trg_collect_deleted_identity
AFTER DELETE ON products_unnormalized_data
REFERENCING OLD TABLE AS deleted_rows
FOR EACH STATEMENT EXECUTE FUNCTION collect_deleted_identity();

-- Constraint triggers are FOR EACH ROW only, so the cascade body would run
-- once per deleted row. The xid guard makes every call after the first a
-- no-op: the first one clears this transaction's collected rows, so the rest
-- return immediately at the EXISTS check.
DROP TRIGGER IF EXISTS trg_cascade_orphaned_identity ON users_unnormalized_data;
CREATE CONSTRAINT TRIGGER trg_cascade_orphaned_identity
AFTER DELETE ON users_unnormalized_data
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW EXECUTE FUNCTION cascade_orphaned_identity();

DROP TRIGGER IF EXISTS trg_cascade_orphaned_identity ON products_unnormalized_data;
CREATE CONSTRAINT TRIGGER trg_cascade_orphaned_identity
AFTER DELETE ON products_unnormalized_data
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW EXECUTE FUNCTION cascade_orphaned_identity();

COMMIT;
