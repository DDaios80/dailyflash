-- Phase 23a — Reissue lock-out hotfix.
--
-- Problem (diagnosed 2026-05-14 ~22:30 Athens):
--   The Supabase edge function `reissue-flash` never reliably writes
--   pipeline_finished_at to flash_reissue_log. Evidence:
--     - Every recorded row has finished_at = NEXT row's started_at
--       (e.g. bf2be97c sat in 'running' for 12 days until 14 May)
--     - The cron pipeline takes 10-17 min; the edge function polls for
--       only 90s; Supabase kills edge functions at 150s wall-clock.
--   Result: every reissue leaves a stuck 'running' row. The preview RPC
--   blocks the NEXT reissue for up to 10 min via has_running_reissue.
--   Thelxi is locked out for 10 min after every click.
--
-- This hotfix:
--   - Shrinks BOTH windows from 10 min -> 2 min:
--       (a) supersede window inside flash_reissue_log_start
--       (b) has_running window inside flash_reissue_preview
--   - Worst-case lock-out becomes 2 min instead of 10 min.
--   - No architectural change. Stuck rows still appear; they just
--     clear themselves faster.
--
-- Follow-up (proper fix, planned for tomorrow):
--   Have src/webhook.py write pipeline_finished_at directly from the
--   Python webhook once the cron pipeline actually finishes. Single
--   source of truth. Edge function returns immediately after kickoff.
--   See docs/phase23b-reissue-architectural-fix.md.


-- 1. Tighter supersede window inside the START helper.
create or replace function flash_reissue_log_start(
  p_report_date date,
  p_diff jsonb
) returns uuid
  language plpgsql security definer set search_path = public
as $$
declare
  v_id uuid;
begin
  if not can_admin() then
    raise exception 'admin only' using errcode = '42501';
  end if;

  -- Supersede stale-running rows older than 2 minutes.
  -- The cron pipeline takes 10-17 min so the edge function ALWAYS
  -- times out before writing pipeline_finished_at; only this UPDATE
  -- ever closes them. Shorter window = shorter user lock-out.
  update flash_reissue_log
    set status = 'superseded',
        pipeline_finished_at = now()
  where status = 'running' and triggered_at < now() - interval '2 minutes';

  insert into flash_reissue_log
    (triggered_by, report_date, status, diff_preview, pipeline_started_at)
  values
    (auth.uid(), p_report_date, 'running', p_diff, now())
  returning id into v_id;

  return v_id;
end $$;
grant execute on function flash_reissue_log_start(date, jsonb) to authenticated;


-- 2. Tighter has_running window inside PREVIEW.
-- Also sweeps stale rows as a side effect so the UI flag clears even
-- when no one has clicked reissue recently (was VOLATILE-unsafe under
-- STABLE; we flip the function to VOLATILE which is correct anyway
-- since it can now write).
create or replace function flash_reissue_preview()
  returns jsonb
  language plpgsql volatile security definer set search_path = public
as $$
declare
  v_report_date date;
  v_payload_computed_at timestamptz;
  v_site_inspections_new int;
  v_fam_trips_new int;
  v_has_running boolean;
begin
  if not can_admin() then
    raise exception 'admin only' using errcode = '42501';
  end if;

  -- Phase 23a sweep — any 'running' row older than 2 min is stuck
  -- (edge function reliability bug). Close it before reading the flag.
  update flash_reissue_log
    set status = 'superseded',
        pipeline_finished_at = now()
  where status = 'running' and triggered_at < now() - interval '2 minutes';

  v_report_date := ((now() at time zone 'Europe/Athens')::date) + 1;

  select greatest(coalesce(updated_at, created_at), created_at)
  into v_payload_computed_at
  from flash_reports where report_date = v_report_date
  limit 1;

  select count(*) into v_site_inspections_new
  from site_inspections
  where status = 'approved'
    and coalesce(updated_at, created_at) > coalesce(v_payload_computed_at, 'epoch'::timestamptz);

  select count(*) into v_fam_trips_new
  from fam_trips
  where status = 'approved'
    and coalesce(updated_at, created_at) > coalesce(v_payload_computed_at, 'epoch'::timestamptz)
    and start_date <= v_report_date
    and end_date   >= v_report_date;

  -- Tighter window: 2 min (matches the supersede above).
  select exists(
    select 1 from flash_reissue_log
    where status = 'running'
      and triggered_at > now() - interval '2 minutes'
  ) into v_has_running;

  return jsonb_build_object(
    'report_date', v_report_date,
    'payload_computed_at', v_payload_computed_at,
    'site_inspections_new', v_site_inspections_new,
    'fam_trips_new', v_fam_trips_new,
    'has_running_reissue', v_has_running,
    'can_reissue_now', can_reissue_tonight()
  );
end $$;
grant execute on function flash_reissue_preview() to authenticated;


-- 3. One-time cleanup — close any currently-stuck rows so Thelxi's
-- next click works immediately without waiting 2 min for the sweep.
update flash_reissue_log
  set status = 'superseded',
      pipeline_finished_at = now()
where status = 'running'
  and pipeline_finished_at is null;
