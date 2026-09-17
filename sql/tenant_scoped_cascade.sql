-- Tenant scoping + orphan cascade for Retano's normalized/derived tables.
--
-- Two things happen here:
--   1. Every derived table gains a tenant_id column, backfilled from the
--      identity it hangs off (users.tenant_id, or products.tenant_id for the
--      product-side tables), then constrained NOT NULL + FK + indexed.
--   2. Deleting rows from users_unnormalized_data / products_unnormalized_data
--      now cascades to every table that derives from them, removing only the
--      user_ids / order_ids / product_ids that became genuinely orphaned.
--
-- Deployment:
--   1. Stop/pause Celery upload + sync workers.
--   2. Apply this script in the Supabase SQL Editor (or via a migration).
--   3. Restart workers.
--
-- Global-pool statistics (buying power, RFM percentiles) are deliberately left
-- pooled across all tenants; adding tenant_id does not change how they score.

SET LOCAL statement_timeout = '0';
SELECT pg_advisory_xact_lock(hashtext('retano-tenant-cascade-v1'));

-- ---------------------------------------------------------------------------
-- 1. tenant_id columns
-- ---------------------------------------------------------------------------

ALTER TABLE products              ADD COLUMN IF NOT EXISTS tenant_id bigint;
ALTER TABLE orders                ADD COLUMN IF NOT EXISTS tenant_id bigint;
ALTER TABLE order_items           ADD COLUMN IF NOT EXISTS tenant_id bigint;
ALTER TABLE product_co_purchase   ADD COLUMN IF NOT EXISTS tenant_id bigint;
ALTER TABLE user_summary          ADD COLUMN IF NOT EXISTS tenant_id bigint;
ALTER TABLE user_attribute_scores ADD COLUMN IF NOT EXISTS tenant_id bigint;
ALTER TABLE users_summary_rfm     ADD COLUMN IF NOT EXISTS tenant_id bigint;
ALTER TABLE users_summary_rfm_segmented
                                  ADD COLUMN IF NOT EXISTS tenant_id bigint;
ALTER TABLE retention_history     ADD COLUMN IF NOT EXISTS tenant_id bigint;

-- ---------------------------------------------------------------------------
-- 2. Backfill
--
-- products has no user to hang off, so its tenant comes from the identity
-- registry that minted the product_id in the first place. Everything else
-- resolves through users.tenant_id.
-- ---------------------------------------------------------------------------

UPDATE products p
SET tenant_id = i.tenant_id
FROM global_product_identity i
WHERE p.tenant_id IS NULL
  AND i.product_id::text = p.product_id;

UPDATE orders o
SET tenant_id = u.tenant_id
FROM users u
WHERE o.tenant_id IS NULL
  AND u.user_id = o.user_id;

UPDATE order_items oi
SET tenant_id = o.tenant_id
FROM orders o
WHERE oi.tenant_id IS NULL
  AND o.order_id = oi.order_id;

UPDATE product_co_purchase pcp
SET tenant_id = p.tenant_id
FROM products p
WHERE pcp.tenant_id IS NULL
  AND p.product_id = pcp.base_product_id;

UPDATE user_summary us
SET tenant_id = u.tenant_id
FROM users u
WHERE us.tenant_id IS NULL
  AND u.user_id = us.user_id;

UPDATE user_attribute_scores uas
SET tenant_id = u.tenant_id
FROM users u
WHERE uas.tenant_id IS NULL
  AND u.user_id = uas.user_id;

UPDATE users_summary_rfm r
SET tenant_id = u.tenant_id
FROM users u
WHERE r.tenant_id IS NULL
  AND u.user_id = r.user_id;

UPDATE users_summary_rfm_segmented s
SET tenant_id = u.tenant_id
FROM users u
WHERE s.tenant_id IS NULL
  AND u.user_id = s.user_id;

UPDATE retention_history rh
SET tenant_id = u.tenant_id
FROM users u
WHERE rh.tenant_id IS NULL
  AND u.user_id = rh.user_id;

