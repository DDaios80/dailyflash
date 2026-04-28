-- Phase 27 — Knowledge graph for the Cove.
--
-- Postgres-only graph layer using materialized views. Refreshed nightly.
-- Powers the Ask Daios voice assistant's historical / cross-cutting
-- questions ("Has Tijen stayed before?", "Which rooms have recurring
-- complaints?", "Which travel agents bring the most A-listers?").
--
-- Why materialized views, not Neo4j: at Daios Cove's scale (3700
-- reservations, 450 notes) Postgres handles graph traversals in
-- microseconds. Adds zero infra. The `graphify` skill can render
-- a static visual snapshot weekly for super admin discovery; the
-- live queries below power the assistant.

-- ─── 1. Guest identity (dedup across stays) ──────────────────────────────
-- Opera doesn't ship a stable guest_id across confirmations, so we key
-- on normalised name + nationality. Imperfect but workable. Future
-- improvement: match on email/phone if those land in the dataset.
create materialized view if not exists graph_guest_identity as
select
  md5(
    lower(trim(coalesce(guest_name, ''))) || '|' ||
    lower(trim(coalesce(guest_country_desc, '')))
  ) as guest_key,
  -- Display values from the most recent reservation for that key.
  (array_agg(guest_name order by arrival desc nulls last))[1] as display_name,
  (array_agg(guest_country_desc order by arrival desc nulls last))[1] as country,
  count(distinct confirmation_no) as stay_count,
  min(arrival) as first_stay,
  max(arrival) as last_stay,
  array_agg(distinct travel_agent_name) filter (where travel_agent_name is not null) as travel_agents
from reservations
where guest_name is not null
group by 1;

create unique index if not exists graph_guest_identity_pk on graph_guest_identity (guest_key);
create index if not exists graph_guest_identity_name_idx on graph_guest_identity (lower(display_name));


-- ─── 2. Stay node — denormalised reservation row keyed by guest ─────────
create materialized view if not exists graph_stay as
select
  r.id as stay_id,
  r.confirmation_no,
  md5(
    lower(trim(coalesce(r.guest_name, ''))) || '|' ||
    lower(trim(coalesce(r.guest_country_desc, '')))
  ) as guest_key,
  r.guest_name,
  r.guest_country_desc as country,
  r.room,
  r.arrival,
  r.departure,
  (r.departure::date - r.arrival::date) as nights,
  r.travel_agent_name,
  r.source_desc as channel,
  r.vip,
  r.upload_id,
  -- Best-effort A-lister flag
  exists (
    select 1 from alister_findings af
    where af.reservation_id = r.id
      and af.is_notable = true
      and af.review_status in ('confirmed','needs_review')
      and af.confidence >= 85
  ) as is_alister,
  -- Best-effort allergy flag (any comment_extraction with allergy_flag)
  exists (
    select 1 from comment_extractions ce
    where ce.resv_name_id = r.resv_name_id
      and (ce.payload->>'allergy_flag')::boolean is true
  ) as has_allergy
from reservations r
where r.arrival is not null;

create unique index if not exists graph_stay_pk on graph_stay (stay_id);
create index if not exists graph_stay_guest_key_idx on graph_stay (guest_key);
create index if not exists graph_stay_room_idx on graph_stay (room, arrival desc);
create index if not exists graph_stay_ta_idx on graph_stay (travel_agent_name) where travel_agent_name is not null;
create index if not exists graph_stay_arrival_idx on graph_stay (arrival desc);


