-- Phase 70 — Monday Agenda Builder (2026-05-14)
--
-- Builds on Phase 25 scaffolding (monday_agendas, generate_monday_agenda,
-- monday_agenda_view, idea_decide_at_monday) to support a full chair-driven
-- agenda assembly flow with ExCom member status submissions and structured
-- block organization.
--
-- See docs/phase70-agenda-builder-spec.md for the full design.
--
-- Apply via Supabase SQL editor.

-- ─── 1. Extend monday_agendas with publication + meta ──────────────────

alter table monday_agendas
  add column if not exists published_at timestamptz,
  add column if not exists published_by_user_id uuid references auth.users(id),
  add column if not exists published_markdown text,
  add column if not exists pre_reads jsonb not null default '[]'::jsonb,
  add column if not exists duration_minutes int not null default 120;

-- ─── 2. excom_status_snapshots ────────────────────────────────────────

create table if not exists excom_status_snapshots (
  id uuid primary key default gen_random_uuid(),
  meeting_date date not null references monday_agendas(meeting_date) on delete cascade,
  member_user_id uuid not null references auth.users(id),
  member_name text not null,
  bullets text[] not null default '{}',
  submitted_at timestamptz not null default now(),
  updated_at timestamptz,
  revision_count int not null default 0,
  unique (meeting_date, member_user_id)
);

create index if not exists idx_excom_status_meeting on excom_status_snapshots(meeting_date);
create index if not exists idx_excom_status_member on excom_status_snapshots(member_user_id);

alter table excom_status_snapshots enable row level security;

drop policy if exists read_excom_status_admins on excom_status_snapshots;
create policy read_excom_status_admins on excom_status_snapshots
  for select using (current_user_role() in ('admin','management'));

drop policy if exists write_excom_status_self on excom_status_snapshots;
create policy write_excom_status_self on excom_status_snapshots
  for all using (member_user_id = auth.uid() or is_super_admin())
  with check (member_user_id = auth.uid() or is_super_admin());

-- ─── 3. agenda_items ──────────────────────────────────────────────────

create table if not exists agenda_items (
  id uuid primary key default gen_random_uuid(),
  meeting_date date not null references monday_agendas(meeting_date) on delete cascade,
  block text not null check (block in (
    'A_opening','B_strategic','C_carryover','D_new','E_headsup','F_close'
  )),
  position int not null,
  title text not null,
  description text,
  owner_label text not null,
  owner_user_id uuid references auth.users(id),
  item_type text check (item_type in (
    'heads_up','decision','input','scoping','apply_it','walkthrough','close'
  )),
  time_minutes int not null check (time_minutes between 1 and 60),
  source_idea_id uuid references ideas(id) on delete set null,
  created_at timestamptz not null default now(),
  created_by_user_id uuid references auth.users(id),
  unique (meeting_date, block, position)
);

create index if not exists idx_agenda_items_meeting on agenda_items(meeting_date);
create index if not exists idx_agenda_items_block on agenda_items(meeting_date, block, position);

alter table agenda_items enable row level security;

drop policy if exists read_agenda_items_admins on agenda_items;
create policy read_agenda_items_admins on agenda_items
  for select using (current_user_role() in ('admin','management'));

-- Writes gated to chair + super_admin (enforced in RPCs, but RLS as backstop).
drop policy if exists write_agenda_items_chair on agenda_items;
create policy write_agenda_items_chair on agenda_items
  for all using (
    is_super_admin()
    or (current_committee_chair_id() = auth.uid())
  )
  with check (
    is_super_admin()
    or (current_committee_chair_id() = auth.uid())
  );

-- ─── 4. RPCs ──────────────────────────────────────────────────────────

-- 4a. ExCom member submits or updates their status for a meeting.
create or replace function excom_submit_status(
  p_meeting_date date,
  p_bullets text[]
) returns uuid
  language plpgsql security definer set search_path = public
as $$
declare
  v_user_id uuid := auth.uid();
  v_member_name text;
  v_existing_id uuid;
  v_result_id uuid;
