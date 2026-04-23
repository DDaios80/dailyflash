-- Phase 14 — Groups table + unified events calendar helper.
--
-- Adds a third event type ("Groups") alongside FAM trips and site
-- inspections, plus a `list_events_in_range()` RPC that returns a
-- unified row shape for the calendar UI.
--
-- Groups are hotel-managed group bookings: weddings, corporate retreats,
-- incentives, conferences. Distinct from Opera rate-group bookings.
-- No approval workflow for MVP — just a simple operational record with
-- a status lifecycle (planned -> confirmed -> in_progress -> completed /
-- cancelled).

-- ─── Enums ──────────────────────────────────────────────────────────────────
do $$ begin
  create type group_type as enum (
    'wedding', 'corporate', 'incentive', 'conference', 'other'
  );
exception when duplicate_object then null; end $$;

do $$ begin
  create type group_status as enum (
    'planned', 'confirmed', 'in_progress', 'completed', 'cancelled'
  );
exception when duplicate_object then null; end $$;


-- ─── Groups table ──────────────────────────────────────────────────────────
create table if not exists groups (
  id uuid primary key default gen_random_uuid(),

  name                   text not null check (length(name) between 1 and 200),
  type                   group_type not null default 'other',
  start_date             date not null,
  end_date               date not null,
  total_pax              int,

  -- Primary contact (usually the client-side coordinator)
  primary_contact_name   text,
  primary_contact_email  text,
  primary_contact_phone  text,

  -- Internal owner(s) at the hotel
  in_charge              text,

  notes                  text,
  status                 group_status not null default 'planned',

  -- Attribution
  created_by_user_id     uuid references auth.users(id),
  created_by_name        text,

  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),

  check (end_date >= start_date)
);

create index if not exists groups_date_range_idx on groups (start_date, end_date);
create index if not exists groups_status_idx     on groups (status, start_date);
create index if not exists groups_type_idx       on groups (type, start_date);


-- ─── updated_at trigger ────────────────────────────────────────────────────
create or replace function _touch_groups_updated_at()
  returns trigger language plpgsql as
$$ begin new.updated_at := now(); return new; end $$;

drop trigger if exists groups_touch on groups;
create trigger groups_touch
  before update on groups
  for each row execute function _touch_groups_updated_at();


-- ─── RLS ───────────────────────────────────────────────────────────────────
alter table groups enable row level security;

drop policy if exists read_groups on groups;
create policy read_groups on groups
  for select using (auth.role() = 'authenticated');

drop policy if exists insert_groups on groups;
create policy insert_groups on groups
  for insert with check (
    current_user_role() in ('admin','management','guest_relations','sales')
    and (created_by_user_id = auth.uid() or created_by_user_id is null)
  );

drop policy if exists update_groups on groups;
create policy update_groups on groups
  for update using (
    current_user_role() in ('admin','management','guest_relations','sales')
  );

drop policy if exists delete_groups_admin on groups;
create policy delete_groups_admin on groups
  for delete using (can_admin());


-- ─── Unified events RPC for the calendar ───────────────────────────────────
-- Returns FAM trips + site inspections + groups overlapping the window as
-- a single row shape the calendar UI can render directly.
--
-- Event types:
--   'fam_trip'         — spans start_date..end_date
--   'site_inspection'  — single day (inspection_date == start == end)
--   'group'            — spans start_date..end_date
--
-- Status is the native table status, cast to text so the consumer sees
-- the same strings regardless of source enum.
create or replace function list_events_in_range(p_start date, p_end date)
  returns table (
    event_type text,
    event_id   uuid,
    name       text,
    subtype    text,   -- group.type, or null for other event types
    start_date date,
    end_date   date,
    status     text,
    pax        int,
    in_charge  text
  )
  language sql stable security invoker
as $$
  -- FAM trips
  select
    'fam_trip'     as event_type,
    ft.id          as event_id,
    ft.name,
    null::text     as subtype,
    ft.start_date,
    ft.end_date,
    ft.status::text,
    ft.total_pax   as pax,
    ft.in_charge
  from fam_trips ft
  where ft.start_date <= p_end and ft.end_date >= p_start

  union all

  -- Site inspections (single-day)
  select
    'site_inspection' as event_type,
    si.id             as event_id,
    coalesce(si.agency_contact_person, si.travel_agency, 'Site inspection') as name,
    si.reason_of_visit::text as subtype,
    si.inspection_date as start_date,
    si.inspection_date as end_date,
    si.status::text,
    si.number_of_persons as pax,
    si.inspection_performed_by as in_charge
  from site_inspections si
  where si.inspection_date between p_start and p_end

  union all

  -- Groups
  select
    'group'        as event_type,
    g.id           as event_id,
    g.name,
    g.type::text   as subtype,
    g.start_date,
    g.end_date,
    g.status::text,
    g.total_pax    as pax,
    g.in_charge
  from groups g
  where g.start_date <= p_end and g.end_date >= p_start

  order by start_date, event_type;
$$;
grant execute on function list_events_in_range(date, date) to authenticated;
