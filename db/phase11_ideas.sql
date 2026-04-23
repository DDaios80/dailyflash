-- Phase 11 — Ideas & Opinions.
--
-- Employee suggestion / feedback channel. Any authenticated user can submit
-- an idea, opinion, or issue. On submit:
--   1) Row is inserted with status='submitted'
--   2) Edge function `analyze-idea` is fired via pg_net — it calls Claude
--      Opus 4.7 with web_search to categorise, assess severity/sentiment,
--      surface good practices + starting points, then emails the committee.
--
-- Visibility:
--   * Submitter sees their own submissions (including anonymous ones)
--   * Admin / management (the "Executive Committee") see everything
--   * Nobody else — we don't want ideas being read by peers
--
-- Email routing: committee@daioscove.com (configurable via
-- app_settings.ideas_committee_email).

-- ─── Enums ──────────────────────────────────────────────────────────────────
do $$ begin
  create type idea_status as enum (
    'submitted', 'triaged', 'in_progress', 'resolved', 'closed', 'archived'
  );
exception when duplicate_object then null; end $$;

do $$ begin
  create type idea_category as enum (
    'guest_experience',
    'operations',
    'staff_hr',
    'cost_revenue',
    'safety_compliance',
    'technology',
    'sustainability',
    'other'
  );
exception when duplicate_object then null; end $$;

do $$ begin
  create type idea_severity as enum ('low', 'medium', 'high', 'critical');
exception when duplicate_object then null; end $$;

do $$ begin
  create type idea_sentiment as enum (
    'positive', 'constructive', 'frustrated', 'urgent'
  );
exception when duplicate_object then null; end $$;


