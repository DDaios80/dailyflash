-- Phase 36 — Surface kid ages + cribs in the Explore arrival-detail view.
--
-- User request: 'I want to add as well the number of kids and the ages of
-- the kids to be seen on the dashboard.' Operations need this for
-- kids-club staffing, restaurant high-chair allocation, breakfast counts,
-- spa childcare planning, beach-cabana priority, etc.
--
-- The data already lives on reservations:
--   - children    int     (count)
--   - ages        text    (Opera's free-text list, e.g. "3, 7" or "3/7/10")
--   - cribs       int     (number of cribs requested)
--
-- The Phase 31 explore_arrival_detail view already exposed `children` and
-- `pax`. This phase adds `children_ages` and `cribs` so the dashboard
-- can render guest cards with the full party composition.
--
-- Note: CREATE OR REPLACE VIEW only allows ADDING columns at the END of
-- the column list — you can't insert in the middle. Since we want the
-- new fields next to `children` semantically, we DROP and recreate.
-- Safe: the view has no dependent objects (no other view, RPC, or table
-- references explore_arrival_detail; the dashboard reads by column name).

drop view if exists explore_arrival_detail;

create view explore_arrival_detail as
select
  r.id as reservation_id,
  r.resv_name_id,
  r.arrival::date as arrival_date,
  r.departure::date as departure_date,
  (r.departure::date - r.arrival::date) as nights,
  r.room,
  r.room_category_label,
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
  -- Phase 36 — kid ages + cribs
  r.ages as children_ages,           -- raw Opera text, e.g. "3, 7"
  coalesce(r.cribs, 0) as cribs,
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
