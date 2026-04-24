-- Phase 20 — Idea reminders + SLA enforcement.
--
-- Enforces the committee's response-time SLAs on /ideas submissions.
-- Every hour a pg_cron job finds stale ideas and posts to the
-- send-idea-reminders edge function, which fires appropriate emails
-- (critical alert, owner nudge, escalation, daily / weekly digest).
--
-- SLAs (wall-clock hours):
--                        ack      triage    respond   escalate
--   critical             4h       24h       24h       36h
--   high                 24h      72h       120h      240h
--   medium               48h      168h      336h      504h
--   low                  168h     336h      720h      1080h

-- ─── Committee chair setting ────────────────────────────────────────────────
-- Seed to d.daios@daioshotels.com. Rotatable via /admin later.
do $$
declare v_user_id uuid;
begin
  select id into v_user_id from auth.users
  where email = 'd.daios@daioshotels.com' limit 1;

  if v_user_id is not null then
    insert into app_settings (key, value)
    values ('committee_chair_user_id', v_user_id::text)
    on conflict (key) do update set value = excluded.value, updated_at = now();
  else
    raise notice 'd.daios@daioshotels.com not found — committee_chair_user_id not seeded';
  end if;
end $$;


-- ─── SLA lookup ─────────────────────────────────────────────────────────────
-- Returns jsonb of hours for a given severity. Single source of truth used
-- by the reminder scheduler, the /ideas UI (chip rendering), and any ad-hoc
-- queries.
create or replace function get_idea_sla_hours(p_severity idea_severity)
  returns jsonb
  language sql immutable
as $$
  select case p_severity
    when 'critical' then jsonb_build_object('ack', 4,   'triage', 24,  'respond', 24,  'escalate', 36)
    when 'high'     then jsonb_build_object('ack', 24,  'triage', 72,  'respond', 120, 'escalate', 240)
    when 'medium'   then jsonb_build_object('ack', 48,  'triage', 168, 'respond', 336, 'escalate', 504)
    when 'low'      then jsonb_build_object('ack', 168, 'triage', 336, 'respond', 720, 'escalate', 1080)
    else                 jsonb_build_object('ack', 48,  'triage', 168, 'respond', 336, 'escalate', 504)
  end;
$$;
grant execute on function get_idea_sla_hours(idea_severity) to authenticated;


-- ─── SLA status for a single idea (used by UI chip + reminder logic) ─────
create or replace function idea_sla_status(p_idea_id uuid)
  returns jsonb
  language plpgsql stable security invoker
as $$
declare
  v_idea record;
  v_sla jsonb;
  v_hours_elapsed numeric;
  v_bucket text;
  v_state  text;
  v_deadline_hours int;
begin
  select i.* into v_idea from ideas i where i.id = p_idea_id;
  if not found then return null; end if;

  -- severity may still be null if AI analysis hasn't landed yet; treat as medium
  v_sla := get_idea_sla_hours(coalesce(v_idea.severity, 'medium'));
  v_hours_elapsed := extract(epoch from (now() - v_idea.created_at)) / 3600.0;

  -- Determine the bucket we're measuring against based on status
  if v_idea.status = 'submitted' then
    v_bucket := 'ack';
    v_deadline_hours := (v_sla->>'ack')::int;
  elsif v_idea.status in ('triaged','in_progress') and v_idea.committee_response is null then
    v_bucket := 'respond';
    v_deadline_hours := (v_sla->>'respond')::int;
  else
    -- resolved / closed / archived: no active SLA
    return jsonb_build_object('state', 'closed', 'bucket', null, 'hours_elapsed', v_hours_elapsed);
  end if;

  -- State calculation: on_track / warning / overdue / escalated
  if v_hours_elapsed >= (v_deadline_hours * 1.5) then
    v_state := 'escalated';
  elsif v_hours_elapsed > v_deadline_hours then
    v_state := 'overdue';
  elsif v_hours_elapsed > (v_deadline_hours * 0.5) then
    v_state := 'warning';
  else
    v_state := 'on_track';
  end if;

  return jsonb_build_object(
    'state', v_state,
    'bucket', v_bucket,
    'hours_elapsed', round(v_hours_elapsed::numeric, 1),
    'deadline_hours', v_deadline_hours,
    'escalation_hours', round((v_deadline_hours * 1.5)::numeric, 1)
  );
end $$;
grant execute on function idea_sla_status(uuid) to authenticated;


-- ─── Find all stale ideas for the reminder scheduler ──────────────────────
-- Returns every open idea with a fresh SLA state, plus the committee chair
-- and owner emails needed by the email templates.
create or replace function find_stale_ideas()
  returns table (
    idea_id          uuid,
    subject          text,
    severity         text,
    status           text,
    created_at       timestamptz,
    committee_response text,
    assigned_to_user_id uuid,
    assigned_to_name text,
    submitter_email  text,
    submitter_name   text,
    sla_state        text,
    sla_bucket       text,
    hours_elapsed    numeric,
    deadline_hours   int
  )
  language plpgsql stable security definer set search_path = public