-- Rows whose parent identity has already vanished cannot be attributed to a
-- tenant and would block the NOT NULL below. They are unreachable orphans of
-- exactly the kind this script exists to prevent, so drop them.
DO $$
DECLARE
    v_dropped integer;
BEGIN
    DELETE FROM product_co_purchase   WHERE tenant_id IS NULL;
    GET DIAGNOSTICS v_dropped = ROW_COUNT;
    RAISE NOTICE 'backfill: dropped % unattributable product_co_purchase rows', v_dropped;

    DELETE FROM order_items           WHERE tenant_id IS NULL;
    GET DIAGNOSTICS v_dropped = ROW_COUNT;
    RAISE NOTICE 'backfill: dropped % unattributable order_items rows', v_dropped;

    DELETE FROM orders                WHERE tenant_id IS NULL;
    GET DIAGNOSTICS v_dropped = ROW_COUNT;
    RAISE NOTICE 'backfill: dropped % unattributable orders rows', v_dropped;

    DELETE FROM user_attribute_scores WHERE tenant_id IS NULL;
    GET DIAGNOSTICS v_dropped = ROW_COUNT;
    RAISE NOTICE 'backfill: dropped % unattributable user_attribute_scores rows', v_dropped;

    DELETE FROM user_summary          WHERE tenant_id IS NULL;
    GET DIAGNOSTICS v_dropped = ROW_COUNT;
    RAISE NOTICE 'backfill: dropped % unattributable user_summary rows', v_dropped;

    DELETE FROM users_summary_rfm            WHERE tenant_id IS NULL;
    DELETE FROM users_summary_rfm_segmented  WHERE tenant_id IS NULL;
    DELETE FROM retention_history            WHERE tenant_id IS NULL;
    DELETE FROM products                     WHERE tenant_id IS NULL;
END;
$$;

-- ---------------------------------------------------------------------------
-- 3. Constraints + indexes
-- ---------------------------------------------------------------------------

ALTER TABLE products                    ALTER COLUMN tenant_id SET NOT NULL;
ALTER TABLE orders                      ALTER COLUMN tenant_id SET NOT NULL;
ALTER TABLE order_items                 ALTER COLUMN tenant_id SET NOT NULL;
ALTER TABLE product_co_purchase         ALTER COLUMN tenant_id SET NOT NULL;
ALTER TABLE user_summary                ALTER COLUMN tenant_id SET NOT NULL;
ALTER TABLE user_attribute_scores       ALTER COLUMN tenant_id SET NOT NULL;
ALTER TABLE users_summary_rfm           ALTER COLUMN tenant_id SET NOT NULL;
ALTER TABLE users_summary_rfm_segmented ALTER COLUMN tenant_id SET NOT NULL;
ALTER TABLE retention_history           ALTER COLUMN tenant_id SET NOT NULL;

DO $$
DECLARE
    t text;
BEGIN
    FOREACH t IN ARRAY ARRAY[
        'products', 'orders', 'order_items', 'product_co_purchase',
        'user_summary', 'user_attribute_scores', 'users_summary_rfm',
        'users_summary_rfm_segmented', 'retention_history'
    ] LOOP
        IF NOT EXISTS (
            SELECT 1 FROM pg_constraint
            WHERE conname = t || '_tenant_id_fkey'
        ) THEN
            EXECUTE format(
                'ALTER TABLE %I ADD CONSTRAINT %I
                 FOREIGN KEY (tenant_id) REFERENCES core_tenant(id)
                 ON DELETE CASCADE',
                t, t || '_tenant_id_fkey'
            );
        END IF;
        EXECUTE format(
            'CREATE INDEX IF NOT EXISTS %I ON %I (tenant_id)',
            t || '_tenant_id_idx', t
        );
    END LOOP;
END;
$$;

-- ---------------------------------------------------------------------------
-- 4. Keep tenant_id correct on new rows
--
-- The flush functions insert into these tables without a tenant_id. Rather
-- than rewrite every INSERT, derive it on write from the identity registries,
-- which are the authority for which tenant owns an id.
-- ---------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION fill_tenant_from_user_id()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF NEW.tenant_id IS NULL THEN
        SELECT tenant_id INTO NEW.tenant_id
        FROM users WHERE user_id = NEW.user_id;
    END IF;
    RETURN NEW;
