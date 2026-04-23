-- Phase 6 — admin-configurable email send time.
--
-- Admin picks the daily email send time from the dashboard. pg_cron polls
-- every 5 minutes; when the current Athens time (rounded to 5-min)
-- matches the configured time AND today's fanout hasn't been triggered
-- yet, it fires send-flash-email.
--
-- Seed default: 20:00 Athens.
-- Granularity: 5 minutes.
-- Idempotency: guarded by flash_email_approvals.preview_sent_at = null for today.

-- ─── app_settings table ─────────────────────────────────────────────────────
create table if not exists app_settings (
  key text primary key,
  value text not null,
  updated_at timestamptz not null default now(),
  updated_by uuid
);

alter table app_settings enable row level security;

drop policy if exists read_app_settings on app_settings;
create policy read_app_settings on app_settings
  for select using (auth.role() = 'authenticated');

drop policy if exists write_app_settings_admin on app_settings;
create policy write_app_settings_admin on app_settings
  for all using (can_admin()) with check (can_admin());

-- Seed default
insert into app_settings (key, value) values ('email_send_time_athens', '20:00')
on conflict (key) do nothing;


-- ─── Admin RPCs ────────────────────────────────────────────────────────────
create or replace function get_email_send_time()
  returns text
  language sql stable security invoker
as $$
  select value from app_settings where key = 'email_send_time_athens';
$$;
grant execute on function get_email_send_time() to authenticated;

create or replace function set_email_send_time(p_time text)
  returns void
  language plpgsql security invoker
as $$
begin
  if not can_admin() then raise exception 'admin only'; end if;
  if p_time !~ '^[0-2][0-9]:[0-5][0-9]$' then
    raise exception 'invalid time format, expected HH:MM (e.g. 20:00)';
  end if;
  -- Snap to 5-min granularity
  if (substring(p_time from 4 for 2)::int % 5) <> 0 then
    raise exception 'minutes must be a multiple of 5 (cron polls every 5 min)';
  end if;
  insert into app_settings (key, value, updated_by, updated_at)
  values ('email_send_time_athens', p_time, auth.uid(), now())
  on conflict (key) do update set
    value = excluded.value,
    updated_by = auth.uid(),
    updated_at = now();
end $$;
grant execute on function set_email_send_time(text) to authenticated;


-- ─── Private schema for cron secrets ──────────────────────────────────────
-- Not exposed via PostgREST. Only callable by the trigger function running
-- security definer. Admins set the secret via the set_cron_secret RPC.
create schema if not exists cron_private;
revoke all on schema cron_private from public, anon, authenticated;

create table if not exists cron_private.secrets (
  key text primary key,
  value text not null,
  updated_at timestamptz not null default now()
);

create or replace function set_cron_secret(p_key text, p_value text)
  returns void
  language plpgsql security definer set search_path = cron_private, public
as $$
begin
  if not can_admin() then raise exception 'admin only'; end if;
  insert into cron_private.secrets (key, value)
  values (p_key, p_value)
  on conflict (key) do update set value = excluded.value, updated_at = now();
end $$;
grant execute on function set_cron_secret(text, text) to authenticated;


-- ─── Extensions ───────────────────────────────────────────────────────────
create extension if not exists pg_cron;
create extension if not exists pg_net;


-- ─── Trigger function ──────────────────────────────────────────────────────
-- Polled every 5 min by pg_cron. Checks if now (Athens, 5-min snapped)
-- matches the configured send time and, if so, POSTs to send-flash-email.
create or replace function maybe_trigger_flash_email()
  returns void
  language plpgsql security definer set search_path = cron_private, public
as $$
declare
  v_now_athens timestamptz;
  v_current_slot text;
  v_target text;
  v_today date;
  v_url text;
  v_secret text;
  v_already_sent boolean;
begin
  v_now_athens := now() at time zone 'Europe/Athens';
  -- Round minutes down to nearest 5-min block
  v_current_slot := to_char(
    v_now_athens - (extract(minute from v_now_athens)::int % 5) * interval '1 minute',
    'HH24:MI'
  );
  -- Configured target
  select value into v_target from app_settings where key = 'email_send_time_athens';
  if v_target is null then return; end if;
  if v_current_slot <> v_target then return; end if;

  -- Check idempotency — has the preview already been sent for today?
  v_today := v_now_athens::date;
  select exists(
    select 1 from flash_email_approvals
    where report_date = v_today and preview_sent_at is not null
  ) into v_already_sent;
  if v_already_sent then return; end if;

  -- Read the URL + secret
  select value into v_url from cron_private.secrets where key = 'send_flash_email_url';
  select value into v_secret from cron_private.secrets where key = 'pipeline_secret';
  if v_url is null or v_secret is null then
    raise notice 'cron_private.secrets missing send_flash_email_url or pipeline_secret';
    return;
  end if;

  -- Fire the request (fire-and-forget; net.http_post returns a request_id)
  perform net.http_post(
    url := v_url,
    headers := jsonb_build_object(
      'Content-Type', 'application/json',
      'Authorization', 'Bearer ' || v_secret
    ),
    body := jsonb_build_object('date', v_today::text, 'mode', 'preview')
  );
end $$;


-- ─── pg_cron schedule ──────────────────────────────────────────────────────
-- Safe to re-run: unschedule existing then schedule fresh.
do $$
declare j_id bigint;
begin
  select jobid into j_id from cron.job where jobname = 'flash-email-trigger';
  if j_id is not null then perform cron.unschedule(j_id); end if;
  perform cron.schedule(
    'flash-email-trigger',
    '*/5 * * * *',
    'select maybe_trigger_flash_email();'
  );
end $$;


-- ─── One-time secret setup (admin runs manually after migration) ──────────
-- After applying this migration, an admin must set the two secrets:
--
--   select set_cron_secret(
--     'send_flash_email_url',
--     'https://wgbghdbfmapuqbfeiygb.supabase.co/functions/v1/send-flash-email'
--   );
--   select set_cron_secret(
--     'pipeline_secret',
--     '<paste PIPELINE_SECRET value here>'
--   );
--
-- Until both secrets are set, the trigger function will log a NOTICE and
-- skip sending — no errors, no emails.
