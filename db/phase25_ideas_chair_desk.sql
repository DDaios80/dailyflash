-- Phase 25 — Ideas & Opinions UX redesign.
--
-- Builds on Phase 11 (ideas table) + Phase 20 (SLA reminders) + Phase 21
-- (rotating ExCom chair). Adds:
--   * Extended idea_status enum (acknowledged / in_discussion / for_monday /
--     decided_at_monday)
--   * Committee discussion thread + @mention table
--   * True anonymity (super-admin-only reveal with audit log)
--   * Monday agenda auto-generation 3 days before each ExCom meeting
--   * Chair Desk RPC returning 5 sections in one round-trip
--   * Quick-action RPCs (acknowledge / solvable_now / need_committee /
--     to_monday / decide_at_monday)
--   * Submitter CSAT capture
--
-- Scope: data layer + RPCs only. UI prompt handed to Lovable separately.

-- ─── 1. Status enum extensions ─────────────────────────────────────────────
alter type idea_status add value if not exists 'acknowledged';
alter type idea_status add value if not exists 'in_discussion';
alter type idea_status add value if not exists 'for_monday';
alter type idea_status add value if not exists 'decided_at_monday';


-- ─── 2. Extra columns on ideas ─────────────────────────────────────────────
alter table ideas
  add column if not exists acknowledged_at        timestamptz,
  add column if not exists acknowledged_by        uuid references auth.users(id),
  add column if not exists for_monday_at          timestamptz,
  add column if not exists for_monday_reason      text,
  add column if not exists monday_meeting_date    date,
  add column if not exists monday_decision_outcome text
    check (monday_decision_outcome in ('decided','tabled','action_assigned') or monday_decision_outcome is null),
  add column if not exists monday_decision_notes  text,
  add column if not exists action_assignee_user_id uuid references auth.users(id),
  add column if not exists csat_rating            int check (csat_rating between 1 and 5),
  add column if not exists csat_comment           text,
  add column if not exists csat_at                timestamptz;

create index if not exists ideas_for_monday_idx
  on ideas (monday_meeting_date) where status = 'for_monday';

create index if not exists ideas_acknowledged_idx
  on ideas (acknowledged_at desc) where acknowledged_at is not null;


-- ─── 3. Committee discussion thread ────────────────────────────────────────
create table if not exists idea_comments (
  id                  uuid primary key default gen_random_uuid(),
  idea_id             uuid not null references ideas(id) on delete cascade,
  author_user_id      uuid references auth.users(id),
  author_name         text,
  author_role         text,
  body                text not null check (length(body) between 1 and 5000),
  mentioned_user_ids  uuid[] not null default '{}',
  created_at          timestamptz not null default now()
);

create index if not exists idea_comments_idea_idx on idea_comments (idea_id, created_at);
create index if not exists idea_comments_mentioned_idx
  on idea_comments using gin (mentioned_user_ids);

alter table idea_comments enable row level security;

-- READ: admin/management see all. Submitter does NOT see committee thread —
-- it's internal deliberation. Mentioned users see comments where they're tagged.
drop policy if exists read_idea_comments on idea_comments;
create policy read_idea_comments on idea_comments
  for select using (
    current_user_role() in ('admin', 'management')
    or auth.uid() = any(mentioned_user_ids)
  );

-- INSERT: admin/management only. The Chair Desk gates this in the UI; RLS
-- as belt-and-braces.
drop policy if exists insert_idea_comments on idea_comments;
create policy insert_idea_comments on idea_comments
  for insert with check (current_user_role() in ('admin','management'));


-- ─── 4. Anonymity reveal audit ────────────────────────────────────────────
-- True anonymity = even the chair doesn't see submitter identity. Only
-- super admin can reveal, with mandatory reason, and the action is logged.
create table if not exists idea_anonymity_reveals (
  id              uuid primary key default gen_random_uuid(),
  idea_id         uuid not null references ideas(id) on delete cascade,
  revealed_by     uuid not null references auth.users(id),
  reason          text not null check (length(reason) >= 10),
  revealed_at     timestamptz not null default now()
);

create index if not exists idea_reveals_idea_idx on idea_anonymity_reveals (idea_id, revealed_at desc);

