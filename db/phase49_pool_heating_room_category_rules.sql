-- Phase 49 — apply room-category business rules to pool heating dashboard.
--
-- Operations rules from Dimitris (7 May 2026):
--   - Collection Suites (C*) — heated pool INCLUDED in the package, so
--     maintenance has no daily action to take. EXCLUDE from dashboard.
--   - All Villas (V*) — always heated, daily action required by
--     maintenance. INCLUDE on dashboard every day occupied.
--   - Deluxe Sea View Room with individual pool (DLXP) — pool exists but
--     NEVER heatable. EXCLUDE.
--   - Deluxe Junior Suite with individual pool (DJSTEP) — same: never
--     heatable. EXCLUDE.
--   - Premium Junior Suite with Private pool (JSTEP) — CAN be heated on
--     request. Use LLM-extracted comment flag.
--   - One Bedroom Suite with private pool (STEP) — CAN be heated on
--     request. Use LLM-extracted comment flag.
--   - All other room categories — no private pool, no heating concept.
--
-- Important framing: this RPC powers an action-oriented dashboard
-- ("which pools need heating today?"), not a data-truth view of which
-- pools are heated. Collection pools ARE heated; they just don't need
-- to be on the daily maintenance list.
--
-- Why at SQL: separation of concerns. The LLM extraction in `extract.py`
-- is purely text-based ("does the comment mention pool heating?"). The
-- room-category rules are operational defaults. Mixing them would couple
-- the LLM to room-category knowledge that changes more often than the
-- prompt should.
--
-- Replaces: explore_pool_heating from db/phase31_1_pool_heating_dedup.sql.
-- The function signature, return shape, and grouping behaviour are
-- unchanged. The only difference: a CASE expression on
-- room_category_label decides each reservation's effective pool heating
-- before the by_room aggregation.

create or replace function explore_pool_heating(p_start date, p_end date)
returns jsonb
language sql stable security invoker
set search_path = public
as $$
  with reservation_pool_status as (
    -- Phase 49 — room-category-aware effective pool heating.
    select
      ead.*,
      -- "effective_pool_heating" here means "should appear on the
      -- maintenance dashboard", not "is the pool heated". Collection
      -- suites are heated by package default but require no daily
      -- action, so they're excluded.
      case
        -- Villas: always heated, daily action required.
        when upper(coalesce(ead.room_category_label, '')) like 'V%' then true
        -- Collection suites: heated by package, no dashboard action.
        when upper(coalesce(ead.room_category_label, '')) like 'C%' then false
        -- DLXP and DJSTEP: private pool exists but never heatable.
        -- (DJSTE without trailing P = no private pool, falls through
        -- to ELSE — also excluded since there's no pool to heat.)
        when upper(coalesce(ead.room_category_label, '')) in ('DLXP', 'DJSTEP') then false
        -- JSTEP and STEP: heated only if comment mentions it.
        when upper(coalesce(ead.room_category_label, '')) in ('JSTEP', 'STEP')
          then coalesce(ead.ce_pool_heating, false)
        -- All other categories: no private pool, not heated.
        else false
      end as effective_pool_heating
    from explore_arrival_detail ead
  ),
  by_room as (
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
      min(arrival_date)               as arrival,
      max(departure_date)             as departure,
      max(nights)                     as nights,
      max(room_category_label)        as room_category_label,
      bool_or(pool_fence)             as pool_fence,
      bool_or(effective_pool_heating) as pool_heating
    from reservation_pool_status
    where (effective_pool_heating = true or pool_fence = true)
      and arrival_date <= p_end and departure_date >= p_start
    group by room
  )
  select coalesce(jsonb_agg(jsonb_build_object(
    'room',                room,
    'guest_name',          guests[1],
    'guests',              guests,
    'full_names',          full_names,
    'occupants',           array_length(full_names, 1),
    'arrival',             arrival,
    'departure',           departure,
    'nights',              nights,
    'room_category_label', room_category_label,
    'pool_fence',          pool_fence,
    'pool_heating',        pool_heating
  ) order by arrival, room), '[]'::jsonb)
  from by_room;
$$;
grant execute on function explore_pool_heating(date, date) to authenticated;

-- Verify: count rooms in the panel by category bucket. Expected:
-- Villas appear (action required), JSTEP/STEP appear when commented,
-- Collection / DLXP / DJSTEP / Other do NOT appear.
select
  case
    when upper(coalesce(room_category_label, '')) like 'V%' then 'Villa (action required)'
    when upper(coalesce(room_category_label, '')) like 'C%' then 'Collection (SHOULD NOT APPEAR)'
    when upper(coalesce(room_category_label, '')) in ('JSTEP', 'STEP') then 'JSTEP/STEP (commented)'
    when upper(coalesce(room_category_label, '')) in ('DLXP', 'DJSTEP') then 'DLXP/DJSTEP (SHOULD NOT APPEAR)'
    else 'Other (SHOULD NOT APPEAR)'
  end as bucket,
  count(*)
from jsonb_to_recordset(explore_pool_heating(current_date, (current_date + interval '14 days')::date))
  as t(room text, room_category_label text)
group by 1
order by 1;
