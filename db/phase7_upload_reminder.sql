-- Phase 7 — daily upload reminder to the Rooms Division Manager.
--
-- Sends a short email at the admin-configured time (default 19:00 Athens)
-- asking Thelxi to upload the Daily Flash + Birthdays xlsx before the
-- 19:30 Athens pipeline fires.
--
-- Same mechanics as maybe_trigger_flash_email (phase 6): pg_cron every
-- 5 min → SQL function checks Athens time + idempotency → POSTs to a
-- dedicated edge function send-upload-reminder → Resend.

-- ─── Settings: time + recipient (both admin-configurable) ─────────────────
insert into app_settings (key, value) values
  ('upload_reminder_time_athens', '19:00'),
  ('upload_reminder_recipient_email', 'thelxi.smyrnaki@daioshotels.com')
on conflict (key) do nothing;

create or replace function get_upload_reminder_time()
  returns text
  language sql stable security invoker
as $$
  select value from app_settings where key = 'upload_reminder_time_athens';
$$;
grant execute on function get_upload_reminder_time() to authenticated;

create or replace function set_upload_reminder_time(p_time text)
  returns void
  language plpgsql security invoker
as $$
begin
  if not can_admin() then raise exception 'admin only'; end if;
  if p_time !~ '^[0-2][0-9]:[0-5][0-9]$' then
    raise exception 'invalid time format, expected HH:MM';
  end if;
  if (substring(p_time from 4 for 2)::int % 5) <> 0 then
    raise exception 'minutes must be a multiple of 5';
  end if;
  insert into app_settings (key, value, updated_by, updated_at)
  values ('upload_reminder_time_athens', p_time, auth.uid(), now())
  on conflict (key) do update set
    value = excluded.value,
    updated_by = auth.uid(),
    updated_at = now();
end $$;
grant execute on function set_upload_reminder_time(text) to authenticated;

create or replace function get_upload_reminder_recipient()
  returns text
  language sql stable security invoker
as $$
  select value from app_settings where key = 'upload_reminder_recipient_email';
$$;
grant execute on function get_upload_reminder_recipient() to authenticated;

create or replace function set_upload_reminder_recipient(p_email text)
  returns void
  language plpgsql security invoker
as $$
begin
  if not can_admin() then raise exception 'admin only'; end if;
  -- Permissive email sanity — reject obvious junk but don't over-gate
  if p_email !~ '^[^@\s]+@[^@\s]+\.[^@\s]+$' then
    raise exception 'invalid email format';
  end if;
  insert into app_settings (key, value, updated_by, updated_at)
  values ('upload_reminder_recipient_email', p_email, auth.uid(), now())
  on conflict (key) do update set
    value = excluded.value,
    updated_by = auth.uid(),
    updated_at = now();
end $$;
grant execute on function set_upload_reminder_recipient(text) to authenticated;


-- ─── Idempotency log ──────────────────────────────────────────────────────
create table if not exists upload_reminders (
  sent_date date primary key,
  sent_at timestamptz not null default now(),
  recipient text not null,
  resend_message_id text,
  status text not null default 'sent',   -- 'sent' | 'failed'
  error text
);

alter table upload_reminders enable row level security;
drop policy if exists read_upload_reminders_admin on upload_reminders;
create policy read_upload_reminders_admin on upload_reminders
  for select using (can_admin());
-- No write policy — edge function uses service role.


-- ─── Trigger function ─────────────────────────────────────────────────────
create or replace function maybe_trigger_upload_reminder()
  returns void
  language plpgsql security definer set search_path = cron_private, public
as $$
declare
  v_now timestamptz;
  v_slot text;
  v_target text;
  v_today date;
  v_url text;
  v_secret text;
  v_already boolean;
begin
  v_now := now() at time zone 'Europe/Athens';
  v_slot := to_char(
    v_now - (extract(minute from v_now)::int % 5) * interval '1 minute',
    'HH24:MI'
  );

  select value into v_target from app_settings where key = 'upload_reminder_time_athens';
  if v_target is null or v_slot <> v_target then return; end if;

  v_today := v_now::date;
  select exists(select 1 from upload_reminders where sent_date = v_today) into v_already;
  if v_already then return; end if;

  select value into v_url from cron_private.secrets where key = 'send_upload_reminder_url';
  select value into v_secret from cron_private.secrets where key = 'pipeline_secret';
  if v_url is null or v_secret is null then
    raise notice 'cron_private.secrets missing send_upload_reminder_url or pipeline_secret';
    return;
  end if;

  perform net.http_post(
    url := v_url,
    headers := jsonb_build_object(
      'Content-Type', 'application/json',
      'Authorization', 'Bearer ' || v_secret
    ),
    body := jsonb_build_object('date', v_today::text)
  );
end $$;


-- ─── pg_cron schedule ─────────────────────────────────────────────────────
do $$
declare j_id bigint;
begin
  select jobid into j_id from cron.job where jobname = 'upload-reminder-trigger';
  if j_id is not null then perform cron.unschedule(j_id); end if;
  perform cron.schedule(
    'upload-reminder-trigger',
    '*/5 * * * *',
    'select maybe_trigger_upload_reminder();'
  );
end $$;


-- ─── Post-migration: admin must prime the secret ──────────────────────────
-- After applying, run as superuser:
--
--   insert into cron_private.secrets (key, value) values
--     ('send_upload_reminder_url',
--      'https://wgbghdbfmapuqbfeiygb.supabase.co/functions/v1/send-upload-reminder')
--   on conflict (key) do update set value = excluded.value, updated_at = now();
