-- Phase 21 — Weekly ExCom rotation for committee/chair emails.
--
-- Replaces the single static committee_chair_user_id with a rotating
-- roster. Super admin owns the roster and can override any specific
-- week (e.g. for leave).
--
-- Rotation model:
--   * excom_rotation is an ordered roster (positions 1..N)
--   * Every Monday Athens time a new person becomes active chair
--   * The active person is (weeks_since_seed mod N) + 1
--   * Per-week overrides in excom_rotation_overrides trump the rotation
--   * If roster is empty, falls back to app_settings.committee_chair_user_id
--     (preserves Phase 20 behaviour while roster is being built).

-- ─── Ordered roster ────────────────────────────────────────────────────────
create table if not exists excom_rotation (
  position  int primary key check (position >= 1),
  user_id   uuid not null unique references auth.users(id) on delete cascade,
  added_at  timestamptz not null default now(),
  added_by  uuid references auth.users(id)
);

alter table excom_rotation enable row level security;

drop policy if exists excom_rotation_read on excom_rotation;
create policy excom_rotation_read on excom_rotation
  for select using (is_super_admin() or can_admin());

drop policy if exists excom_rotation_write on excom_rotation;
create policy excom_rotation_write on excom_rotation
  for all using (is_super_admin()) with check (is_super_admin());


-- ─── Per-week overrides ───────────────────────────────────────────────────
create table if not exists excom_rotation_overrides (
  week_monday date primary key,
  user_id     uuid not null references auth.users(id) on delete cascade,
  reason      text,
  created_by  uuid references auth.users(id),
  created_at  timestamptz not null default now()
);

alter table excom_rotation_overrides enable row level security;

drop policy if exists excom_ro_read on excom_rotation_overrides;
create policy excom_ro_read on excom_rotation_overrides
  for select using (is_super_admin() or can_admin());

drop policy if exists excom_ro_write on excom_rotation_overrides;
create policy excom_ro_write on excom_rotation_overrides
  for all using (is_super_admin()) with check (is_super_admin());


-- ─── Helper: Monday of the Athens week containing ts ─────────────────────
create or replace function athens_monday(ts timestamptz default now())
  returns date language sql immutable as $$
  select (date_trunc('week', ts at time zone 'Europe/Athens'))::date;
$$;
grant execute on function athens_monday(timestamptz) to authenticated;


-- ─── Position lookup for a given Monday ──────────────────────────────────
create or replace function excom_chair_for_week(p_week_monday date)
  returns uuid
  language plpgsql stable security definer set search_path = public
as $$
declare
  v_n         int;
  v_seed_date date;
  v_idx       int;
  v_uid       uuid;
begin
  select count(*) into v_n from excom_rotation;
  if v_n = 0 then
    return null;
  end if;

  -- Seed defaults to the first time we read it; stored as ISO date text.
  select value::date into v_seed_date
    from app_settings where key = 'excom_rotation_seed_date';

  if v_seed_date is null then
    v_seed_date := athens_monday();
    insert into app_settings (key, value)
    values ('excom_rotation_seed_date', v_seed_date::text)
    on conflict (key) do nothing;
  end if;

  -- Safe modulo for negative deltas too.
  v_idx := ((((p_week_monday - v_seed_date) / 7) % v_n) + v_n) % v_n + 1;

  select user_id into v_uid from excom_rotation where position = v_idx;
  return v_uid;
end $$;
grant execute on function excom_chair_for_week(date) to authenticated;


-- ─── Current chair (honours overrides, falls back to Phase 20 setting) ──
create or replace function current_committee_chair_id()
  returns uuid
  language plpgsql stable security definer set search_path = public
as $$
declare
  v_wm      date := athens_monday();
  v_uid     uuid;
begin
  -- 1. explicit override
  select user_id into v_uid
    from excom_rotation_overrides where week_monday = v_wm;
  if v_uid is not null then return v_uid; end if;

  -- 2. rotation lookup
  v_uid := excom_chair_for_week(v_wm);
  if v_uid is not null then return v_uid; end if;

  -- 3. legacy fallback
  return (select value::uuid from app_settings
          where key = 'committee_chair_user_id');
end $$;
grant execute on function current_committee_chair_id() to authenticated;


-- ─── Resolved chair with email + name (used by the edge function) ───────
create or replace function current_committee_chair()
  returns table (user_id uuid, email text, display_name text, source text)
  language plpgsql stable security definer set search_path = public, auth
as $$
declare
  v_wm   date := athens_monday();
  v_uid  uuid;
  v_src  text;
begin
  select user_id into v_uid
    from excom_rotation_overrides where week_monday = v_wm;
  if v_uid is not null then
    v_src := 'override';
  else
    v_uid := excom_chair_for_week(v_wm);
    if v_uid is not null then
      v_src := 'rotation';
    else
      v_uid := (select value::uuid from app_settings
                where key = 'committee_chair_user_id');
      v_src := 'fallback';
    end if;
  end if;

  if v_uid is null then return; end if;

  return query
  select u.id,
         u.email,
         coalesce(
           (u.raw_user_meta_data->>'full_name'),
           (u.raw_user_meta_data->>'name'),
           u.email
         ) as display_name,
         v_src
  from auth.users u where u.id = v_uid;
end $$;
grant execute on function current_committee_chair() to authenticated;