begin
  if current_user_role() not in ('admin','management') then
    raise exception 'committee only' using errcode = '42501';
  end if;

  if array_length(p_bullets, 1) is null or array_length(p_bullets, 1) = 0 then
    raise exception 'at least one bullet required';
  end if;

  if array_length(p_bullets, 1) > 10 then
    raise exception 'maximum 10 bullets allowed';
  end if;

  -- Make sure the meeting row exists (auto-generate if not).
  insert into monday_agendas (meeting_date, generated_by, total_items)
    values (p_meeting_date, 'manual_excom_submit', 0)
    on conflict (meeting_date) do nothing;

  select coalesce(u.raw_user_meta_data->>'full_name', u.email)
    into v_member_name
    from auth.users u where u.id = v_user_id;

  select id into v_existing_id
    from excom_status_snapshots
    where meeting_date = p_meeting_date
      and member_user_id = v_user_id;

  if v_existing_id is not null then
    update excom_status_snapshots set
      bullets = p_bullets,
      member_name = v_member_name,
      updated_at = now(),
      revision_count = revision_count + 1
    where id = v_existing_id;
    v_result_id := v_existing_id;
  else
    insert into excom_status_snapshots (
      meeting_date, member_user_id, member_name, bullets
    ) values (
      p_meeting_date, v_user_id, v_member_name, p_bullets
    ) returning id into v_result_id;
  end if;

  return v_result_id;
end $$;

grant execute on function excom_submit_status(date, text[])
  to authenticator, anon, authenticated, service_role;


-- 4b. Add an item to an agenda block. Auto-assigns next position.
create or replace function agenda_add_item(
  p_meeting_date date,
  p_block text,
  p_title text,
  p_description text,
  p_owner_label text,
  p_owner_user_id uuid,
  p_item_type text,
  p_time_minutes int,
  p_source_idea_id uuid default null
) returns uuid
  language plpgsql security definer set search_path = public
as $$
declare
  v_next_position int;
  v_result_id uuid;
begin
  if not (is_super_admin() or current_committee_chair_id() = auth.uid()) then
    raise exception 'only the current chair or super_admin can manage agenda items'
      using errcode = '42501';
  end if;

  -- Make sure the meeting row exists.
  insert into monday_agendas (meeting_date, generated_by, total_items)
    values (p_meeting_date, 'manual_agenda_build', 0)
    on conflict (meeting_date) do nothing;

  select coalesce(max(position), 0) + 1
    into v_next_position
    from agenda_items
    where meeting_date = p_meeting_date and block = p_block;

  insert into agenda_items (
    meeting_date, block, position,
    title, description,
    owner_label, owner_user_id,
    item_type, time_minutes,
    source_idea_id, created_by_user_id
  ) values (
    p_meeting_date, p_block, v_next_position,
    p_title, p_description,
    p_owner_label, p_owner_user_id,
    p_item_type, p_time_minutes,
    p_source_idea_id, auth.uid()
  ) returning id into v_result_id;

  return v_result_id;
end $$;

grant execute on function agenda_add_item(date, text, text, text, text, uuid, text, int, uuid)
  to authenticator, anon, authenticated, service_role;


-- 4c. Remove an item from the agenda.
create or replace function agenda_remove_item(p_item_id uuid)
  returns void
  language plpgsql security definer set search_path = public
as $$
begin
  if not (is_super_admin() or current_committee_chair_id() = auth.uid()) then
    raise exception 'only the current chair or super_admin can manage agenda items'
      using errcode = '42501';
  end if;

  delete from agenda_items where id = p_item_id;
end $$;

grant execute on function agenda_remove_item(uuid)
  to authenticator, anon, authenticated, service_role;


-- 4d. Reorder items within a block.
create or replace function agenda_reorder_block(
  p_meeting_date date,
  p_block text,
  p_ordered_item_ids uuid[]
) returns void
  language plpgsql security definer set search_path = public
as $$
declare
  v_i int;
begin
  if not (is_super_admin() or current_committee_chair_id() = auth.uid()) then
    raise exception 'only the current chair or super_admin can manage agenda items'
      using errcode = '42501';
  end if;

  -- Temporarily set positions to negative to avoid unique conflicts during reorder.
  update agenda_items set position = -position
    where meeting_date = p_meeting_date and block = p_block;

  for v_i in 1..array_length(p_ordered_item_ids, 1) loop
    update agenda_items set position = v_i
      where id = p_ordered_item_ids[v_i]
        and meeting_date = p_meeting_date
        and block = p_block;
  end loop;
end $$;

grant execute on function agenda_reorder_block(date, text, uuid[])
  to authenticator, anon, authenticated, service_role;


-- 4e. Publish (lock + snapshot) the agenda.
create or replace function agenda_publish(
  p_meeting_date date,
  p_rendered_markdown text
) returns void
  language plpgsql security definer set search_path = public