-- ─── 3. Complaint edges — join zoho_notes to stays via room+date ───────
-- We can't always join via reservation_ref because not every complaint
-- has the confirmation_no. Falls back to room + date overlap match.
create materialized view if not exists graph_complaint as
select
  zn.id as complaint_id,
  zn.source_type,
  zn.room,
  zn.note_created_at,
  zn.resolved_at,
  case
    when zn.resolved_at is not null
    then extract(epoch from (zn.resolved_at - zn.note_created_at)) / 60.0
    else null
  end as resolution_minutes,
  zn.body,
  zn.subject,
  zn.severity,
  -- Best-effort guest lookup: latest stay for that room overlapping the
  -- note date.
  (
    select gs.guest_key
    from graph_stay gs
    where gs.room = zn.room
      and zn.note_created_at::date between gs.arrival::date and gs.departure::date
    order by gs.arrival desc
    limit 1
  ) as guest_key,
  (
    select gs.travel_agent_name
    from graph_stay gs
    where gs.room = zn.room
      and zn.note_created_at::date between gs.arrival::date and gs.departure::date
    order by gs.arrival desc
    limit 1
  ) as travel_agent_at_time
from zoho_notes zn
where zn.source_type in ('pending_complaints','solved_complaints')
  and zn.room is not null;

create unique index if not exists graph_complaint_pk on graph_complaint (complaint_id);
create index if not exists graph_complaint_room_idx on graph_complaint (room, note_created_at desc);
create index if not exists graph_complaint_guest_idx on graph_complaint (guest_key) where guest_key is not null;
create index if not exists graph_complaint_ta_idx on graph_complaint (travel_agent_at_time) where travel_agent_at_time is not null;


-- ─── 4. Room quality aggregate ───────────────────────────────────────────
create materialized view if not exists graph_room_quality as
select
  room,
  count(*) filter (where note_created_at >= now() - interval '30 days') as complaints_30d,
  count(*) filter (where note_created_at >= now() - interval '90 days') as complaints_90d,
  count(distinct guest_key) filter (where note_created_at >= now() - interval '30 days') as distinct_guests_30d,
  max(note_created_at) as last_complaint_at,
  avg(resolution_minutes) filter (where resolved_at is not null and note_created_at >= now() - interval '90 days') as avg_resolution_minutes_90d
from graph_complaint
group by room;

create unique index if not exists graph_room_quality_pk on graph_room_quality (room);
create index if not exists graph_room_quality_30d_idx on graph_room_quality (complaints_30d desc) where complaints_30d > 0;


-- ─── 5. Travel agent quality aggregate ──────────────────────────────────
create materialized view if not exists graph_ta_quality as
with last_30 as (
  select * from graph_stay where arrival >= now() - interval '30 days'
), last_90 as (
  select * from graph_stay where arrival >= now() - interval '90 days'
)
select
  travel_agent_name,
  count(distinct stay_id) filter (where arrival >= now() - interval '30 days') as stays_30d,
  count(distinct stay_id) filter (where arrival >= now() - interval '90 days') as stays_90d,
  count(distinct stay_id) filter (where is_alister and arrival >= now() - interval '90 days') as alister_stays_90d,
  count(distinct stay_id) filter (where has_allergy and arrival >= now() - interval '90 days') as allergy_stays_90d,
  count(distinct stay_id) filter (where vip is not null and arrival >= now() - interval '90 days') as vip_stays_90d,
  count(distinct guest_key) filter (where arrival >= now() - interval '90 days') as unique_guests_90d,
  avg(nights) filter (where arrival >= now() - interval '90 days') as avg_nights_90d
from graph_stay
where travel_agent_name is not null
group by travel_agent_name;

create unique index if not exists graph_ta_quality_pk on graph_ta_quality (travel_agent_name);


-- ─── 6. A-lister loyalty ────────────────────────────────────────────────
create materialized view if not exists graph_alister_loyalty as
select
  gs.guest_key,
  gi.display_name,
  gi.country,
  count(distinct gs.stay_id) as stay_count,
  min(gs.arrival) as first_stay,
  max(gs.arrival) as last_stay,
  array_agg(distinct af.matched_name) as alister_names,
  max(af.confidence) as max_confidence,
  array_agg(distinct af.category) filter (where af.category is not null) as categories
