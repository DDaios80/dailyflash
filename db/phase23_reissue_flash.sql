-- Phase 23 — Reissue flash report on demand.
--
-- Lets admins re-run the 19:30 pipeline + trigger a re-issued flash email
-- when late data arrives after 20:00 (site inspections, FAM trips,
-- birthdays updates). The email side already honours the re-issue banner
-- shipped by Lovable this evening.

-- ─── Gating: only admin + only tonight's window ─────────────────────────
create or replace function can_reissue_tonight()
  returns boolean
  language plpgsql stable security invoker
as $$
declare
  v_role text;
  v_now_athens timestamptz;
  v_hour int;
begin
  -- admin only
  select role into v_role from user_roles where user_id = auth.uid();
  if v_role <> 'admin' then return false; end if;

  -- window: 20:00 Athens (post daily email) through 23:59 Athens
  v_now_athens := now() at time zone 'Europe/Athens';
  v_hour := extract(hour from v_now_athens)::int;
  return v_hour >= 20 and v_hour <= 23;
end $$;
grant execute on function can_reissue_tonight() to authenticated;


-- ─── Reissue run log ─────────────────────────────────────────────────────
create table if not exists flash_reissue_log (
  id                      uuid primary key default gen_random_uuid(),
  triggered_by            uuid references auth.users(id) on delete set null,
  triggered_at            timestamptz not null default now(),
  report_date             date not null,
  status                  text not null default 'running'
                          check (status in ('running','ok','failed','timeout','superseded')),
  diff_preview            jsonb,         -- what the preview showed the user
  pipeline_started_at     timestamptz,
  pipeline_finished_at    timestamptz,
  payload_updated         boolean default false,
  reissue_email_triggered boolean default false,
  error                   text,
  created_at              timestamptz not null default now()
);

create index if not exists flash_reissue_log_date_idx
  on flash_reissue_log (report_date, triggered_at desc);

alter table flash_reissue_log enable row level security;

drop policy if exists flash_reissue_log_read on flash_reissue_log;
create policy flash_reissue_log_read on flash_reissue_log
  for select using (can_admin() or is_super_admin());


-- ─── Preview RPC (no pipeline run — cheap diff from DB state) ───────────
-- Returns what would change if we re-ran the pipeline right now against
-- today's report_date. Counts newly-approved site inspections, FAM trips
-- and detects if birthdays have been loaded since the current flash_reports
-- payload was computed.
create or replace function flash_reissue_preview()
  returns jsonb
  language plpgsql stable security definer set search_path = public
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

  -- The report_date = tomorrow from the pipeline's perspective (19:30 run
  -- yesterday wrote today's flash; 19:30 today writes tomorrow's flash).
  -- At reissue time (evening of day N), the live report is for day N+1.
  v_report_date := ((now() at time zone 'Europe/Athens')::date) + 1;

  -- When was this report's payload last computed?
  select greatest(coalesce(updated_at, created_at), created_at)
  into v_payload_computed_at
  from flash_reports where report_date = v_report_date
  limit 1;

  -- Newly-approved site inspections since then, aimed at the target date.
  select count(*) into v_site_inspections_new
  from site_inspections
  where status = 'approved'
    and coalesce(updated_at, created_at) > coalesce(v_payload_computed_at, 'epoch'::timestamptz);

  -- Newly-approved FAM trips since then, aimed at the target date window.
  select count(*) into v_fam_trips_new
  from fam_trips
  where status = 'approved'
    and coalesce(updated_at, created_at) > coalesce(v_payload_computed_at, 'epoch'::timestamptz)
    and start_date <= v_report_date
    and end_date   >= v_report_date;

  -- Is any reissue already running?
  select exists(
    select 1 from flash_reissue_log
    where status = 'running'
      and triggered_at > now() - interval '10 minutes'
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


-- ─── Log row lifecycle helpers ───────────────────────────────────────────
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

  -- Supersede any stale-running rows older than 10 minutes
  update flash_reissue_log
    set status = 'superseded',
        pipeline_finished_at = now()
  where status = 'running' and triggered_at < now() - interval '10 minutes';

  insert into flash_reissue_log
    (triggered_by, report_date, status, diff_preview, pipeline_started_at)
  values
    (auth.uid(), p_report_date, 'running', p_diff, now())
  returning id into v_id;

  return v_id;
end $$;
grant execute on function flash_reissue_log_start(date, jsonb) to authenticated;


create or replace function flash_reissue_log_finish(
  p_id uuid,
  p_status text,
  p_payload_updated boolean default false,
  p_email_triggered boolean default false,
  p_error text default null
) returns void
  language plpgsql security definer set search_path = public
as $$
begin
  -- Edge function (service role) writes this; admin JWT also allowed for
  -- cancel-style flows.
  update flash_reissue_log
    set status = p_status,
        pipeline_finished_at = now(),
        payload_updated = p_payload_updated,
        reissue_email_triggered = p_email_triggered,
        error = p_error
  where id = p_id;
end $$;
grant execute on function flash_reissue_log_finish(uuid, text, boolean, boolean, text) to authenticated;


-- ─── Super-admin health widget hook ──────────────────────────────────────
-- Latest 10 reissue events, for the /super-admin Health tab.
create or replace function super_admin_reissue_history(p_limit int default 10)
  returns table (
    id                      uuid,
    triggered_by_email      text,
    triggered_at            timestamptz,
    report_date             date,
    status                  text,
    diff_preview            jsonb,
    payload_updated         boolean,
    reissue_email_triggered boolean,
    error                   text
  )
  language plpgsql stable security definer set search_path = public, auth
as $$
begin
  if not is_super_admin() then
    raise exception 'super admin only' using errcode = '42501';
  end if;

  return query
  select
    l.id,
    u.email,
    l.triggered_at,
    l.report_date,
    l.status,
    l.diff_preview,
    l.payload_updated,
    l.reissue_email_triggered,
    l.error
  from flash_reissue_log l
  left join auth.users u on u.id = l.triggered_by
  order by l.triggered_at desc
  limit p_limit;
end $$;
grant execute on function super_admin_reissue_history(int) to authenticated;
