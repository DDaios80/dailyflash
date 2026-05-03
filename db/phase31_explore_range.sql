-- Phase 31 — Forward-looking Explore view for operational planning.
--
-- Use case: GR / FO / F&B want to see arrivals across a date range (next
-- 7-30 days) so they can pre-stage rooms, plan breakfast counts by guest
-- type, schedule pool heating, and prepare bad-weather contingencies.
-- Without this view they have to log into Opera daily.
--
-- Surfaces:
--   1. `explore_arrival_detail` view — flat per-reservation row joining
--      reservations + comment_extractions, with `room_bucket` derived
--      (Standard / Suite / Collection / Villa). Dashboard can `select *`
--      with date range filters for the per-room detail view.
--
--   2. `explore_summary(p_start, p_end)` RPC — daily-rolled aggregates
--      across the range. One JSONB row per day. Lightweight; powers the
--      summary tiles + sparklines without N+1 reservation reads.
--
-- Both forward-looking — they read reservations directly, so future
-- arrivals are visible (flash_reports only has today's snapshot).

-- ─── 1. Per-reservation flat view ─────────────────────────────────────
create or replace view explore_arrival_detail as
select
  r.id as reservation_id,
  r.resv_name_id,
  r.arrival::date as arrival_date,
  r.departure::date as departure_date,
  (r.departure::date - r.arrival::date) as nights,
  r.room,
  r.room_category_label,
  -- Same bucket heuristic as src/compute.py::_room_bucket. Mirrored here
  -- so the dashboard can group rows without a roundtrip to the pipeline.
  case
    when r.room_category_label is null or r.room_category_label = '' then 'Unknown'
    when upper(r.room_category_label) like 'V%' then 'Villa'
    when upper(r.room_category_label) like 'C%' then 'Collection'
    when upper(r.room_category_label) like '%STE%'
      or upper(r.room_category_label) like '%JST%'
      or upper(r.room_category_label) like '%STP'
      then 'Suite'
    else 'Standard'
  end as room_bucket,
  r.guest_first_name,
  r.guest_name,
  r.guest_country_desc as nationality,
  coalesce(r.adults, 0) as adults,
  coalesce(r.children, 0) as children,
  coalesce(r.pax, 0) as pax,
  (coalesce(r.children, 0) = 0) as is_adults_only,
  r.travel_agent_name,
  r.group_name,
  r.market_desc,
  r.complimentary_yn,
  r.special_requests,
  r.vip,
  r.guest_vip_desc,
  r.comments,
  ce.allergies_present,
  ce.allergies_text,
  ce.honeymoon,
  ce.free_upgrade,
  ce.pool_fence,
  ce.pool_heating as ce_pool_heating,
  ce.amenities,
  ce.ops_notes
from reservations r
left join comment_extractions ce on ce.reservation_id = r.id
where r.arrival is not null;

grant select on explore_arrival_detail to authenticated;


-- ─── 2. Daily-summary RPC for a date range ────────────────────────────
-- One row per day in [p_start, p_end]. Useful for headline tiles and
-- sparklines. Queries the view above so the bucket logic stays DRY.
create or replace function explore_summary(p_start date, p_end date)
returns jsonb
language sql stable security invoker
set search_path = public
as $$
  with days as (
    select generate_series(p_start, p_end, '1 day'::interval)::date as d
  ),
  arrivals as (
    select
      arrival_date as d,
      count(*) as total,
      count(*) filter (where is_adults_only) as adults_only_rooms,
      sum(pax) filter (where is_adults_only) as adults_only_guests,
      count(*) filter (where not is_adults_only) as family_rooms,
      sum(pax) filter (where not is_adults_only) as family_guests,
      count(*) filter (where room_bucket = 'Standard') as standard_rooms,
      count(*) filter (where room_bucket = 'Suite') as suite_rooms,
      count(*) filter (where room_bucket = 'Collection') as collection_rooms,
      count(*) filter (where room_bucket = 'Villa') as villa_rooms,
      count(*) filter (where allergies_present) as allergy_rooms,
      count(*) filter (where honeymoon) as honeymoon_rooms,
      count(*) filter (where free_upgrade) as free_upgrade_rooms,
      count(*) filter (where pool_fence) as pool_fence_rooms,
      count(*) filter (where ce_pool_heating) as pool_heating_rooms,
      count(*) filter (where vip is not null and vip <> '') as vip_rooms
    from explore_arrival_detail
    where arrival_date between p_start and p_end
    group by 1
  ),
  departures as (
    select departure_date as d, count(*) as total
    from explore_arrival_detail
    where departure_date between p_start and p_end
    group by 1
  )
  select coalesce(jsonb_agg(
    jsonb_build_object(
      'date', days.d,
      'arrivals', coalesce(arrivals.total, 0),
      'departures', coalesce(departures.total, 0),
      'adults_only_rooms', coalesce(arrivals.adults_only_rooms, 0),
      'adults_only_guests', coalesce(arrivals.adults_only_guests, 0),
      'family_rooms', coalesce(arrivals.family_rooms, 0),
      'family_guests', coalesce(arrivals.family_guests, 0),
      'rooms_by_category', jsonb_build_object(
        'Standard', coalesce(arrivals.standard_rooms, 0),
        'Suite', coalesce(arrivals.suite_rooms, 0),
        'Collection', coalesce(arrivals.collection_rooms, 0),
        'Villa', coalesce(arrivals.villa_rooms, 0)
      ),
      'flags', jsonb_build_object(
        'allergy_rooms', coalesce(arrivals.allergy_rooms, 0),
        'honeymoon_rooms', coalesce(arrivals.honeymoon_rooms, 0),
        'free_upgrade_rooms', coalesce(arrivals.free_upgrade_rooms, 0),
        'pool_fence_rooms', coalesce(arrivals.pool_fence_rooms, 0),
        'pool_heating_rooms', coalesce(arrivals.pool_heating_rooms, 0),
        'vip_rooms', coalesce(arrivals.vip_rooms, 0)
      )
    ) order by days.d
  ), '[]'::jsonb)
  from days
  left join arrivals on arrivals.d = days.d
  left join departures on departures.d = days.d;
$$;
grant execute on function explore_summary(date, date) to authenticated;


-- ─── 3. Pool heating schedule for a date range ────────────────────────
-- Rooms whose extracted comments mention pool heating, with their stay
-- window. F&B / engineering use this to schedule heating before arrival.
create or replace function explore_pool_heating(p_start date, p_end date)
returns jsonb
language sql stable security invoker
set search_path = public
as $$
  select coalesce(jsonb_agg(jsonb_build_object(
    'room', room,
    'guest_name', guest_name,
    'arrival', arrival_date,
    'departure', departure_date,
    'nights', nights,
    'pool_fence', pool_fence,
    'pool_heating', ce_pool_heating
  ) order by arrival_date, room), '[]'::jsonb)
  from explore_arrival_detail
  where (ce_pool_heating = true or pool_fence = true)
    and (
      (arrival_date <= p_end and departure_date >= p_start)
    );
$$;
grant execute on function explore_pool_heating(date, date) to authenticated;
