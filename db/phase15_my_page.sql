-- Phase 15 — "My" page with configurable widgets.
--
-- Each user picks which dashboard sections they want to see on their
-- personal /my view. Preferences are stored per-user; defaults are
-- derived from the user's role so first-login is immediately useful
-- (F&B gets allergies + kids, Housekeeping gets cots + fences, etc.).
--
-- The frontend renders widgets from the existing flash_reports.payload
-- plus fam_trips / site_inspections / groups — no new backend data
-- needed. The widget registry lives in the frontend.

-- ─── Table ─────────────────────────────────────────────────────────────────
create table if not exists user_preferences (
  user_id    uuid primary key references auth.users(id) on delete cascade,
  widgets    text[] not null default '{}',  -- ordered list of widget IDs
  updated_at timestamptz not null default now()
);

create index if not exists user_preferences_updated_idx
  on user_preferences (updated_at desc);


-- ─── RLS ───────────────────────────────────────────────────────────────────
alter table user_preferences enable row level security;

drop policy if exists read_own_prefs on user_preferences;
create policy read_own_prefs on user_preferences
  for select using (user_id = auth.uid() or can_admin());

drop policy if exists upsert_own_prefs on user_preferences;
create policy upsert_own_prefs on user_preferences
  for insert with check (user_id = auth.uid());

drop policy if exists update_own_prefs on user_preferences;
create policy update_own_prefs on user_preferences
  for update using (user_id = auth.uid());

drop policy if exists delete_own_prefs_admin on user_preferences;
create policy delete_own_prefs_admin on user_preferences
  for delete using (can_admin());


-- ─── Role defaults ─────────────────────────────────────────────────────────
-- Returns the default widget IDs for a role. Used by get_my_widgets() when
-- a user has no saved preferences yet, so first-login is useful immediately.
create or replace function _default_widgets_for_role(p_role text)
  returns text[]
  language sql immutable
as $$
  select case p_role
    when 'admin' then array[
      'a_lister_findings', 'occupancy_snapshot', 'daily_briefing',
      'fam_trips_active', 'site_inspections_today'
    ]
    when 'management' then array[
      'a_lister_findings', 'occupancy_snapshot', 'daily_briefing',
      'fam_trips_active', 'groups_active'
    ]
    when 'guest_relations' then array[
      'special_attention_arrivals', 'birthdays_today', 'honeymoon_arrivals',
      'a_lister_findings', 'daily_briefing', 'fam_trips_active'
    ]
    when 'sales' then array[
      'fam_trips_active', 'site_inspections_today', 'groups_active',
      'occupancy_snapshot', 'daily_briefing'
    ]
    when 'marketing' then array[
      'fam_trips_active', 'groups_active', 'birthdays_today', 'daily_briefing'
    ]
    when 'call_center' then array[
      'special_attention_arrivals', 'transfers_today', 'occupancy_snapshot'
    ]
    when 'f_and_b' then array[
      'allergies_in_house', 'children_today', 'birthdays_today',
      'groups_active', 'fam_trips_active', 'daily_briefing'
    ]
    when 'kitchen' then array[
      'allergies_in_house', 'children_today', 'groups_active',
      'fam_trips_active', 'daily_briefing'
    ]
    when 'housekeeping' then array[
      'cots_and_extra_beds', 'pool_fence', 'special_attention_arrivals',
      'daily_briefing'
    ]
    when 'maintenance' then array[
      'pool_heating', 'pool_fence', 'daily_briefing'
    ]
    when 'front_office' then array[
      'special_attention_arrivals', 'occupancy_snapshot', 'transfers_today',
      'daily_briefing', 'birthdays_today'
    ]
    when 'reservations' then array[
      'occupancy_snapshot', 'special_attention_arrivals', 'daily_briefing'
    ]
    when 'spa' then array[
      'children_today', 'allergies_in_house', 'daily_briefing'
    ]
    when 'fitness' then array[
      'children_today', 'daily_briefing'
    ]
    when 'finance' then array[
      'occupancy_snapshot', 'daily_briefing'
    ]
    when 'hr' then array[
      'daily_briefing'
    ]
    else array[
      'daily_briefing', 'weather_3day', 'occupancy_snapshot'
    ]
  end;
$$;


-- ─── RPC: get widgets for current user ────────────────────────────────────
-- Returns the user's saved widget list, or the role-based default if they
-- haven't saved yet. Single round-trip for the /my page load.
create or replace function get_my_widgets()
  returns text[]
  language plpgsql stable security invoker
as $$
declare
  v_saved text[];
  v_role  text;
begin
  if auth.uid() is null then
    return array[]::text[];
  end if;

  select widgets into v_saved
  from user_preferences where user_id = auth.uid();

  if v_saved is not null and array_length(v_saved, 1) is not null then
    return v_saved;
  end if;

  -- No saved preferences — fall back to role defaults
  select role::text into v_role
  from user_roles where user_id = auth.uid();

  return _default_widgets_for_role(coalesce(v_role, 'other'));
end $$;
grant execute on function get_my_widgets() to authenticated;


-- ─── RPC: save widgets for current user ───────────────────────────────────
create or replace function save_my_widgets(p_widgets text[])
  returns void
  language plpgsql security invoker
as $$
begin
  if auth.uid() is null then
    raise exception 'not authenticated';
  end if;
  if array_length(p_widgets, 1) is null then
    p_widgets := array[]::text[];
  end if;
  if array_length(p_widgets, 1) > 50 then
    raise exception 'too many widgets (max 50)';
  end if;

  insert into user_preferences (user_id, widgets, updated_at)
  values (auth.uid(), p_widgets, now())
  on conflict (user_id) do update set
    widgets = excluded.widgets,
    updated_at = now();
end $$;
grant execute on function save_my_widgets(text[]) to authenticated;


-- ─── RPC: reset to role defaults ──────────────────────────────────────────
-- Deletes the user's saved preferences so get_my_widgets() falls back to
-- the role default. Useful when the admin adds new widgets and an existing
-- user wants a fresh start.
create or replace function reset_my_widgets()
  returns void
  language plpgsql security invoker
as $$
begin
  if auth.uid() is null then
    raise exception 'not authenticated';
  end if;
  delete from user_preferences where user_id = auth.uid();
end $$;
grant execute on function reset_my_widgets() to authenticated;