END;
$$;

CREATE OR REPLACE FUNCTION fill_tenant_from_product_id()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF NEW.tenant_id IS NULL THEN
        SELECT tenant_id INTO NEW.tenant_id
        FROM global_product_identity
        WHERE product_id::text = NEW.product_id;
    END IF;
    RETURN NEW;
END;
$$;

CREATE OR REPLACE FUNCTION fill_tenant_from_order_id()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF NEW.tenant_id IS NULL THEN
        SELECT tenant_id INTO NEW.tenant_id
        FROM orders WHERE order_id = NEW.order_id;
    END IF;
    RETURN NEW;
END;
$$;

CREATE OR REPLACE FUNCTION fill_tenant_from_base_product()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF NEW.tenant_id IS NULL THEN
        SELECT tenant_id INTO NEW.tenant_id
        FROM products WHERE product_id = NEW.base_product_id;
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_fill_tenant ON orders;
CREATE TRIGGER trg_fill_tenant BEFORE INSERT OR UPDATE OF user_id ON orders
FOR EACH ROW EXECUTE FUNCTION fill_tenant_from_user_id();

DROP TRIGGER IF EXISTS trg_fill_tenant ON user_summary;
CREATE TRIGGER trg_fill_tenant BEFORE INSERT ON user_summary
FOR EACH ROW EXECUTE FUNCTION fill_tenant_from_user_id();

DROP TRIGGER IF EXISTS trg_fill_tenant ON user_attribute_scores;
CREATE TRIGGER trg_fill_tenant BEFORE INSERT ON user_attribute_scores
FOR EACH ROW EXECUTE FUNCTION fill_tenant_from_user_id();

DROP TRIGGER IF EXISTS trg_fill_tenant ON users_summary_rfm;
CREATE TRIGGER trg_fill_tenant BEFORE INSERT ON users_summary_rfm
FOR EACH ROW EXECUTE FUNCTION fill_tenant_from_user_id();

DROP TRIGGER IF EXISTS trg_fill_tenant ON users_summary_rfm_segmented;
CREATE TRIGGER trg_fill_tenant BEFORE INSERT ON users_summary_rfm_segmented
FOR EACH ROW EXECUTE FUNCTION fill_tenant_from_user_id();

DROP TRIGGER IF EXISTS trg_fill_tenant ON retention_history;
CREATE TRIGGER trg_fill_tenant BEFORE INSERT ON retention_history
FOR EACH ROW EXECUTE FUNCTION fill_tenant_from_user_id();

DROP TRIGGER IF EXISTS trg_fill_tenant ON products;
CREATE TRIGGER trg_fill_tenant BEFORE INSERT ON products
FOR EACH ROW EXECUTE FUNCTION fill_tenant_from_product_id();

DROP TRIGGER IF EXISTS trg_fill_tenant ON order_items;
CREATE TRIGGER trg_fill_tenant BEFORE INSERT ON order_items
FOR EACH ROW EXECUTE FUNCTION fill_tenant_from_order_id();

DROP TRIGGER IF EXISTS trg_fill_tenant ON product_co_purchase;
CREATE TRIGGER trg_fill_tenant BEFORE INSERT ON product_co_purchase
FOR EACH ROW EXECUTE FUNCTION fill_tenant_from_base_product();

-- ---------------------------------------------------------------------------
-- 5. Orphan cascade
--
-- The automated sync path (ingest_user_rows / ingest_product_rows) implements
-- an UPDATE as delete-from-permanent then restage, with the restage happening
-- after the delete statement completes. A statement-level AFTER DELETE trigger
-- would see those ids as orphaned mid-transaction and destroy derived data for
-- what is only a field update.
--
-- So the cascade is a DEFERRABLE INITIALLY DEFERRED constraint trigger: it
-- records touched ids as rows are deleted, and resolves orphanhood once at
-- COMMIT, by which point any restaged rows are visible in *_staging.
-- ---------------------------------------------------------------------------

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