alter table idea_anonymity_reveals enable row level security;

-- READ: super admin only.
drop policy if exists read_reveals on idea_anonymity_reveals;
create policy read_reveals on idea_anonymity_reveals
  for select using (is_super_admin());


-- ─── 5. Monday agenda snapshots ───────────────────────────────────────────
-- One row per Monday meeting. Auto-populated 3 days before (Friday 09:00).
create table if not exists monday_agendas (
  meeting_date     date primary key,
  generated_at     timestamptz not null default now(),
  generated_by     text not null default 'cron',  -- 'cron' or 'manual'
  idea_ids         uuid[] not null default '{}',
  total_items      int not null default 0,
  preview_emailed_at timestamptz,
  meeting_started_at timestamptz,
  meeting_ended_at timestamptz
);

alter table monday_agendas enable row level security;

drop policy if exists read_monday_agendas on monday_agendas;
create policy read_monday_agendas on monday_agendas
  for select using (current_user_role() in ('admin','management'));


-- ─── 6. Helper: next Monday ───────────────────────────────────────────────
create or replace function next_monday(from_date date default current_date)
  returns date language sql immutable as $$
  -- Days until the next Monday (Mon=1 in ISO). If it IS Monday, return today.
  select from_date + ((1 - extract(isodow from from_date)::int + 7) % 7);
$$;
grant execute on function next_monday(date) to authenticated;


-- ─── 7. Chair gate ────────────────────────────────────────────────────────
-- Returns true if the calling user is the active chair for this week.
create or replace function is_chair_now()
  returns boolean
  language plpgsql stable security invoker
as $$
declare v_chair uuid;
begin
  select current_committee_chair_id() into v_chair;
  return v_chair is not null and v_chair = auth.uid();
end $$;
grant execute on function is_chair_now() to authenticated;


-- ─── 8. RPC: chair_desk_view ──────────────────────────────────────────────
-- One round-trip returning all 5 sections the Chair Desk needs.
-- Each section is a jsonb array of trimmed idea rows + SLA state.
create or replace function chair_desk_view()
  returns jsonb
  language plpgsql stable security definer set search_path = public
as $$
declare
  v_uid uuid := auth.uid();
  v_is_chair boolean := is_chair_now();
  v_now timestamptz := now();
  v_week_start_monday date := (date_trunc('week', v_now at time zone 'Europe/Athens'))::date;
  v_today_queue jsonb;
  v_consensus jsonb;
  v_for_monday jsonb;
  v_closed_this_week jsonb;
  v_metrics jsonb;
