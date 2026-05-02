-- Phase 28.4 — deduplicate reservations + add UNIQUE on resv_name_id.
--
-- Audit found single arrivals duplicated up to 32 times. Cause: each
-- daily Opera xlsx upload re-inserted every reservation in the forward
-- window without dedup'ing against existing rows. Over weeks, the same
-- Opera resv_name_id accumulates 16-32 rows in `reservations`.
--
-- This migration:
--   1. Reports pre-stats
--   2. For each resv_name_id, keeps the row from the most recent upload
--      (by uploads.uploaded_at desc), deletes the rest. Cascades clear
--      stale comment_extractions and alister_findings.
--   3. Adds a partial UNIQUE index on resv_name_id (NULL rows excluded
--      so legacy rows without a Opera id can coexist).
--   4. Reports post-stats.
--
-- Pair with the Lovable-side edge function update for `ingest-flash-report`
-- to UPSERT on resv_name_id going forward.

do $$
declare
  v_before_total int;
  v_before_uniq  int;
  v_null_rows    int;
  v_deleted      int;
  v_after_total  int;
begin
  select count(*), count(distinct resv_name_id), count(*) filter (where resv_name_id is null)
    into v_before_total, v_before_uniq, v_null_rows
    from reservations;
  raise notice 'BEFORE — total rows: %, distinct resv_name_ids: %, NULL resv_name_id rows: %',
    v_before_total, v_before_uniq, v_null_rows;

  -- Keep latest-upload row per resv_name_id; delete the rest.
  with ranked as (
    select r.id,
           row_number() over (
             partition by r.resv_name_id
             order by u.uploaded_at desc nulls last, r.id desc
           ) as rn
    from reservations r
    left join uploads u on u.id = r.upload_id
    where r.resv_name_id is not null
  )
  delete from reservations where id in (select id from ranked where rn > 1);
  get diagnostics v_deleted = row_count;
  raise notice 'DELETED: % duplicate reservation rows (cascades cleared comment_extractions + alister_findings)', v_deleted;

  select count(*) into v_after_total from reservations;
  raise notice 'AFTER — total rows: % (compression ratio %x)',
    v_after_total,
    round((v_before_total::numeric / nullif(v_after_total, 0)), 2);
end $$;

-- Add the constraint AFTER cleanup so the migration succeeds. Partial
-- index excludes NULLs (legacy rows without Opera resv_name_id).
create unique index if not exists reservations_resv_name_id_key
  on reservations (resv_name_id)
  where resv_name_id is not null;