-- ---------------------------------------------------------------------------
-- 6. Views carry tenant_id
--
-- The percentile maths below stay pooled across all tenants by design: the
-- IQR fence and the r/m cutoffs are computed over every tenant's users, as
-- they were before. tenant_id is carried through for filtering only.
-- ---------------------------------------------------------------------------

DROP MATERIALIZED VIEW IF EXISTS the_users_summary_rfm CASCADE;

CREATE MATERIALIZED VIEW the_users_summary_rfm AS
WITH base AS (
    SELECT us.user_id, u.tenant_id, us.recency_days, us.frequency, us.monetary
    FROM user_summary us
    JOIN users u ON u.user_id = us.user_id
    WHERE us.recency_days IS NOT NULL
      AND us.frequency IS NOT NULL
      AND us.monetary IS NOT NULL
),
orders_agg AS (
    SELECT user_id, CURRENT_DATE - min(order_date) AS customer_age_days
    FROM orders
    GROUP BY user_id
),
iqr_bounds AS (
    SELECT
        percentile_cont(0.25) WITHIN GROUP (ORDER BY recency_days::double precision) AS r_q1,
        percentile_cont(0.75) WITHIN GROUP (ORDER BY recency_days::double precision) AS r_q3,
        percentile_cont(0.25) WITHIN GROUP (ORDER BY monetary::double precision)     AS m_q1,
        percentile_cont(0.75) WITHIN GROUP (ORDER BY monetary::double precision)     AS m_q3
    FROM base
),
cleaned AS (
    SELECT b.user_id, b.recency_days, b.monetary
    FROM base b
    CROSS JOIN iqr_bounds q
    WHERE b.recency_days::double precision >= (q.r_q1 - 1.5 * (q.r_q3 - q.r_q1))
      AND b.recency_days::double precision <= (q.r_q3 + 1.5 * (q.r_q3 - q.r_q1))
      AND b.monetary::double precision     >= (q.m_q1 - 1.5 * (q.m_q3 - q.m_q1))
      AND b.monetary::double precision     <= (q.m_q3 + 1.5 * (q.m_q3 - q.m_q1))
),
rfm_percentiles AS (
    SELECT
        percentile_cont(0.20) WITHIN GROUP (ORDER BY recency_days::double precision) AS r_p20,
        percentile_cont(0.40) WITHIN GROUP (ORDER BY recency_days::double precision) AS r_p40,
        percentile_cont(0.60) WITHIN GROUP (ORDER BY recency_days::double precision) AS r_p60,
        percentile_cont(0.80) WITHIN GROUP (ORDER BY recency_days::double precision) AS r_p80,
        percentile_cont(0.20) WITHIN GROUP (ORDER BY monetary::double precision)     AS m_p20,
        percentile_cont(0.40) WITHIN GROUP (ORDER BY monetary::double precision)     AS m_p40,
        percentile_cont(0.60) WITHIN GROUP (ORDER BY monetary::double precision)     AS m_p60,
        percentile_cont(0.80) WITHIN GROUP (ORDER BY monetary::double precision)     AS m_p80
    FROM cleaned
),
scored AS (
    SELECT
        b.user_id,
        b.tenant_id,
        b.recency_days,
        b.frequency,
        b.monetary,
        COALESCE(o.customer_age_days, 9999) AS customer_age_days,
        CASE
            WHEN b.recency_days::double precision <= p.r_p20 THEN 5
            WHEN b.recency_days::double precision <= p.r_p40 THEN 4
            WHEN b.recency_days::double precision <= p.r_p60 THEN 3
            WHEN b.recency_days::double precision <= p.r_p80 THEN 2
            ELSE 1
        END AS r_score,
        CASE
            WHEN b.frequency = 1 THEN 1
            WHEN b.frequency BETWEEN 2 AND 3  THEN 2
            WHEN b.frequency BETWEEN 4 AND 7  THEN 3
            WHEN b.frequency BETWEEN 8 AND 15 THEN 4
            ELSE 5
        END AS f_score,
        CASE
            WHEN b.monetary::double precision <= p.m_p20 THEN 1
            WHEN b.monetary::double precision <= p.m_p40 THEN 2
            WHEN b.monetary::double precision <= p.m_p60 THEN 3
            WHEN b.monetary::double precision <= p.m_p80 THEN 4
            ELSE 5
        END AS m_score
    FROM base b
    LEFT JOIN orders_agg o ON o.user_id = b.user_id
    CROSS JOIN rfm_percentiles p
)
SELECT user_id, tenant_id, r_score, f_score, m_score,
       recency_days, frequency, monetary, customer_age_days