begin
  if not (v_is_chair or current_user_role() in ('admin','management')) then
    raise exception 'chair desk visible to admin / management only' using errcode = '42501';
  end if;

  -- 1. Today's queue: submitted or acknowledged but not yet responded /
  --    discussed / scheduled. Sorted by SLA urgency.
  with q as (
    select i.*,
           idea_sla_status(i.id) as sla,
           extract(epoch from (now() - i.created_at))/3600.0 as age_h
    from ideas i
    where i.status in ('submitted','acknowledged')
  )
  select coalesce(jsonb_agg(jsonb_build_object(
    'id', q.id,
    'subject', q.subject,
    'category', q.category,
    'severity', q.severity,
    'status', q.status,
    'submitter_role', q.submitter_role,
    'submitter_name', case when q.is_anonymous then null else q.submitter_name end,
    'is_anonymous', q.is_anonymous,
    'age_hours', round(q.age_h::numeric, 1),
    'created_at', q.created_at,
    'sla', q.sla,
    'ai_summary', q.ai_summary
  ) order by
    case (q.sla->>'state') when 'escalated' then 0 when 'overdue' then 1 when 'warning' then 2 else 3 end,
    q.severity desc nulls last,
    q.created_at
  ), '[]'::jsonb)
  into v_today_queue from q;

  -- 2. Awaiting consensus: ideas with status=in_discussion
  select coalesce(jsonb_agg(jsonb_build_object(
    'id', i.id,
    'subject', i.subject,
    'category', i.category,
    'severity', i.severity,
    'comment_count', (select count(*) from idea_comments c where c.idea_id = i.id),
    'last_comment_at', (select max(created_at) from idea_comments c where c.idea_id = i.id),
    'created_at', i.created_at,
    'sla', idea_sla_status(i.id)
  ) order by i.created_at), '[]'::jsonb)
  into v_consensus
  from ideas i where i.status = 'in_discussion';

  -- 3. For Monday (read-only preview)
  select coalesce(jsonb_agg(jsonb_build_object(
    'id', i.id,
    'subject', i.subject,
    'category', i.category,
    'severity', i.severity,
    'monday_meeting_date', i.monday_meeting_date,
    'for_monday_reason', i.for_monday_reason,
    'tagged_at', i.for_monday_at
  ) order by i.severity desc nulls last, i.for_monday_at), '[]'::jsonb)
  into v_for_monday
  from ideas i where i.status = 'for_monday';

  -- 4. Closed this week (week starting last Monday Athens)
  select coalesce(jsonb_agg(jsonb_build_object(
    'id', i.id,
    'subject', i.subject,
    'category', i.category,
    'closed_at', coalesce(i.closed_at, i.resolved_at),
    'csat_rating', i.csat_rating
  ) order by coalesce(i.closed_at, i.resolved_at) desc), '[]'::jsonb)
  into v_closed_this_week
  from ideas i
  where i.status in ('resolved','closed','decided_at_monday')
    and coalesce(i.closed_at, i.resolved_at) >= (v_week_start_monday::timestamp at time zone 'Europe/Athens');

  -- 5. Header metrics
  select jsonb_build_object(
    'median_response_hours', (
      select round(percentile_cont(0.5) within group (order by extract(epoch from (resolved_at - created_at))/3600.0)::numeric, 1)
      from ideas where resolved_at is not null
        and resolved_at >= now() - interval '7 days'
    ),
    'median_csat', (
      select round(percentile_cont(0.5) within group (order by csat_rating)::numeric, 1)
      from ideas where csat_rating is not null
        and csat_at >= now() - interval '7 days'
    ),
    'open_count', (select count(*) from ideas where status in ('submitted','acknowledged','in_discussion')),
    'overdue_count', (
      select count(*) from ideas i
      where i.status in ('submitted','acknowledged','in_discussion')
        and (idea_sla_status(i.id)->>'state') in ('overdue','escalated')
    ),
    'is_chair_now', v_is_chair,
    'week_starting', v_week_start_monday
  ) into v_metrics;

  return jsonb_build_object(
    'today_queue', v_today_queue,
    'awaiting_consensus', v_consensus,
    'for_monday', v_for_monday,
    'closed_this_week', v_closed_this_week,
    'metrics', v_metrics
  );
end $$;
grant execute on function chair_desk_view() to authenticated;


-- ─── 9. Quick-action RPCs ─────────────────────────────────────────────────

-- 9a. Acknowledge: clear the ack-SLA, send templated email to submitter.
create or replace function idea_acknowledge(p_idea_id uuid)
  returns void
  language plpgsql security definer set search_path = public
as $$
begin
  if not (is_chair_now() or current_user_role() in ('admin','management')) then
    raise exception 'chair only' using errcode = '42501';
  end if;
  update ideas
    set status = 'acknowledged',
        acknowledged_at = now(),
        acknowledged_by = auth.uid()
  where id = p_idea_id and status = 'submitted';
end $$;
grant execute on function idea_acknowledge(uuid) to authenticated;

-- 9b. Solvable now: chair posts response, idea closes.
create or replace function idea_solvable_now(p_idea_id uuid, p_response text)
  returns void
  language plpgsql security definer set search_path = public
as $$
begin
  if not (is_chair_now() or current_user_role() in ('admin','management')) then
    raise exception 'chair only' using errcode = '42501';
  end if;
  if p_response is null or length(trim(p_response)) < 10 then
    raise exception 'response must be at least 10 characters';
  end if;
  update ideas
    set status = 'resolved',
        committee_response = p_response,
        resolved_at = now(),
        closed_at = now()
  where id = p_idea_id;
end $$;
grant execute on function idea_solvable_now(uuid, text) to authenticated;

