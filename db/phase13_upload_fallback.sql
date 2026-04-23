-- Phase 13 — upload fallback email to Kyrillos Michailides.
--
-- If Thelxi hasn't uploaded today's Daily Flash file by 19:30 Athens, a
-- fallback email fires at 19:40 to Kyrillos Michailides with detailed
-- instructions on how to extract the Opera report and upload it to
-- OneDrive. This ensures the nightly pipeline has data to process.
--
-- Detection logic: check the `uploads` table at 19:40 Athens. If no row
-- exists with today's date pattern in the filename (DD.MM.YYYY /
-- YYYY-MM-DD / DD-MM-YYYY) AND uploaded after today 18:00 Athens, assume
-- Thelxi missed the deadline and fire the fallback.
--
-- Same mechanics as phases 6 and 7: pg_cron every 5 min → SQL function
-- checks Athens time + idempotency + upload presence → POSTs to
-- send-upload-fallback edge function → Resend.

-- ─── Settings: time + recipient (admin-configurable) ───────────────────────
insert into app_settings (key, value) values
  ('upload_fallback_time_athens', '19:40'),
  ('upload_fallback_recipient_email', 'kyrillos.michailides@daioshotels.com'),
  ('upload_fallback_instructions_text',
   $$Thelxi has not uploaded today's Daily Flash file to OneDrive by the 19:30 deadline. Please extract and upload it now so tonight's flash email fan-out can happen on schedule.

HOW TO EXTRACT THE DAILY FLASH FROM OPERA PMS:

1. Log into Opera PMS on the on-premise workstation.
2. Open Reports → Daily Reports (or the equivalent path your team uses for the Daily Flash).
3. Set the report date to today.
4. Run the report and export to Excel (File → Export, or the Excel icon).
5. Save the file locally with the filename:
      Daily Flash DD.MM.YYYY.xlsx
   (substituting today's date, e.g. "Daily Flash 23.04.2026.xlsx")

WHERE TO UPLOAD:

Upload the .xlsx to the shared OneDrive folder:
   OneDrive for Business → Hellas Holiday Hotels SA → DailyFlash

If a Birthdays file is also expected today, upload it to the
   DailyFlash/Birthdays subfolder with the same date convention.

DEADLINE:
   Please upload within the next 30 minutes so the 20:00 Athens approval
   email can still be dispatched tonight.

If you hit any blocker, contact Thelxi Smyrnaki or Dimitris Daios (d.daios@daioshotels.com) immediately.$$)
on conflict (key) do nothing;


-- ─── Getters / setters (admin-editable via the settings UI) ────────────────
create or replace function get_upload_fallback_time()
  returns text language sql stable security invoker as $$
  select value from app_settings where key = 'upload_fallback_time_athens';
$$;
grant execute on function get_upload_fallback_time() to authenticated;

create or replace function set_upload_fallback_time(p_time text)
  returns void language plpgsql security invoker as $$
begin
  if not can_admin() then raise exception 'admin only'; end if;
  if p_time !~ '^[0-2][0-9]:[0-5][0-9]$' then
    raise exception 'invalid time format, expected HH:MM';
  end if;
  if (substring(p_time from 4 for 2)::int % 5) <> 0 then
    raise exception 'minutes must be a multiple of 5';
  end if;
  insert into app_settings (key, value, updated_by, updated_at)
  values ('upload_fallback_time_athens', p_time, auth.uid(), now())
  on conflict (key) do update set
    value = excluded.value, updated_by = auth.uid(), updated_at = now();
end $$;
grant execute on function set_upload_fallback_time(text) to authenticated;

create or replace function get_upload_fallback_recipient()
  returns text language sql stable security invoker as $$
  select value from app_settings where key = 'upload_fallback_recipient_email';
$$;
grant execute on function get_upload_fallback_recipient() to authenticated;

create or replace function set_upload_fallback_recipient(p_email text)
  returns void language plpgsql security invoker as $$
begin
  if not can_admin() then raise exception 'admin only'; end if;
  if p_email !~ '^[^@\s]+@[^@\s]+\.[^@\s]+$' then
    raise exception 'invalid email format';
  end if;
  insert into app_settings (key, value, updated_by, updated_at)
  values ('upload_fallback_recipient_email', p_email, auth.uid(), now())
  on conflict (key) do update set
    value = excluded.value, updated_by = auth.uid(), updated_at = now();
end $$;
grant execute on function set_upload_fallback_recipient(text) to authenticated;

create or replace function set_upload_fallback_instructions(p_text text)
  returns void language plpgsql security invoker as $$
begin
  if not can_admin() then raise exception 'admin only'; end if;
  if length(p_text) < 50 or length(p_text) > 10000 then
    raise exception 'instructions must be 50-10000 characters';
  end if;
  insert into app_settings (key, value, updated_by, updated_at)
  values ('upload_fallback_instructions_text', p_text, auth.uid(), now())
  on conflict (key) do update set
    value = excluded.value, updated_by = auth.uid(), updated_at = now();
end $$;
grant execute on function set_upload_fallback_instructions(text) to authenticated;


-- ─── Idempotency log ───────────────────────────────────────────────────────
create table if not exists upload_fallback_fires (
  sent_date date primary key,
  sent_at timestamptz not null default now(),
  recipient text not null,
  resend_message_id text,
  status text not null default 'sent',   -- 'sent' | 'failed' | 'skipped'
  skip_reason text,
  error text
);

alter table upload_fallback_fires enable row level security;
drop policy if exists read_upload_fallback_fires_admin on upload_fallback_fires;
create policy read_upload_fallback_fires_admin on upload_fallback_fires
  for select using (can_admin());


-- ─── Trigger function ──────────────────────────────────────────────────────
-- Polled every 5 min by pg_cron. At the configured slot, checks whether
-- today's Opera file was uploaded. If not, fires the fallback to Kyrillos.
create or replace function maybe_trigger_upload_fallback()
  returns void
  language plpgsql security definer set search_path = cron_private, public
as $$
declare
  v_now_athens   timestamptz;
  v_slot         text;
  v_target_time  text;
  v_today        date;
  v_eighteen     timestamptz;
  v_uploaded     boolean;
  v_url          text;
  v_secret       text;
  v_already      boolean;
begin
  v_now_athens := now() at time zone 'Europe/Athens';
  v_slot := to_char(
    v_now_athens - (extract(minute from v_now_athens)::int % 5) * interval '1 minute',
    'HH24:MI'
  );

  select value into v_target_time from app_settings where key = 'upload_fallback_time_athens';
  if v_target_time is null or v_slot <> v_target_time then return; end if;

  v_today := v_now_athens::date;

  -- Idempotency: only fire once per day (success OR skipped)
  select exists(select 1 from upload_fallback_fires where sent_date = v_today) into v_already;
  if v_already then return; end if;

  -- Window: today 18:00 Athens onwards (= any upload made this evening).
  -- Convert Athens-local 18:00 to timestamptz for comparison with uploaded_at.
  v_eighteen := (v_today::timestamp + interval '18 hours') at time zone 'Europe/Athens';

  -- Was today's Opera file already uploaded this evening?
  select exists(
    select 1 from uploads
    where uploaded_at >= v_eighteen
      and (
        filename ~ to_char(v_today, 'DD\.MM\.YYYY')
        or filename ~ to_char(v_today, 'YYYY-MM-DD')
        or filename ~ to_char(v_today, 'DD-MM-YYYY')
      )
  ) into v_uploaded;

  if v_uploaded then
    -- Log the skip so we can see tomorrow that the flow was healthy
    insert into upload_fallback_fires (sent_date, recipient, status, skip_reason)
    values (v_today, '(skipped)', 'skipped', 'today''s Opera file already uploaded')
    on conflict (sent_date) do nothing;
    return;
  end if;

  select value into v_url    from cron_private.secrets where key = 'send_upload_fallback_url';
  select value into v_secret from cron_private.secrets where key = 'pipeline_secret';
  if v_url is null or v_secret is null then
    raise notice 'cron_private.secrets missing send_upload_fallback_url or pipeline_secret';
    return;
  end if;

  perform net.http_post(
    url     := v_url,
    headers := jsonb_build_object(
      'Content-Type',  'application/json',
      'Authorization', 'Bearer ' || v_secret
    ),
    body    := jsonb_build_object('date', v_today::text)
  );

  -- Edge function will upsert the 'sent' status + message_id row.
end $$;


-- ─── pg_cron schedule ──────────────────────────────────────────────────────
do $$
declare j_id bigint;
begin
  select jobid into j_id from cron.job where jobname = 'upload-fallback-trigger';
  if j_id is not null then perform cron.unschedule(j_id); end if;
  perform cron.schedule(
    'upload-fallback-trigger',
    '*/5 * * * *',
    'select maybe_trigger_upload_fallback();'
  );
end $$;


-- ─── Post-migration: admin primes 1 secret ─────────────────────────────────
-- insert into cron_private.secrets (key, value) values
--   ('send_upload_fallback_url',
--    'https://wgbghdbfmapuqbfeiygb.supabase.co/functions/v1/send-upload-fallback')
-- on conflict (key) do update set value = excluded.value, updated_at = now();