from graph_stay gs
inner join graph_guest_identity gi on gi.guest_key = gs.guest_key
inner join alister_findings af on af.reservation_id = gs.stay_id
where gs.is_alister = true
group by gs.guest_key, gi.display_name, gi.country;

create unique index if not exists graph_alister_loyalty_pk on graph_alister_loyalty (guest_key);
create index if not exists graph_alister_loyalty_count_idx on graph_alister_loyalty (stay_count desc);


-- ─── Refresh function ───────────────────────────────────────────────────
create or replace function refresh_graph_views()
  returns void
  language plpgsql security definer set search_path = public
as $$
begin
  refresh materialized view concurrently graph_guest_identity;
  refresh materialized view concurrently graph_stay;
  refresh materialized view concurrently graph_complaint;
  refresh materialized view concurrently graph_room_quality;
  refresh materialized view concurrently graph_ta_quality;
  refresh materialized view concurrently graph_alister_loyalty;
end $$;
grant execute on function refresh_graph_views() to authenticated;


-- ─── Nightly refresh (after gamification at 03:15) ──────────────────────
do $$ declare j_id bigint;
begin
  select jobid into j_id from cron.job where jobname = 'graph-refresh-nightly';
  if j_id is not null then perform cron.unschedule(j_id); end if;
  -- 01:30 UTC = ~04:30 Athens (after gamification + before morning)
  perform cron.schedule(
    'graph-refresh-nightly',
    '30 1 * * *',
    'select refresh_graph_views();'
  );
end $$;


-- ─── 7. Utility RPCs the voice assistant calls via tool use ────────────

-- 7a. Guest history — has X stayed before?
create or replace function graph_guest_history(
  p_name text,
  p_country text default null
) returns jsonb
  language plpgsql stable security definer set search_path = public
as $$
declare v jsonb;
begin
  with matches as (
    select gi.*, gi.guest_key as gk
    from graph_guest_identity gi
    where lower(gi.display_name) like lower('%' || p_name || '%')
      and (p_country is null or lower(gi.country) like lower('%' || p_country || '%'))
    order by gi.last_stay desc nulls last
    limit 5
  )
  select coalesce(jsonb_agg(jsonb_build_object(
    'name', display_name,
    'country', country,
    'stay_count', stay_count,
    'first_stay', first_stay,
    'last_stay', last_stay,
    'travel_agents', travel_agents,
    'is_alister', exists(select 1 from graph_alister_loyalty al where al.guest_key = matches.gk),
    'recent_stays', (
      select coalesce(jsonb_agg(jsonb_build_object(
        'arrival', arrival, 'departure', departure, 'room', room,
        'travel_agent', travel_agent_name, 'channel', channel
      ) order by arrival desc), '[]'::jsonb)
      from (select * from graph_stay where guest_key = matches.gk
            order by arrival desc limit 5) recent
    )
  )), '[]'::jsonb)
  into v from matches;
  return v;
end $$;
grant execute on function graph_guest_history(text, text) to authenticated;


-- 7b. Room complaint history — recurring problems in a room
create or replace function graph_room_complaints(
  p_room text,
  p_days int default 90
) returns jsonb
  language plpgsql stable security definer set search_path = public
as $$
declare v jsonb;
begin
  select jsonb_build_object(
    'room', p_room,
    'window_days', p_days,
    'complaint_count', (
      select count(*) from graph_complaint
      where room = p_room and note_created_at >= now() - (p_days || ' days')::interval
    ),
    'distinct_guests', (
      select count(distinct guest_key) from graph_complaint
      where room = p_room and note_created_at >= now() - (p_days || ' days')::interval
    ),
    'avg_resolution_minutes', (
      select round(avg(resolution_minutes)::numeric, 0) from graph_complaint
      where room = p_room and resolved_at is not null
        and note_created_at >= now() - (p_days || ' days')::interval
    ),
    'recent_complaints', (
      select coalesce(jsonb_agg(jsonb_build_object(
        'subject', subject,
        'body', left(body, 200),
        'note_created_at', note_created_at,
        'resolved_at', resolved_at,
        'resolution_minutes', round(resolution_minutes::numeric, 0)
      ) order by note_created_at desc), '[]'::jsonb)
      from (select * from graph_complaint
            where room = p_room and note_created_at >= now() - (p_days || ' days')::interval
            order by note_created_at desc limit 10) t
    )
  ) into v;
  return v;