-- 9c. Need committee: opens discussion thread + first comment + @mentions.
create or replace function idea_need_committee(
  p_idea_id uuid,
  p_comment text,
  p_mentioned uuid[] default '{}'
) returns uuid
  language plpgsql security definer set search_path = public
as $$
declare v_comment_id uuid;
        v_role text;
        v_name text;
begin
  if not (is_chair_now() or current_user_role() in ('admin','management')) then
    raise exception 'chair only' using errcode = '42501';
  end if;

  update ideas set status = 'in_discussion' where id = p_idea_id;

  select role into v_role from user_roles where user_id = auth.uid();
  select coalesce(raw_user_meta_data->>'full_name', email)
    into v_name from auth.users where id = auth.uid();

  insert into idea_comments (idea_id, author_user_id, author_name, author_role, body, mentioned_user_ids)
  values (p_idea_id, auth.uid(), v_name, v_role, p_comment, coalesce(p_mentioned, '{}'))
  returning id into v_comment_id;

  return v_comment_id;
end $$;
grant execute on function idea_need_committee(uuid, text, uuid[]) to authenticated;

-- 9d. Tag for Monday meeting.
create or replace function idea_to_monday(p_idea_id uuid, p_reason text)
  returns date
  language plpgsql security definer set search_path = public
as $$
declare v_meeting_date date := next_monday();
begin
  if not (is_chair_now() or current_user_role() in ('admin','management')) then
    raise exception 'chair only' using errcode = '42501';
  end if;
  if p_reason is null or length(trim(p_reason)) < 5 then
    raise exception 'reason must be at least 5 characters';
  end if;
  update ideas
    set status = 'for_monday',
        for_monday_at = now(),
        for_monday_reason = p_reason,
        monday_meeting_date = v_meeting_date
  where id = p_idea_id;
  return v_meeting_date;
end $$;
grant execute on function idea_to_monday(uuid, text) to authenticated;

-- 9e. Add comment to existing thread.
create or replace function idea_add_comment(
  p_idea_id uuid,
  p_body text,
  p_mentioned uuid[] default '{}'
) returns uuid
  language plpgsql security definer set search_path = public
as $$
declare v_comment_id uuid;
        v_role text;
        v_name text;
begin
  if current_user_role() not in ('admin','management') then
    raise exception 'committee only' using errcode = '42501';
  end if;
  select role into v_role from user_roles where user_id = auth.uid();
  select coalesce(raw_user_meta_data->>'full_name', email)
    into v_name from auth.users where id = auth.uid();
  insert into idea_comments (idea_id, author_user_id, author_name, author_role, body, mentioned_user_ids)
  values (p_idea_id, auth.uid(), v_name, v_role, p_body, coalesce(p_mentioned, '{}'))
  returning id into v_comment_id;
  return v_comment_id;
end $$;
grant execute on function idea_add_comment(uuid, text, uuid[]) to authenticated;


-- ─── 10. Anonymity reveal — super admin only, audited ─────────────────────
create or replace function idea_reveal_anonymous(p_idea_id uuid, p_reason text)
  returns jsonb
  language plpgsql security definer set search_path = public, auth
as $$
declare v_idea ideas;
        v_user auth.users;
begin
  if not is_super_admin() then
    raise exception 'super admin only' using errcode = '42501';
  end if;
  if p_reason is null or length(trim(p_reason)) < 10 then
    raise exception 'reason must be at least 10 characters';
  end if;

  select * into v_idea from ideas where id = p_idea_id;
  if not found or not v_idea.is_anonymous then
    raise exception 'idea not found or not anonymous';
  end if;

  insert into idea_anonymity_reveals (idea_id, revealed_by, reason)
  values (p_idea_id, auth.uid(), p_reason);

  if v_idea.submitter_user_id is not null then
    select * into v_user from auth.users where id = v_idea.submitter_user_id;
  end if;

  return jsonb_build_object(
    'submitter_user_id', v_idea.submitter_user_id,
    'submitter_name', v_idea.submitter_name,
    'submitter_email', v_idea.submitter_email,
    'submitter_role', v_idea.submitter_role,
    'revealed_at', now()
  );
end $$;
grant execute on function idea_reveal_anonymous(uuid, text) to authenticated;


-- ─── 11. Monday agenda — generate + view ──────────────────────────────────