-- ─── Upcoming schedule view (super-admin UI) ────────────────────────────
create or replace function excom_rotation_upcoming(p_weeks int default 12)
  returns table (
    week_monday     date,
    iso_week        text,
    user_id         uuid,
    email           text,
    display_name    text,
    roster_position int,
    is_override     boolean,
    override_reason text
  )
  language plpgsql stable security definer set search_path = public, auth
as $$
declare
  v_base date := athens_monday();
  v_n    int;
begin
  select count(*) into v_n from excom_rotation;

  return query
  with cal as (
    select (v_base + (gs * 7))::date as wm
    from generate_series(0, greatest(p_weeks - 1, 0)) gs
  ), resolved as (
    select
      c.wm,
      ovr.user_id as override_uid,
      ovr.reason  as override_reason,
      case when v_n = 0 then null
           else excom_chair_for_week(c.wm) end as rotation_uid
    from cal c
    left join excom_rotation_overrides ovr on ovr.week_monday = c.wm
  )
  select
    r.wm as week_monday,
    to_char(r.wm, 'IYYY-"W"IW') as iso_week,
    coalesce(r.override_uid, r.rotation_uid) as user_id,
    u.email,
    coalesce(
      (u.raw_user_meta_data->>'full_name'),
      (u.raw_user_meta_data->>'name'),
      u.email
    ) as display_name,
    rot.position as roster_position,
    (r.override_uid is not null) as is_override,
    r.override_reason
  from resolved r
  left join auth.users u on u.id = coalesce(r.override_uid, r.rotation_uid)
  left join excom_rotation rot on rot.user_id = coalesce(r.override_uid, r.rotation_uid)
  order by r.wm;
end $$;
grant execute on function excom_rotation_upcoming(int) to authenticated;


-- ─── Roster management RPCs (super-admin only) ──────────────────────────

-- Add user to the end of the roster.
create or replace function excom_rotation_add(p_user_id uuid)
  returns int
  language plpgsql security definer set search_path = public
as $$
declare v_next int;
begin
  if not is_super_admin() then
    raise exception 'super admin only' using errcode = '42501';
  end if;
  select coalesce(max(position), 0) + 1 into v_next from excom_rotation;
  insert into excom_rotation (position, user_id, added_by)
  values (v_next, p_user_id, auth.uid());
  return v_next;
end $$;
grant execute on function excom_rotation_add(uuid) to authenticated;

-- Remove user from roster and compact positions.
create or replace function excom_rotation_remove(p_user_id uuid)
  returns void
  language plpgsql security definer set search_path = public
as $$
declare
  v_pos int;
begin
  if not is_super_admin() then
    raise exception 'super admin only' using errcode = '42501';
  end if;

  select position into v_pos from excom_rotation where user_id = p_user_id;
  if v_pos is null then return; end if;

  delete from excom_rotation where user_id = p_user_id;

  -- Compact: shift every position > v_pos down by 1.
  -- Done in a single pass to respect the unique (position) constraint:
  -- negate, then set to positive.
  update excom_rotation set position = -(position - 1) where position > v_pos;
  update excom_rotation set position = -position where position < 0;
end $$;
grant execute on function excom_rotation_remove(uuid) to authenticated;

-- Replace roster with an ordered list of user ids. Simplest & safest for UI.
create or replace function excom_rotation_set(p_user_ids uuid[])
  returns int
  language plpgsql security definer set search_path = public
as $$
declare
  v_i int;
begin
  if not is_super_admin() then
    raise exception 'super admin only' using errcode = '42501';
  end if;

  delete from excom_rotation;
  for v_i in 1 .. coalesce(array_length(p_user_ids, 1), 0) loop
    insert into excom_rotation (position, user_id, added_by)
    values (v_i, p_user_ids[v_i], auth.uid());
  end loop;
  return coalesce(array_length(p_user_ids, 1), 0);
end $$;
grant execute on function excom_rotation_set(uuid[]) to authenticated;


-- Upsert a per-week override.
create or replace function excom_rotation_override_set(
  p_week_monday date, p_user_id uuid, p_reason text default null)
  returns void
  language plpgsql security definer set search_path = public
as $$
begin
  if not is_super_admin() then
    raise exception 'super admin only' using errcode = '42501';
  end if;

  insert into excom_rotation_overrides (week_monday, user_id, reason, created_by)
  values (p_week_monday, p_user_id, p_reason, auth.uid())
  on conflict (week_monday) do update
    set user_id = excluded.user_id,
        reason = excluded.reason,
        created_by = excluded.created_by,
        created_at = now();
end $$;
grant execute on function excom_rotation_override_set(date, uuid, text) to authenticated;

create or replace function excom_rotation_override_clear(p_week_monday date)
  returns void
  language plpgsql security definer set search_path = public
as $$
begin
  if not is_super_admin() then
    raise exception 'super admin only' using errcode = '42501';
  end if;
  delete from excom_rotation_overrides where week_monday = p_week_monday;
end $$;
grant execute on function excom_rotation_override_clear(date) to authenticated;

-- Reset seed date (changes who is "position 1" for this week going forward).
create or replace function excom_rotation_set_seed(p_seed_date date default null)
  returns date
  language plpgsql security definer set search_path = public
as $$
declare v_wm date;
begin
  if not is_super_admin() then
    raise exception 'super admin only' using errcode = '42501';
  end if;
  v_wm := coalesce(p_seed_date, athens_monday());
  insert into app_settings (key, value) values ('excom_rotation_seed_date', v_wm::text)
  on conflict (key) do update set value = excluded.value, updated_at = now();
  return v_wm;
end $$;
grant execute on function excom_rotation_set_seed(date) to authenticated;