end $$;
grant execute on function graph_room_complaints(text, int) to authenticated;


-- 7c. Travel agent quality
create or replace function graph_ta_overview(
  p_name text,
  p_days int default 90
) returns jsonb
  language plpgsql stable security definer set search_path = public
as $$
declare v jsonb;
begin
  with match as (
    select * from graph_ta_quality
    where lower(travel_agent_name) like lower('%' || p_name || '%')
    limit 5
  )
  select coalesce(jsonb_agg(to_jsonb(m)), '[]'::jsonb) into v from match m;
  return v;
end $$;
grant execute on function graph_ta_overview(text, int) to authenticated;


-- 7d. Top recurring-complaint rooms (proactive insight)
create or replace function graph_recurring_complaint_rooms(
  p_min_count int default 3,
  p_days int default 30
) returns jsonb
  language plpgsql stable security definer set search_path = public
as $$
declare v jsonb;
begin
  select coalesce(jsonb_agg(jsonb_build_object(
    'room', room,
    'complaints', case when p_days <= 30 then complaints_30d else complaints_90d end,
    'distinct_guests', distinct_guests_30d,
    'last_complaint_at', last_complaint_at,
    'avg_resolution_minutes', round(avg_resolution_minutes_90d::numeric, 0)
  ) order by case when p_days <= 30 then complaints_30d else complaints_90d end desc), '[]'::jsonb)
  into v
  from graph_room_quality
  where (case when p_days <= 30 then complaints_30d else complaints_90d end) >= p_min_count;
  return v;
end $$;
grant execute on function graph_recurring_complaint_rooms(int, int) to authenticated;


-- 7e. Top travel agents by A-lister rate
create or replace function graph_top_alister_tas(
  p_min_stays int default 3
) returns jsonb
  language plpgsql stable security definer set search_path = public
as $$
declare v jsonb;
begin
  select coalesce(jsonb_agg(jsonb_build_object(
    'travel_agent', travel_agent_name,
    'stays_90d', stays_90d,
    'alister_stays_90d', alister_stays_90d,
    'alister_rate', round((alister_stays_90d::numeric * 100 / nullif(stays_90d, 0))::numeric, 1)
  ) order by alister_stays_90d desc), '[]'::jsonb)
  into v
  from graph_ta_quality
  where stays_90d >= p_min_stays
    and alister_stays_90d > 0;
  return v;
end $$;
grant execute on function graph_top_alister_tas(int) to authenticated;


-- 7f. A-lister loyalty list
create or replace function graph_alister_returners(
  p_min_stays int default 2
) returns jsonb
  language plpgsql stable security definer set search_path = public
as $$
declare v jsonb;
begin
  select coalesce(jsonb_agg(jsonb_build_object(
    'name', display_name,
    'country', country,
    'stay_count', stay_count,
    'first_stay', first_stay,
    'last_stay', last_stay,
    'alister_categories', categories,
    'max_confidence', max_confidence
  ) order by stay_count desc, last_stay desc), '[]'::jsonb)
  into v
  from graph_alister_loyalty
  where stay_count >= p_min_stays;
  return v;
end $$;
grant execute on function graph_alister_returners(int) to authenticated;


-- ─── First refresh now ─────────────────────────────────────────────────
-- Run this after the migration so the views populate immediately
-- without waiting for the 04:30 cron.
do $$ begin
  perform refresh_graph_views();
exception when others then
  raise notice 'initial refresh failed (will retry tomorrow): %', sqlerrm;
end $$;
