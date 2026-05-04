-- Phase 31.1 (revisit) — pool heating schedule dedupe by room.
--
-- User report: 'Ray appears twice because it is two reservations under the
-- same last name (different first name) that stay in the same villa 224.'
-- Two concurrent Opera reservations for the same family in one room
-- (Mr Ray + Mrs Ray, separate bookings, same window) are listed as
-- separate rows in the heating schedule. Operationally there's one
-- heating job for that room, not two.
--
-- This is distinct from Phase 37's continue-stay case (sequential
-- reservations across a stay extension). Both are dedup cases but
-- with different aggregation semantics.
--
-- Fix: rewrite explore_pool_heating to group by room. Combined guest
-- names are surfaced as a string array so the dashboard can render
-- 'Ray (Mr, Mrs)' or 'Ray + 1 other' as it prefers. Heating window
-- is min(arrival) to max(departure) across all reservations sharing
-- the room.

create or replace function explore_pool_heating(p_start date, p_end date)
returns jsonb
language sql stable security invoker
set search_path = public
as $$
  with by_room as (
    select
      room,
      array_agg(distinct guest_name order by guest_name) as guests,
      array_agg(distinct
        coalesce(
          nullif(trim(coalesce(guest_first_name, '') || ' ' || coalesce(guest_name, '')), ''),
          guest_name
        )
        order by coalesce(
          nullif(trim(coalesce(guest_first_name, '') || ' ' || coalesce(guest_name, '')), ''),
          guest_name
        )
      ) as full_names,
      min(arrival_date)   as arrival,
      max(departure_date) as departure,
      max(nights)         as nights,
      bool_or(pool_fence)      as pool_fence,
      bool_or(ce_pool_heating) as pool_heating
    from explore_arrival_detail
    where (ce_pool_heating = true or pool_fence = true)
      and arrival_date <= p_end and departure_date >= p_start
    group by room
  )
  select coalesce(jsonb_agg(jsonb_build_object(
    'room',           room,
    'guest_name',     guests[1],         -- primary last-name (back-compat for existing UI)
    'guests',         guests,            -- distinct last-names array
    'full_names',     full_names,        -- distinct full-name array
    'occupants',      array_length(full_names, 1),
    'arrival',        arrival,
    'departure',      departure,
    'nights',         nights,
    'pool_fence',     pool_fence,
    'pool_heating',   pool_heating
  ) order by arrival, room), '[]'::jsonb)
  from by_room;
$$;
grant execute on function explore_pool_heating(date, date) to authenticated;
