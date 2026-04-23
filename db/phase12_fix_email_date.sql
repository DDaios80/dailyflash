-- Phase 12 — Fix date alignment in the flash-email trigger.
--
-- Context:
--   * Python pipeline runs at 19:30 Athens, writes flash_reports with
--     report_date = TOMORROW (per cron.py default: target = today + 1).
--   * pg_cron maybe_trigger_flash_email() used to fire at 06:00 the next
--     morning — back then, v_today == pipeline-target-day. Date math worked.
--   * After Phase 6 moved the email to 20:00 SAME-DAY as the pipeline,
--     v_today is the upload day (one day BEHIND the pipeline target).
--   * Result: the 20:00 email dispatcher asks send-flash-email for today's
--     flash_reports row, but the pipeline just wrote tomorrow's. Either 404,
--     or stale (yesterday's pipeline having written today's date).
--
-- Fix: dispatch for TOMORROW (v_today + 1), matching the pipeline's target.

create or replace function maybe_trigger_flash_email()
  returns void
  language plpgsql security definer set search_path = cron_private, public
as $$
declare
  v_now_athens   timestamptz;
  v_current_slot text;
  v_target_time  text;
  v_target_date  date;     -- report_date to dispatch (= tomorrow)
  v_url          text;
  v_secret       text;
  v_already_sent boolean;
begin
  v_now_athens := now() at time zone 'Europe/Athens';
  v_current_slot := to_char(
    v_now_athens - (extract(minute from v_now_athens)::int % 5) * interval '1 minute',
    'HH24:MI'
  );

  -- Configured target time
  select value into v_target_time from app_settings where key = 'email_send_time_athens';
  if v_target_time is null then return; end if;
  if v_current_slot <> v_target_time then return; end if;

  -- The 20:00 dispatcher sends TOMORROW's flash (pipeline wrote it at 19:30).
  v_target_date := (v_now_athens::date) + 1;

  -- Idempotency against the target_date (not today)
  select exists(
    select 1 from flash_email_approvals
    where report_date = v_target_date and preview_sent_at is not null
  ) into v_already_sent;
  if v_already_sent then return; end if;

  select value into v_url    from cron_private.secrets where key = 'send_flash_email_url';
  select value into v_secret from cron_private.secrets where key = 'pipeline_secret';
  if v_url is null or v_secret is null then
    raise notice 'cron_private.secrets missing send_flash_email_url or pipeline_secret';
    return;
  end if;

  perform net.http_post(
    url := v_url,
    headers := jsonb_build_object(
      'Content-Type', 'application/json',
      'Authorization', 'Bearer ' || v_secret
    ),
    body := jsonb_build_object('date', v_target_date::text, 'mode', 'preview')
  );
end $$;