-- 11a. Generate (called by pg_cron every Friday 09:00 Athens).
create or replace function generate_monday_agenda(p_meeting_date date default null)
  returns jsonb
  language plpgsql security definer set search_path = public
as $$
declare
  v_meeting_date date := coalesce(p_meeting_date, next_monday());
  v_idea_ids uuid[];
  v_count int;
begin
  -- Snapshot all ideas tagged for_monday whose monday_meeting_date matches.
  select coalesce(array_agg(id order by severity desc nulls last, for_monday_at), '{}')
  into v_idea_ids
  from ideas
  where status = 'for_monday'
    and monday_meeting_date = v_meeting_date;

  v_count := coalesce(array_length(v_idea_ids, 1), 0);

  insert into monday_agendas (meeting_date, generated_by, idea_ids, total_items)
  values (v_meeting_date, 'cron', v_idea_ids, v_count)
  on conflict (meeting_date) do update
    set idea_ids = excluded.idea_ids,
        total_items = excluded.total_items,
        generated_at = now();

  return jsonb_build_object(
    'meeting_date', v_meeting_date,
    'total_items', v_count,
    'idea_ids', v_idea_ids
  );
end $$;
grant execute on function generate_monday_agenda(date) to authenticated;

-- 11b. View — returns the agenda + each idea's full data + thread.
create or replace function monday_agenda_view(p_meeting_date date default null)
  returns jsonb
  language plpgsql stable security definer set search_path = public
as $$
declare
  v_meeting_date date := coalesce(p_meeting_date, next_monday());
  v_agenda monday_agendas;
  v_items jsonb;
begin
  if current_user_role() not in ('admin','management') then
    raise exception 'committee only' using errcode = '42501';
  end if;

  select * into v_agenda from monday_agendas where meeting_date = v_meeting_date;

  if not found then
    return jsonb_build_object(
      'meeting_date', v_meeting_date,
      'generated_at', null,
      'total_items', 0,
      'items', '[]'::jsonb
    );
  end if;

  select coalesce(jsonb_agg(jsonb_build_object(
    'id', i.id,
    'subject', i.subject,
    'body', i.body,
    'category', i.category,
    'subcategory', i.subcategory,
    'severity', i.severity,
    'sentiment', i.sentiment,
    'ai_summary', i.ai_summary,
    'ai_good_practices', i.ai_good_practices,
    'ai_starting_points', i.ai_starting_points,
    'submitter_role', i.submitter_role,
    'submitter_name', case when i.is_anonymous then null else i.submitter_name end,
    'is_anonymous', i.is_anonymous,
    'created_at', i.created_at,
    'for_monday_reason', i.for_monday_reason,
    'monday_decision_outcome', i.monday_decision_outcome,
    'monday_decision_notes', i.monday_decision_notes,
    'comments', (
      select coalesce(jsonb_agg(jsonb_build_object(
        'author_name', c.author_name,
        'author_role', c.author_role,
        'body', c.body,
        'created_at', c.created_at
      ) order by c.created_at), '[]'::jsonb)
      from idea_comments c where c.idea_id = i.id
    )
  ) order by i.severity desc nulls last, i.for_monday_at), '[]'::jsonb)
  into v_items
  from ideas i
  where i.id = any(v_agenda.idea_ids);

  return jsonb_build_object(
    'meeting_date', v_agenda.meeting_date,
    'generated_at', v_agenda.generated_at,
    'preview_emailed_at', v_agenda.preview_emailed_at,
    'meeting_started_at', v_agenda.meeting_started_at,
    'meeting_ended_at', v_agenda.meeting_ended_at,
    'total_items', v_agenda.total_items,
    'items', v_items
  );
end $$;
grant execute on function monday_agenda_view(date) to authenticated;


-- ─── 12. Capture Monday meeting decisions ─────────────────────────────────
create or replace function idea_decide_at_monday(
  p_idea_id uuid,
  p_outcome text,
  p_notes text,
  p_action_assignee uuid default null
) returns void
  language plpgsql security definer set search_path = public