-- ─── Main table ─────────────────────────────────────────────────────────────
create table if not exists ideas (
  id uuid primary key default gen_random_uuid(),

  -- Submitter (captured at insert time for denormalised display)
  submitter_user_id   uuid references auth.users(id),
  submitter_name      text,                      -- display name at submit time
  submitter_role      text,                      -- role at submit time
  submitter_email     text,                      -- email at submit time (for confirmation)
  is_anonymous        boolean not null default false,

  -- Content
  subject             text not null check (length(subject) between 3 and 200),
  body                text not null check (length(body) between 10 and 10000),
  category_hint       idea_category,             -- user's own guess, optional

  -- AI analysis (populated by analyze-idea edge function)
  ai_analyzed_at      timestamptz,
  ai_model            text,                      -- e.g. 'claude-opus-4-7'
  category            idea_category,
  subcategory         text,                      -- free-text department/area
  severity            idea_severity,
  sentiment           idea_sentiment,
  ai_summary          text,                      -- 1-sentence summary
  ai_good_practices   jsonb,                     -- array of {title, detail, source_url?}
  ai_starting_points  jsonb,                     -- array of {title, detail}
  ai_similar_idea_ids uuid[],                    -- IDs of semantically similar prior ideas
  ai_evidence_urls    text[],                    -- URLs Claude used

  -- Workflow
  status              idea_status not null default 'submitted',
  committee_response  text,
  assigned_to_user_id uuid references auth.users(id),
  assigned_to_name    text,
  resolved_at         timestamptz,
  closed_at           timestamptz,

  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create index if not exists ideas_status_idx     on ideas (status, created_at desc);
create index if not exists ideas_category_idx   on ideas (category, created_at desc) where category is not null;
create index if not exists ideas_submitter_idx  on ideas (submitter_user_id, created_at desc);
create index if not exists ideas_severity_idx   on ideas (severity, created_at desc) where severity is not null;


-- ─── updated_at trigger ─────────────────────────────────────────────────────
create or replace function _touch_ideas_updated_at()
  returns trigger language plpgsql as
$$ begin new.updated_at := now(); return new; end $$;

drop trigger if exists ideas_touch on ideas;
create trigger ideas_touch
  before update on ideas
  for each row execute function _touch_ideas_updated_at();


-- ─── RLS ────────────────────────────────────────────────────────────────────
alter table ideas enable row level security;

-- READ: submitter sees own; admin/management (Executive Committee) see all.
drop policy if exists read_ideas on ideas;
create policy read_ideas on ideas
  for select using (
    current_user_role() in ('admin', 'management')
    or submitter_user_id = auth.uid()
  );

-- INSERT: any authenticated user. submitter_user_id must be self.
drop policy if exists insert_ideas on ideas;
create policy insert_ideas on ideas
  for insert with check (
    auth.role() = 'authenticated'
    and submitter_user_id = auth.uid()
  );

-- UPDATE: admin/management can change anything; submitter can only edit
-- their own draft before committee sees it (i.e. status='submitted' and
-- within 15 minutes of creation, to protect against late regrets).
drop policy if exists update_ideas on ideas;
create policy update_ideas on ideas
  for update using (
    current_user_role() in ('admin', 'management')
    or (
      submitter_user_id = auth.uid()
      and status = 'submitted'
      and created_at > now() - interval '15 minutes'
    )
  );

-- DELETE: admin only (keeps an audit trail of honest feedback intact).
drop policy if exists delete_ideas_admin on ideas;
create policy delete_ideas_admin on ideas
  for delete using (can_admin());


-- ─── Settings: committee email (admin-editable) ─────────────────────────────
insert into app_settings (key, value) values
  ('ideas_committee_email', 'committee@daioscove.com')
on conflict (key) do nothing;


-- ─── RPC: submit an idea and fire the analyzer ─────────────────────────────
-- Called by the frontend. Handles: insert row, denormalise submitter info,
-- then fire the analyze-idea edge function via pg_net.
create or replace function submit_idea(
  p_subject       text,
  p_body          text,
  p_category_hint idea_category default null,
  p_is_anonymous  boolean        default false
) returns uuid
  language plpgsql security definer set search_path = cron_private, public
as $$
declare
  v_user_id   uuid := auth.uid();
  v_email     text;
  v_name      text;
  v_role      text;
  v_new_id    uuid;
  v_url       text;
  v_secret    text;
begin
  if v_user_id is null then
    raise exception 'not authenticated';
  end if;

  -- Pull display-friendly submitter info
  select
    u.email,
    coalesce((u.raw_user_meta_data ->> 'full_name'), (u.raw_user_meta_data ->> 'name'), u.email),
    ur.role::text
  into v_email, v_name, v_role
  from auth.users u
  left join user_roles ur on ur.user_id = u.id
  where u.id = v_user_id;

  insert into ideas (
    submitter_user_id, submitter_name, submitter_role, submitter_email,
    is_anonymous, subject, body, category_hint
  ) values (
    v_user_id, v_name, v_role, v_email,
    coalesce(p_is_anonymous, false), p_subject, p_body, p_category_hint
  )
  returning id into v_new_id;

  -- Fire analyzer (best-effort; edge function handles email + AI analysis)
  select value into v_url    from cron_private.secrets where key = 'analyze_idea_url';
  select value into v_secret from cron_private.secrets where key = 'pipeline_secret';
  if v_url is not null and v_secret is not null then
    perform net.http_post(
      url     := v_url,
      headers := jsonb_build_object(
        'Content-Type',  'application/json',
        'Authorization', 'Bearer ' || v_secret
      ),
      body    := jsonb_build_object('idea_id', v_new_id::text)
    );
  end if;

  return v_new_id;
end $$;
grant execute on function submit_idea(text, text, idea_category, boolean) to authenticated;


-- ─── RPC: committee actions (set status / response / assignment) ───────────
create or replace function committee_update_idea(
  p_idea_id          uuid,
  p_status           idea_status default null,
  p_committee_response text      default null,
  p_assigned_user_id uuid        default null
) returns void
  language plpgsql security invoker
as $$
declare
  v_assignee_name text;
begin
  if current_user_role() not in ('admin', 'management') then
    raise exception 'executive committee only';
  end if;

  if p_assigned_user_id is not null then
    select coalesce((u.raw_user_meta_data ->> 'full_name'), (u.raw_user_meta_data ->> 'name'), u.email)
    into v_assignee_name
    from auth.users u where u.id = p_assigned_user_id;
  end if;

  update ideas set
    status              = coalesce(p_status, status),
    committee_response  = coalesce(p_committee_response, committee_response),
    assigned_to_user_id = coalesce(p_assigned_user_id, assigned_to_user_id),
    assigned_to_name    = case when p_assigned_user_id is not null then v_assignee_name
                               else assigned_to_name end,
    resolved_at         = case when p_status = 'resolved' and resolved_at is null then now()
                               else resolved_at end,
    closed_at           = case when p_status in ('closed', 'archived') and closed_at is null then now()
                               else closed_at end
  where id = p_idea_id;
end $$;
grant execute on function committee_update_idea(uuid, idea_status, text, uuid) to authenticated;


-- ─── RPC: list committee members (for assignment dropdown) ─────────────────
create or replace function list_executive_committee()
  returns table(user_id uuid, email text, display_name text, role text)
  language sql stable security definer set search_path = public
as $$
  select u.id, u.email,
         coalesce(
           (u.raw_user_meta_data ->> 'full_name'),
           (u.raw_user_meta_data ->> 'name'),
           u.email
         ),
         ur.role::text
  from auth.users u
  inner join user_roles ur on ur.user_id = u.id
  where ur.role in ('admin', 'management');
$$;
grant execute on function list_executive_committee() to authenticated;


-- ─── Post-migration: admin primes 1 secret ─────────────────────────────────
-- insert into cron_private.secrets (key, value) values
--   ('analyze_idea_url',
--    'https://wgbghdbfmapuqbfeiygb.supabase.co/functions/v1/analyze-idea')
-- on conflict (key) do update set value = excluded.value, updated_at = now();