as $$
begin
  return query
  select
    i.id,
    i.subject,
    coalesce(i.severity::text, 'medium'),
    i.status::text,
    i.created_at,
    i.committee_response,
    i.assigned_to_user_id,
    i.assigned_to_name,
    i.submitter_email,
    i.submitter_name,
    (idea_sla_status(i.id)->>'state')::text,
    (idea_sla_status(i.id)->>'bucket')::text,
    (idea_sla_status(i.id)->>'hours_elapsed')::numeric,
    (idea_sla_status(i.id)->>'deadline_hours')::int
  from ideas i
  where i.status in ('submitted','triaged','in_progress');
end $$;
grant execute on function find_stale_ideas() to authenticated;


-- ─── Idempotency log for reminders ─────────────────────────────────────────
create table if not exists idea_reminder_fires (
  id                uuid primary key default gen_random_uuid(),
  idea_id           uuid references ideas(id) on delete cascade,   -- null for digests
  recipient_user_id uuid references auth.users(id),
  recipient_email   text,
  reminder_type     text not null check (reminder_type in
    ('critical_alert','owner_nudge','chair_nudge','escalation',
     'committee_digest','owner_digest')),
  fire_date         date not null default current_date,
  status            text not null check (status in ('sent','failed','skipped')),
  resend_message_id text,
  error             text,
  created_at        timestamptz not null default now()
);

-- Uniqueness: per-idea reminders fire max once per day per idea per type
create unique index if not exists idea_reminder_fires_per_idea_unique
  on idea_reminder_fires (idea_id, reminder_type, fire_date)
  where idea_id is not null;

-- Uniqueness: digests fire max once per day per recipient per type
create unique index if not exists idea_reminder_fires_digest_unique
  on idea_reminder_fires (recipient_user_id, reminder_type, fire_date)
  where idea_id is null;

create index if not exists idea_reminder_fires_idea_idx
  on idea_reminder_fires (idea_id, created_at desc);

alter table idea_reminder_fires enable row level security;
drop policy if exists read_reminder_fires_admin on idea_reminder_fires;
create policy read_reminder_fires_admin on idea_reminder_fires
  for select using (can_admin());


-- ─── pg_cron trigger function ─────────────────────────────────────────────
-- Polled every hour. Calls the send-idea-reminders edge function which does
-- the actual checks and email sends. Keeps this function minimal so pg_cron
-- just fires and forgets.
create or replace function maybe_trigger_idea_reminders()
  returns void
  language plpgsql security definer set search_path = cron_private, public
as $$
declare
  v_url    text;
  v_secret text;
  v_now_athens timestamptz;
  v_slot text;
begin
  v_now_athens := now() at time zone 'Europe/Athens';
  -- Hour slot in Athens — edge fn uses this to decide whether to send digests
  v_slot := to_char(v_now_athens, 'HH24:MI');

  select value into v_url    from cron_private.secrets where key = 'send_idea_reminders_url';
  select value into v_secret from cron_private.secrets where key = 'pipeline_secret';
  if v_url is null or v_secret is null then
    raise notice 'cron_private.secrets missing send_idea_reminders_url or pipeline_secret';
    return;
  end if;

  perform net.http_post(
    url     := v_url,
    headers := jsonb_build_object(
      'Content-Type',  'application/json',
      'Authorization', 'Bearer ' || v_secret
    ),
    body    := jsonb_build_object(
      'athens_slot', v_slot,
      'athens_date', (v_now_athens::date)::text,
      'athens_dow', to_char(v_now_athens, 'Dy')  -- Mon, Tue, ...
    )
  );
end $$;


-- ─── pg_cron schedule ─────────────────────────────────────────────────────
-- Every hour on the hour. Edge fn gates digest sends to 08:00 Athens internally.
do $$
declare j_id bigint;
begin
  select jobid into j_id from cron.job where jobname = 'idea-reminder-trigger';
  if j_id is not null then perform cron.unschedule(j_id); end if;
  perform cron.schedule(
    'idea-reminder-trigger',
    '0 * * * *',
    'select maybe_trigger_idea_reminders();'
  );
end $$;


-- ─── Post-migration: admin primes the secret ──────────────────────────────
-- insert into cron_private.secrets (key, value) values
--   ('send_idea_reminders_url',
--    'https://wgbghdbfmapuqbfeiygb.supabase.co/functions/v1/send-idea-reminders')
-- on conflict (key) do update set value = excluded.value, updated_at = now();