FROM scored;

-- REFRESH ... CONCURRENTLY requires a unique index.
CREATE UNIQUE INDEX IF NOT EXISTS the_users_summary_rfm_user_idx
    ON the_users_summary_rfm (user_id);
CREATE INDEX IF NOT EXISTS the_users_summary_rfm_tenant_idx
    ON the_users_summary_rfm (tenant_id);

CREATE OR REPLACE VIEW the_users_summary_rfm_segmented AS
SELECT
    user_id,
    tenant_id,
    r_score,
    f_score,
    m_score,
    recency_days,
    frequency,
    monetary,
    customer_age_days,
    CASE
        WHEN customer_age_days <= 30 THEN 'new'
        WHEN recency_days >= 180     THEN 'churned'
        WHEN recency_days >= 90      THEN 'at_risk'
        WHEN r_score >= 4 AND f_score >= 4 AND m_score >= 4 THEN 'vip'
        ELSE 'active'
    END AS user_segment
FROM the_users_summary_rfm;

-- ---------------------------------------------------------------------------
-- 7. Functions that write to the newly-scoped tables
--
-- Only the INSERTs need to carry tenant_id (the BEFORE INSERT triggers above
-- would fill it, but writing it explicitly avoids a per-row lookup). UPDATE
-- statements that join through user_id are already implicitly tenant-correct,
-- since a user_id belongs to exactly one tenant.
-- ---------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION refresh_rfm_scores()
RETURNS void
LANGUAGE plpgsql
AS $$
BEGIN
    REFRESH MATERIALIZED VIEW CONCURRENTLY the_users_summary_rfm;

    INSERT INTO users_summary_rfm (user_id, tenant_id, r_score, f_score, m_score)
    SELECT user_id, tenant_id, r_score, f_score, m_score
    FROM the_users_summary_rfm
    ON CONFLICT (user_id) DO UPDATE
    SET r_score   = EXCLUDED.r_score,
        f_score   = EXCLUDED.f_score,
        m_score   = EXCLUDED.m_score,
        tenant_id = EXCLUDED.tenant_id;

    INSERT INTO users_summary_rfm_segmented (
        user_id, tenant_id, r_score, f_score, m_score, user_segment
    )
    SELECT user_id, tenant_id, r_score, f_score, m_score, user_segment
    FROM the_users_summary_rfm_segmented
    ON CONFLICT (user_id) DO UPDATE
    SET r_score      = EXCLUDED.r_score,
        f_score      = EXCLUDED.f_score,
        m_score      = EXCLUDED.m_score,
        user_segment = EXCLUDED.user_segment,
        tenant_id    = EXCLUDED.tenant_id;

    UPDATE user_summary u
    SET rfm_segment = s.user_segment
    FROM users_summary_rfm_segmented s
    WHERE u.user_id = s.user_id;
END;
$$;

CREATE OR REPLACE FUNCTION refresh_user_summary_rfm_metrics()
RETURNS void
LANGUAGE plpgsql
AS $$
BEGIN
    INSERT INTO user_summary (
        user_id, tenant_id, recency_days, frequency, monetary, updated_at
    )
    SELECT
        o.user_id,
        o.tenant_id,
        CURRENT_DATE - max(o.order_date),
        count(o.order_id),
        COALESCE(sum(o.total_amount), 0),
        now()
    FROM orders o
    GROUP BY o.user_id, o.tenant_id
    ON CONFLICT (user_id) DO UPDATE
    SET recency_days = EXCLUDED.recency_days,
        frequency    = EXCLUDED.frequency,
        monetary     = EXCLUDED.monetary,
        updated_at   = now();
