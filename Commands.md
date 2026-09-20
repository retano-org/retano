c:\Projects\retanobi\myvenv\Scripts\activate.bat

git merge -X theirs develop

python manage.py spectacular --file schema.yaml --validate
## Durable global-ID upload migration (0055)

This deployment changes large staging/permanent ID columns to `bigint`, adds
persistent identity registries, and replaces tenant-wide upload finalization
with job-scoped functions. Schedule a maintenance window and stop upload
traffic and workers before applying it. Take a Supabase backup/snapshot first.

```powershell
docker compose build web
docker compose stop web worker
docker compose run --rm --no-deps web python manage.py migrate core 0055
```

The migration runs `sql/global_identity_upload_pipeline.sql`. Do not run that
SQL file separately if migration 0055 has already completed.

If the VPS cannot run Django migrations against Supabase, the exact fallback
is to paste the contents of `sql/global_identity_upload_pipeline.sql` between
`BEGIN;` and `COMMIT;` in Supabase SQL Editor, then record only the matching
Django migration state:

```powershell
docker compose run --rm web python manage.py migrate core 0055 --fake
```

Use either the normal migration or this SQL-Editor-plus-`--fake` fallback,
never both.

### One-time cleanup for the known failed tenant-9 upload

Migration 0055 deliberately preserves legacy staging rows. The failed job
`25d1209d-e6d0-4af6-b84a-e5b3d8c38205` has no source object left to retry and
its 486,592 pre-migration rows have no `upload_job_id`. After taking a backup,
run this separately in Supabase SQL Editor. The count assertion makes the
operation abort if the staging state has changed.

```sql
BEGIN;

DO $$
DECLARE
    v_rows bigint;
BEGIN
    SELECT count(*) INTO v_rows
    FROM users_unnormalized_data_staging
    WHERE tenant_id = 9
      AND upload_job_id IS NULL
      AND user_id IS NULL
      AND order_id IS NULL
      AND product_id IS NULL;

    IF v_rows <> 486592 THEN
        RAISE EXCEPTION
            'Cleanup aborted: expected 486592 rows, found %', v_rows;
    END IF;

    DELETE FROM users_unnormalized_data_staging
    WHERE tenant_id = 9
      AND upload_job_id IS NULL
      AND user_id IS NULL
      AND order_id IS NULL
      AND product_id IS NULL;
END;
$$;

COMMIT;
```

Then recreate the application containers from the newly built image and submit
a new upload. Do not reuse the failed job:

```powershell
docker compose up -d --force-recreate web worker
```







docker exec -d supabase-db bash -c "psql -U postgres -d postgres > /tmp/del9.log 2>&1 <<'SQL'
\timing on
BEGIN;
DELETE FROM public.users_unnormalized_data WHERE tenant_id = 9;
SELECT cascade_orphaned_identity_now();
COMMIT;
SQL"




docker exec -i supabase-db psql -U postgres -d postgres <<'SQL'
\timing on
BEGIN;
DELETE FROM public.products_unnormalized_data WHERE tenant_id = 9;
SELECT cascade_orphaned_identity_now();
COMMIT;
SQL




docker exec -i supabase-db psql -U postgres -d postgres -c \
"VACUUM (ANALYZE) users, orders, order_items, user_summary, user_attribute_scores, users_unnormalized_data, _retano_deleted_identity;"





docker exec -i supabase-db psql -U postgres -d postgres <<'SQL'
SELECT cron.schedule('refresh_user_summary_rfm_metrics', '0 2 * * *',
                     $$SELECT refresh_user_summary_rfm_metrics();$$);
SELECT cron.schedule('refresh_buying_power', '20 2 * * *',
                     $$SELECT refresh_buying_power();$$);
SELECT cron.schedule('refresh_rfm_scores', '40 2 * * *',
                     $$SELECT refresh_rfm_scores();$$);
SQL






docker exec -d supabase-db bash -c "psql -U postgres -d postgres > /tmp/rfm.log 2>&1 <<'SQL'
\timing on
SELECT update_user_top_products();
SQL"




docker exec -i supabase-db psql -U postgres -d postgres -c \
"SELECT pid, now()-xact_start AS age, wait_event_type, state, left(query,60) FROM pg_stat_activity WHERE state<>'idle' AND pid<>pg_backend_pid() ORDER BY xact_start;"



curl -s -X POST http://localhost:8000/functions/v1/send-campaign-sms \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer REDACTED" \
  -d '{"tenant_id": 9}'