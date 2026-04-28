-- Phase 27.5 — Visualisation RPCs.
--
-- Two RPCs that let the super-admin pages render graph data directly:
--
--   graph_room_complaint_sparkline   — daily complaint counts for N rooms
--   graph_visualisation_data         — nodes + edges for the network view
--
-- These are read-only views on top of the Phase 27 graph_* materialized
-- views, so they inherit the nightly-refresh cadence. No new tables.


-- ─── A. Sparkline data: daily complaint counts per room ────────────────
-- Returns one row per (room, day) for the requested rooms over the last
-- N days. Frontend buckets by room and renders a tiny line chart.
create or replace function graph_room_complaint_sparkline(
  p_rooms text[],
  p_days int default 30
) returns jsonb
  language sql stable security definer set search_path = public
as $$
  with days as (
    select generate_series(
      (current_date - (p_days - 1))::date,
      current_date,
      '1 day'::interval
    )::date as day
  ),
  pairs as (
    select r.room, d.day
    from unnest(p_rooms) as r(room)
    cross join days d
  ),
  counts as (
    select
      gc.room,
      gc.note_created_at::date as day,
      count(*) as complaints
    from graph_complaint gc
    where gc.room = any(p_rooms)
      and gc.note_created_at::date >= current_date - (p_days - 1)
    group by 1, 2
  )
  select coalesce(jsonb_agg(jsonb_build_object(
    'room', p.room,
    'day', p.day,
    'complaints', coalesce(c.complaints, 0)
  ) order by p.room, p.day), '[]'::jsonb)
  from pairs p
  left join counts c on c.room = p.room and c.day = p.day;
$$;
grant execute on function graph_room_complaint_sparkline(text[], int) to authenticated;


-- ─── B. Network graph: nodes + edges for force-directed viz ────────────
-- Returns a JSONB document with two top-level keys: 'nodes' and 'edges'.
-- Each node has {id, type, label, stats}. Each edge has {source, target, type}.
--
-- Node types: 'guest' | 'room' | 'ta'
-- Edge types: 'stayed_in' | 'booked_via' | 'complained_about'
--
-- p_focus filters the slice to keep the viz tractable:
--   'all'           — recent stays (last p_window_days)
--   'alister'       — only A-list returners + the rooms/TAs they touched
--   'problem_rooms' — only rooms with >= 2 complaints + their guests/TAs
create or replace function graph_visualisation_data(
  p_window_days int default 30,
  p_focus text default 'all'
) returns jsonb
  language plpgsql stable security definer set search_path = public
as $$
declare
  v_stays jsonb;
  v_complaints jsonb;
  v_nodes jsonb;
  v_edges jsonb;
  v_max_stays int := 200;  -- hard cap to keep the viz usable
begin
  -- 1. Pick the relevant stays.
  with relevant as (
    select gs.*
    from graph_stay gs
    where gs.arrival >= now() - (p_window_days || ' days')::interval
      and (
        p_focus = 'all'
        or (p_focus = 'alister' and gs.is_alister = true)
        or (p_focus = 'problem_rooms' and gs.room in (
              select room from graph_room_quality where complaints_30d >= 2
            ))
      )
    order by gs.arrival desc
    limit v_max_stays
  )
  select jsonb_agg(to_jsonb(r)) into v_stays from relevant r;

  v_stays := coalesce(v_stays, '[]'::jsonb);

  -- 2. Complaints attached to those stays' rooms.
  select jsonb_agg(jsonb_build_object(
    'room', gc.room,
    'guest_key', gc.guest_key,
    'note_created_at', gc.note_created_at
  ))
  into v_complaints
  from graph_complaint gc
  where gc.room in (select jsonb_array_elements(v_stays)->>'room')
    and gc.note_created_at >= now() - (p_window_days || ' days')::interval;

  v_complaints := coalesce(v_complaints, '[]'::jsonb);

  -- 3. Build nodes: distinct guests, rooms, TAs from the stays.
  with stay_nodes as (
    select jsonb_array_elements(v_stays) as s
  ),
  guest_nodes as (
    select distinct
      'guest:' || (s->>'guest_key') as id,
      'guest' as type,
      coalesce(gi.display_name, s->>'guest_name') as label,
      jsonb_build_object(
        'stay_count', gi.stay_count,
        'is_alister', exists(select 1 from graph_alister_loyalty al where al.guest_key = (s->>'guest_key')),
        'country', gi.country
      ) as stats
    from stay_nodes
    left join graph_guest_identity gi on gi.guest_key = (s->>'guest_key')
    where s->>'guest_key' is not null
  ),
  room_nodes as (
    select distinct
      'room:' || (s->>'room') as id,
      'room' as type,
      'Room ' || (s->>'room') as label,
      jsonb_build_object(
        'complaints_30d', rq.complaints_30d,
        'complaints_90d', rq.complaints_90d
      ) as stats
    from stay_nodes
    left join graph_room_quality rq on rq.room = (s->>'room')
    where s->>'room' is not null
  ),
  ta_nodes as (
    select distinct
      'ta:' || (s->>'travel_agent_name') as id,
      'ta' as type,
      (s->>'travel_agent_name') as label,
      jsonb_build_object(
        'stays_30d', tq.stays_30d,
        'alister_stays_90d', tq.alister_stays_90d
      ) as stats
    from stay_nodes
    left join graph_ta_quality tq on tq.travel_agent_name = (s->>'travel_agent_name')
    where s->>'travel_agent_name' is not null
  ),
  all_nodes as (
    select id, type, label, stats from guest_nodes
    union all
    select id, type, label, stats from room_nodes
    union all
    select id, type, label, stats from ta_nodes
  )
  select jsonb_agg(jsonb_build_object(
    'id', id, 'type', type, 'label', label, 'stats', stats
  )) into v_nodes from all_nodes;

  -- 4. Build edges: stayed_in (guest→room), booked_via (guest→ta),
  --    complained_about (guest→room via complaint).
  with stay_rows as (
    select jsonb_array_elements(v_stays) as s
  ),
  stayed_edges as (
    select distinct
      'guest:' || (s->>'guest_key') as source,
      'room:'  || (s->>'room')      as target,
      'stayed_in' as type
    from stay_rows
    where s->>'guest_key' is not null and s->>'room' is not null
  ),
  booked_edges as (
    select distinct
      'guest:' || (s->>'guest_key')           as source,
      'ta:'    || (s->>'travel_agent_name')   as target,
      'booked_via' as type
    from stay_rows
    where s->>'guest_key' is not null and s->>'travel_agent_name' is not null
  ),
  complaint_rows as (
    select jsonb_array_elements(v_complaints) as c
  ),
  complaint_edges as (
    select distinct
      'guest:' || (c->>'guest_key') as source,
      'room:'  || (c->>'room')      as target,
      'complained_about' as type
    from complaint_rows
    where c->>'guest_key' is not null and c->>'room' is not null
  ),
  all_edges as (
    select * from stayed_edges
    union all select * from booked_edges
    union all select * from complaint_edges
  )
  select jsonb_agg(jsonb_build_object(
    'source', source, 'target', target, 'type', type
  )) into v_edges from all_edges;

  return jsonb_build_object(
    'window_days', p_window_days,
    'focus', p_focus,
    'node_count', coalesce(jsonb_array_length(v_nodes), 0),
    'edge_count', coalesce(jsonb_array_length(v_edges), 0),
    'nodes', coalesce(v_nodes, '[]'::jsonb),
    'edges', coalesce(v_edges, '[]'::jsonb)
  );
end $$;
grant execute on function graph_visualisation_data(int, text) to authenticated;
