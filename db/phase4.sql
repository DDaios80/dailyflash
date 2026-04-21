-- Phase 4 — backend for the Lovable dashboard.
-- Adds: user roles, flash_reports (pre-computed payload), RPCs, tightened RLS.

-- ─── User roles ─────────────────────────────────────────────────────────────
do $$ begin
  create type user_role as enum ('front_office','guest_relations','management','admin');
exception when duplicate_object then null; end $$;

create table if not exists user_roles (
  user_id uuid primary key references auth.users(id) on delete cascade,
  role user_role not null,
  created_at timestamptz not null default now()
);

alter table user_roles enable row level security;
drop policy if exists read_own_role on user_roles;
create policy read_own_role on user_roles
  for select using (auth.uid() = user_id);

create or replace function current_user_role() returns user_role
  language sql stable security definer set search_path = public
as $$
  select role from user_roles where user_id = auth.uid() limit 1;
$$;

create or replace function can_see_alister() returns boolean
  language sql stable security definer set search_path = public
as $$
  select coalesce(current_user_role() in ('guest_relations','management','admin'), false);
$$;

create or replace function can_admin() returns boolean
  language sql stable security definer set search_path = public
as $$
  select coalesce(current_user_role() = 'admin', false);
$$;

-- ─── flash_reports — pre-computed payload per date ─────────────────────────
create table if not exists flash_reports (
  report_date date primary key,
  upload_id uuid references uploads(id) on delete cascade,
  payload jsonb not null,        -- full flash, matches the Daily Flash layout
  computed_at timestamptz not null default now()
);
create index if not exists flash_reports_computed_idx on flash_reports (computed_at desc);

alter table flash_reports enable row level security;
drop policy if exists read_flash_reports on flash_reports;
create policy read_flash_reports on flash_reports
  for select using (auth.role() = 'authenticated');

-- ─── Row-level security — tightened for Phase 4 ────────────────────────────
-- Read policies already exist from the base schema; below we redefine
-- alister_findings so only roles with can_see_alister() view them.
drop policy if exists read_alister_findings on alister_findings;
create policy read_alister_findings on alister_findings
  for select using (auth.role() = 'authenticated' and can_see_alister());

-- Admin-only write policies for daily_briefing and special_attention_overrides.
drop policy if exists admin_write_briefing on daily_briefing;
create policy admin_write_briefing on daily_briefing
  for all using (can_admin()) with check (can_admin());

drop policy if exists gr_write_overrides on special_attention_overrides;
create policy gr_write_overrides on special_attention_overrides
  for all using (
    current_user_role() in ('guest_relations','management','admin')
  ) with check (
    current_user_role() in ('guest_relations','management','admin')
  );

-- ─── RPCs ──────────────────────────────────────────────────────────────────

-- Return the full daily flash payload. Alister data is redacted if the user
-- lacks the role.
create or replace function get_daily_flash(p_date date)
  returns jsonb
  language plpgsql stable security invoker
as $$
declare
  v_payload jsonb;
begin
  select payload into v_payload from flash_reports where report_date = p_date;
  if v_payload is null then return null; end if;

  if not can_see_alister() then
    v_payload := jsonb_set(v_payload, '{alister_findings}', '[]'::jsonb, true);
    v_payload := v_payload - 'alister_findings_count';
  end if;
  return v_payload;
end $$;

-- List every date we have a flash for, newest first.
create or replace function list_flash_dates()
  returns table(report_date date, computed_at timestamptz, row_count int)
  language sql stable security invoker
as $$
  select fr.report_date, fr.computed_at, u.row_count
  from flash_reports fr
  left join uploads u on u.id = fr.upload_id
  order by fr.report_date desc;
$$;

-- Guest Relations promotes a room to "special attention" for a specific date.
create or replace function promote_room(
  p_date date, p_room text, p_reason text
) returns void
  language plpgsql security invoker
as $$
begin
  if current_user_role() not in ('guest_relations','management','admin') then
    raise exception 'not allowed';
  end if;
  insert into special_attention_overrides(report_date, room, reason, added_by)
  values (p_date, p_room, p_reason, auth.uid())
  on conflict (report_date, room) do update
    set reason = excluded.reason,
        added_by = excluded.added_by,
        added_at = now();
end $$;

-- Admin upserts the daily briefing (MOD / weather / events / etc.)
create or replace function upsert_daily_briefing(
  p_date date,
  p_mod_name text,
  p_mod_phone text,
  p_weather jsonb,
  p_hotel_events text,
  p_site_inspections text,
  p_group_events text,
  p_show_rooms text,
  p_notes text
) returns void
  language plpgsql security invoker
as $$
begin
  if not can_admin() then raise exception 'admin only'; end if;
  insert into daily_briefing(
    report_date, mod_name, mod_phone, weather, hotel_events,
    site_inspections, group_events, show_rooms, notes, updated_by
  ) values (
    p_date, p_mod_name, p_mod_phone, p_weather, p_hotel_events,
    p_site_inspections, p_group_events, p_show_rooms, p_notes, auth.uid()
  )
  on conflict (report_date) do update set
    mod_name = excluded.mod_name,
    mod_phone = excluded.mod_phone,
    weather = excluded.weather,
    hotel_events = excluded.hotel_events,
    site_inspections = excluded.site_inspections,
    group_events = excluded.group_events,
    show_rooms = excluded.show_rooms,
    notes = excluded.notes,
    updated_by = auth.uid(),
    updated_at = now();
end $$;

-- ─── Seed convenience: set a user's role ───────────────────────────────────
-- Usage: select set_user_role('user@daioshotels.com', 'admin');
create or replace function set_user_role(p_email text, p_role user_role)
  returns void language plpgsql security definer set search_path = public
as $$
declare v_uid uuid;
begin
  select id into v_uid from auth.users where email = p_email limit 1;
  if v_uid is null then raise exception 'user % not found', p_email; end if;
  insert into user_roles(user_id, role) values (v_uid, p_role)
  on conflict (user_id) do update set role = excluded.role;
end $$;

grant execute on function set_user_role(text, user_role) to authenticated;
grant execute on function get_daily_flash(date) to authenticated;
grant execute on function list_flash_dates() to authenticated;
grant execute on function promote_room(date, text, text) to authenticated;
grant execute on function upsert_daily_briefing(date, text, text, jsonb, text, text, text, text, text) to authenticated;