as $$
begin
  if current_user_role() not in ('admin','management') then
    raise exception 'committee only' using errcode = '42501';
  end if;
  if p_outcome not in ('decided','tabled','action_assigned') then
    raise exception 'outcome must be decided / tabled / action_assigned';
  end if;

  update ideas
    set status = 'decided_at_monday',
        monday_decision_outcome = p_outcome,
        monday_decision_notes = p_notes,
        action_assignee_user_id = p_action_assignee,
        committee_response = coalesce(committee_response, p_notes),
        resolved_at = case when p_outcome in ('decided','action_assigned') then now() else resolved_at end,
        closed_at = case when p_outcome = 'decided' then now() else closed_at end
  where id = p_idea_id;
end $$;
grant execute on function idea_decide_at_monday(uuid, text, text, uuid) to authenticated;


-- ─── 13. Submitter CSAT capture ───────────────────────────────────────────
-- Open to the submitter (their own idea only) OR admin (for back-fill).
create or replace function idea_submit_csat(
  p_idea_id uuid,
  p_rating int,
  p_comment text default null
) returns void
  language plpgsql security definer set search_path = public
as $$
declare v_submitter uuid;
begin
  if p_rating not between 1 and 5 then
    raise exception 'rating must be 1-5';
  end if;

  select submitter_user_id into v_submitter from ideas where id = p_idea_id;
  if v_submitter is null then
    raise exception 'idea not found';
  end if;
  if v_submitter <> auth.uid() and current_user_role() not in ('admin','management') then
    raise exception 'submitter or admin only' using errcode = '42501';
  end if;

  update ideas
    set csat_rating = p_rating,
        csat_comment = p_comment,
        csat_at = now()
  where id = p_idea_id;
end $$;
grant execute on function idea_submit_csat(uuid, int, text) to authenticated;


-- ─── 14. Friday cron — auto-generate Monday agenda + email committee ──────
-- Edge function `generate-monday-agenda-trigger` will fan out the email.
-- This SQL just snapshots and POSTs to the edge fn.
create or replace function maybe_generate_monday_agenda()
  returns void
  language plpgsql security definer set search_path = cron_private, public
as $$
declare
  v_url text;
  v_secret text;
  v_now_athens timestamptz := now() at time zone 'Europe/Athens';
  v_dow text := to_char(v_now_athens, 'Dy');
  v_meeting_date date := next_monday();
  v_agenda jsonb;
begin
  -- Fire only on Fridays, around 09:00 Athens (cron polls hourly so we
  -- pick the first Fri tick of the day).
  if v_dow <> 'Fri' then return; end if;

  -- Snapshot
  v_agenda := generate_monday_agenda(v_meeting_date);

  -- Optionally POST to edge function for the committee preview email
  select value into v_url    from cron_private.secrets where key = 'generate_monday_agenda_url';
  select value into v_secret from cron_private.secrets where key = 'pipeline_secret';
  if v_url is null or v_secret is null then
    raise notice 'generate_monday_agenda_url or pipeline_secret not set; skipping email';
    return;
  end if;

  perform net.http_post(
    url     := v_url,
    headers := jsonb_build_object(
      'Content-Type', 'application/json',
      'Authorization', 'Bearer ' || v_secret
    ),
    body    := jsonb_build_object(
      'meeting_date', v_meeting_date,
      'total_items', v_agenda->>'total_items',
      'idea_ids', v_agenda->'idea_ids'
    )
  );
end $$;

do $$ declare j_id bigint;
begin
  select jobid into j_id from cron.job where jobname = 'monday-agenda-trigger';
  if j_id is not null then perform cron.unschedule(j_id); end if;
  -- Run hourly; the function gates to Fridays internally and is idempotent
  -- via on conflict do update on (meeting_date).
  perform cron.schedule(
    'monday-agenda-trigger',
    '0 * * * *',
    'select maybe_generate_monday_agenda();'
  );
end $$;


-- ─── 15. Default chair acknowledge template ───────────────────────────────
insert into app_settings (key, value)
values ('chair_ack_template',
'Hi {submitter_first_name},

Thanks for raising this. The committee has seen your submission ({subject}) and is looking into it. We aim to respond within {sla_hours} hours.

— {chair_name}, Daios Cove Executive Committee')
on conflict (key) do nothing;