as $$
begin
  if not (is_super_admin() or current_committee_chair_id() = auth.uid()) then
    raise exception 'only the current chair or super_admin can publish'
      using errcode = '42501';
  end if;

  if p_rendered_markdown is null or length(trim(p_rendered_markdown)) < 100 then
    raise exception 'rendered markdown must be at least 100 chars';
  end if;

  update monday_agendas set
    published_at = now(),
    published_by_user_id = auth.uid(),
    published_markdown = p_rendered_markdown
  where meeting_date = p_meeting_date;

  if not found then
    raise exception 'no monday_agendas row for %', p_meeting_date;
  end if;
end $$;

grant execute on function agenda_publish(date, text)
  to authenticator, anon, authenticated, service_role;


-- 4f. Full view — returns the structured agenda for chair UI + ExCom preview.
create or replace function monday_agenda_full_view(p_meeting_date date default null)
  returns jsonb
  language plpgsql stable security definer set search_path = public
as $$
declare
  v_meeting_date date := coalesce(p_meeting_date, next_monday());
  v_agenda monday_agendas;
  v_snapshots jsonb;
  v_items jsonb;
  v_total_time int;
begin
  if current_user_role() not in ('admin','management') then
    raise exception 'committee only' using errcode = '42501';
  end if;

  select * into v_agenda from monday_agendas where meeting_date = v_meeting_date;

  if not found then
    return jsonb_build_object(
      'meeting_date', v_meeting_date,
      'exists', false
    );
  end if;

  -- Status snapshots for Block A.
  select coalesce(jsonb_agg(
    jsonb_build_object(
      'id', s.id,
      'member_user_id', s.member_user_id,
      'member_name', s.member_name,
      'bullets', s.bullets,
      'submitted_at', s.submitted_at,
      'updated_at', s.updated_at,
      'revision_count', s.revision_count
    ) order by s.member_name
  ), '[]'::jsonb)
  into v_snapshots
  from excom_status_snapshots s
  where s.meeting_date = v_meeting_date;

  -- Agenda items grouped by block.
  select coalesce(jsonb_agg(
    jsonb_build_object(
      'id', a.id,
      'block', a.block,
      'position', a.position,
      'title', a.title,
      'description', a.description,
      'owner_label', a.owner_label,
      'owner_user_id', a.owner_user_id,
      'item_type', a.item_type,
      'time_minutes', a.time_minutes,
      'source_idea_id', a.source_idea_id
    ) order by a.block, a.position
  ), '[]'::jsonb)
  into v_items
  from agenda_items a
  where a.meeting_date = v_meeting_date;

  -- Total time = sum of agenda_items + fixed Block A (10 min) + Block F (5 min buffer)
  select coalesce(sum(time_minutes), 0) + 15
    into v_total_time
    from agenda_items
    where meeting_date = v_meeting_date;

  return jsonb_build_object(
    'meeting_date', v_meeting_date,
    'exists', true,
    'duration_minutes', v_agenda.duration_minutes,
    'generated_at', v_agenda.generated_at,
    'generated_by', v_agenda.generated_by,
    'published_at', v_agenda.published_at,
    'published_by_user_id', v_agenda.published_by_user_id,
    'published_markdown', v_agenda.published_markdown,
    'pre_reads', v_agenda.pre_reads,
    'meeting_started_at', v_agenda.meeting_started_at,
    'meeting_ended_at', v_agenda.meeting_ended_at,
    'block_A_status_snapshots', v_snapshots,
    'agenda_items', v_items,
    'total_time_allocated_minutes', v_total_time,
    'time_buffer_minutes', v_agenda.duration_minutes - v_total_time
  );
end $$;

grant execute on function monday_agenda_full_view(date)
  to authenticator, anon, authenticated, service_role;


-- ─── 5. PostgREST cache reload ─────────────────────────────────────────

notify pgrst, 'reload schema';


-- ─── 6. Verification ───────────────────────────────────────────────────

select
  table_name,
  count(*) as column_count
from information_schema.columns
where table_schema = 'public'
  and table_name in ('monday_agendas', 'excom_status_snapshots', 'agenda_items')
group by table_name
order by table_name;

-- Expected: 3 rows. monday_agendas: 13 cols (8 from Phase 25 + 5 added).
-- excom_status_snapshots: 8 cols. agenda_items: 13 cols.

select
  proname,
  pg_get_function_identity_arguments(oid) as args
from pg_proc
where pronamespace = 'public'::regnamespace
  and proname in (
    'excom_submit_status',
    'agenda_add_item',
    'agenda_remove_item',
    'agenda_reorder_block',
    'agenda_publish',
    'monday_agenda_full_view'
  )
order by proname;

-- Expected: 6 rows for the new RPCs.