END;
$$;

CREATE OR REPLACE FUNCTION refresh_buying_power()
RETURNS void
LANGUAGE plpgsql
AS $$
BEGIN
    -- Pooled across all tenants, unchanged from the original: one global
    -- average is the benchmark every user is measured against.
    WITH user_avg_orders AS (
        SELECT user_id, avg(total_amount) AS user_avg_total
        FROM orders
        GROUP BY user_id
    ),
    overall_avg AS (
        SELECT avg(user_avg_total) AS overall_avg_total FROM user_avg_orders
    ),
    buying_power_calc AS (
        SELECT
            u.user_id,
            CASE
                WHEN ((u.user_avg_total - o.overall_avg_total) / o.overall_avg_total) * 100 > 61  THEN 'Very High'
                WHEN ((u.user_avg_total - o.overall_avg_total) / o.overall_avg_total) * 100 > 20  THEN 'High'
                WHEN ((u.user_avg_total - o.overall_avg_total) / o.overall_avg_total) * 100 >= -20 THEN 'Medium'
                WHEN ((u.user_avg_total - o.overall_avg_total) / o.overall_avg_total) * 100 >= -60 THEN 'Low'
                ELSE 'Very Low'
            END AS buying_power_level
        FROM user_avg_orders u
        CROSS JOIN overall_avg o
    )
    UPDATE user_summary us
    SET buying_power = bp.buying_power_level
    FROM buying_power_calc bp
    WHERE us.user_id = bp.user_id;

    UPDATE user_summary us
    SET buying_power = NULL
    WHERE NOT EXISTS (SELECT 1 FROM orders o WHERE o.user_id = us.user_id);
END;
$$;

-- process_campaign_eligibility: the 'اولین خرید' priority-resolution branch
-- counts a user's orders without scoping them to the campaign's tenant. A
-- user_id belongs to exactly one tenant, so this is not a cross-tenant leak
-- and the function is left alone rather than re-declared in full here. Now
-- that orders carries tenant_id, that subquery can be tightened to
--     FROM orders o2 WHERE o2.user_id = af.user_id AND o2.tenant_id = v_campaign.tenant_id
-- whenever the function is next edited, which lets it use orders_tenant_id_idx
-- instead of scanning by user_id alone.

-- ---------------------------------------------------------------------------
-- 8. Verification
-- ---------------------------------------------------------------------------

DO $$
DECLARE
    v_bad integer;
BEGIN
    SELECT count(*) INTO v_bad FROM orders o
    JOIN users u ON u.user_id = o.user_id
    WHERE o.tenant_id IS DISTINCT FROM u.tenant_id;
    IF v_bad > 0 THEN
        RAISE EXCEPTION 'orders.tenant_id disagrees with users.tenant_id on % rows', v_bad;
    END IF;

    SELECT count(*) INTO v_bad FROM order_items oi
    JOIN orders o ON o.order_id = oi.order_id
    WHERE oi.tenant_id IS DISTINCT FROM o.tenant_id;
    IF v_bad > 0 THEN
        RAISE EXCEPTION 'order_items.tenant_id disagrees with orders.tenant_id on % rows', v_bad;
    END IF;

    SELECT count(*) INTO v_bad FROM product_co_purchase pcp
    JOIN products p ON p.product_id = pcp.base_product_id
    WHERE pcp.tenant_id IS DISTINCT FROM p.tenant_id;
    IF v_bad > 0 THEN
        RAISE EXCEPTION 'product_co_purchase.tenant_id disagrees with products.tenant_id on % rows', v_bad;
    END IF;

    RAISE NOTICE 'tenant_id backfill verified across orders, order_items, product_co_purchase';
END;
$$;
