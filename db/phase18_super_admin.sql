-- Phase 18 — Super admin dashboard.
--
-- Adds a single super admin role (distinct from regular admin), a
-- platform_events tracking table, and aggregation RPCs powering the
-- /super-admin dashboard's three tabs: Overview, Usage, Health.
--
-- Scope note: this is observability, not permissions. Regular admin
-- operations (user management, settings) remain admin-gated. Super
-- admin adds a read-only platform-wide view on top.

-- ─── Super admin identity ──────────────────────────────────────────────────
-- Stored in app_settings as a user_id (not an email) so it survives email
-- changes. Seed is the one account requested: d.daios@daioshotels.com.
-- The user_id is looked up at apply-time from auth.users.
do $$
declare
  v_user_id uuid;
begin
  select id into v_user_id from auth.users
  where email = 'd.daios@daioshotels.com' limit 1;

  if v_user_id is not null then
    insert into app_settings (key, value)
    values ('super_admin_user_id', v_user_id::text)
    on conflict (key) do update set value = excluded.value, updated_at = now();
  else
    raise notice 'd.daios@daioshotels.com not found in auth.users — super_admin_user_id not seeded. Admin can set it later.';
  end if;
end $$;

create or replace function is_super_admin()
  returns boolean
  language sql stable security definer set search_path = public
as $$
  select coalesce(
    (select value::uuid from app_settings where key = 'super_admin_user_id')
      = auth.uid(),
    false
  );
$$;
grant execute on function is_super_admin() to authenticated;


-- ─── Platform events (tracking) ────────────────────────────────────────────
do $$ begin
  create type platform_event_type as enum (
    'page_view',
    'click',
    'submit',
    'auth',
    'error',
    'custom'
  );
exception when duplicate_object then null; end $$;

create table if not exists platform_events (
  id           uuid primary key default gen_random_uuid(),
  user_id      uuid references auth.users(id) on delete set null,
  session_id   text,
  event_type   platform_event_type not null,
  path         text,
  target       text,          -- CTA name or error source (e.g. 'new-idea', 'submit-briefing', 'ErrorBoundary:Dashboard')
  metadata     jsonb not null default '{}'::jsonb,
  created_at   timestamptz not null default now()
);

-- Hot-path indexes for the super-admin aggregations
create index if not exists platform_events_created_idx
  on platform_events (created_at desc);
create index if not exists platform_events_user_created_idx
  on platform_events (user_id, created_at desc)
  where user_id is not null;
create index if not exists platform_events_type_idx
  on platform_events (event_type, created_at desc);
create index if not exists platform_events_path_idx
  on platform_events (path, created_at desc)
  where path is not null;


-- ─── RLS ───────────────────────────────────────────────────────────────────
alter table platform_events enable row level security;

-- Users can write their own events (called from frontend)
drop policy if exists insert_own_events on platform_events;
create policy insert_own_events on platform_events
  for insert with check (
    auth.uid() is not null
    and (user_id is null or user_id = auth.uid())
  );

-- Only super admin reads. Regular admin is intentionally excluded —
-- user browsing behaviour is personal data and should not be visible
-- even to fellow admins.
drop policy if exists read_events_super_admin on platform_events;
create policy read_events_super_admin on platform_events
  for select using (is_super_admin());


-- ─── Track RPC (frontend calls this on every page view / CTA click) ───────
create or replace function track_event(
  p_session_id  text,
  p_event_type  platform_event_type,
  p_path        text default null,
  p_target      text default null,
  p_metadata    jsonb default '{}'::jsonb
) returns void
  language plpgsql security invoker
as $$
begin
  if auth.uid() is null then return; end if;  -- silently skip for unauth
  insert into platform_events (user_id, session_id, event_type, path, target, metadata)
  values (auth.uid(), p_session_id, p_event_type, p_path, p_target, coalesce(p_metadata, '{}'::jsonb));
end $$;
grant execute on function track_event(text, platform_event_type, text, text, jsonb) to authenticated;


-- ─── Aggregation RPC: overview tab ────────────────────────────────────────
create or replace function super_admin_overview()
  returns jsonb
  language plpgsql stable security definer set search_path = public
as $$
declare
  v_result jsonb;
  v_today_start timestamptz := (date_trunc('day', now() at time zone 'Europe/Athens') at time zone 'Europe/Athens');
  v_24h_ago    timestamptz := now() - interval '24 hours';
  v_7d_ago     timestamptz := now() - interval '7 days';
begin
  if not is_super_admin() then
    raise exception 'super admin only';
  end if;

  select jsonb_build_object(
    'today_active_users', (
      select count(distinct user_id) from platform_events
      where user_id is not null and created_at >= v_today_start
    ),
    'today_events', (
      select count(*) from platform_events
      where created_at >= v_today_start
    ),
    'pending_approvals', (
      select count(*) from flash_email_approvals
      where approved_at is null and rejected_at is null
    ),
    'errors_24h', (
      select count(*) from platform_events
      where event_type = 'error' and created_at >= v_24h_ago
    ),
    'last_pipeline_run', (
      select jsonb_build_object(
        'computed_at', computed_at,
        'report_date', report_date,
        'sa_arrivals',
          jsonb_array_length(coalesce(payload->'special_attention_arrivals','[]'::jsonb))
      )
      from flash_reports order by computed_at desc limit 1
    ),
    'tonight_email', (
      select jsonb_build_object(
        'report_date', report_date,
        'preview_sent_at', preview_sent_at,
        'approved_at', approved_at,
        'rejected_at', rejected_at,
        'fanned_out_at', fanned_out_at
      )
      from flash_email_approvals
      order by preview_sent_at desc nulls last limit 1
    ),
    'top_users_7d', (
      select jsonb_agg(row)
      from (
        select jsonb_build_object(
          'user_id', u.id,
          'display_name', coalesce(u.raw_user_meta_data ->> 'full_name', u.email),
          'events', cnt
        ) as row
        from (
          select user_id, count(*) as cnt
          from platform_events
          where user_id is not null and created_at >= v_7d_ago
          group by user_id
          order by cnt desc
          limit 10
        ) pe
        inner join auth.users u on u.id = pe.user_id
      ) t
    )
  ) into v_result;

  return v_result;
end $$;
grant execute on function super_admin_overview() to authenticated;


-- ─── Aggregation RPC: usage & behaviour tab ───────────────────────────────
create or replace function super_admin_usage(p_days int default 30)
  returns jsonb
  language plpgsql stable security definer set search_path = public
as $$
declare
  v_result jsonb;
  v_since  timestamptz := now() - (p_days || ' days')::interval;
begin
  if not is_super_admin() then
    raise exception 'super admin only';
  end if;

  select jsonb_build_object(
    'window_days', p_days,
    'dau_7d', (
      select jsonb_agg(row order by day desc)
      from (
        select
          (created_at at time zone 'Europe/Athens')::date as day,
          count(distinct user_id) as users
        from platform_events
        where user_id is not null and created_at >= now() - interval '7 days'
        group by day
      ) daily,
      lateral (select jsonb_build_object('day', day, 'users', users) as row) x
    ),
    'feature_adoption', (
      select jsonb_agg(row)
      from (
        select jsonb_build_object(
          'feature', feature,
          'users_30d', (
            select count(distinct user_id) from platform_events
            where user_id is not null
              and created_at >= v_since
              and (path = feature_path or (target is not null and target = feature_target))
          )
        ) as row
        from (values
          ('Dashboard',      '/dashboard',   null),
          ('My dashboard',   '/my',          null),
          ('Ideas',          '/ideas',       null),
          ('FAM trips',      '/fam-trips',   null),
          ('Site inspections','/site-inspections', null),
          ('Groups',         '/groups',      null),
          ('Calendar',       '/calendar',    null),
          ('Admin',          '/admin',       null),
          ('Submit idea',    null,           'new-idea'),
          ('New FAM trip',   null,           'new-fam-trip'),
          ('Save briefing',  null,           'submit-briefing')
        ) as features(feature, feature_path, feature_target)
      ) t
    ),
    'inactive_users', (
      select jsonb_agg(row)
      from (
        select jsonb_build_object(
          'user_id', u.id,
          'email', u.email,
          'display_name', coalesce(u.raw_user_meta_data ->> 'full_name', u.email),
          'last_sign_in', u.last_sign_in_at,
          'last_event', (
            select max(created_at) from platform_events
            where user_id = u.id
          )
        ) as row
        from auth.users u
        where u.last_sign_in_at is null or u.last_sign_in_at < now() - interval '7 days'
        order by u.last_sign_in_at asc nulls first
        limit 50
      ) t
    ),
    'top_pages', (
      select jsonb_agg(row order by views desc)
      from (
        select
          path,
          count(*) as views,
          count(distinct user_id) as unique_users
        from platform_events
        where event_type = 'page_view'
          and created_at >= v_since
          and path is not null
        group by path
        order by views desc
        limit 15
      ) top,
      lateral (select jsonb_build_object('path', path, 'views', views, 'unique_users', unique_users) as row) x
    )
  ) into v_result;

  return v_result;
end $$;
grant execute on function super_admin_usage(int) to authenticated;


-- ─── Aggregation RPC: system health tab ───────────────────────────────────
create or replace function super_admin_health()
  returns jsonb
  language plpgsql stable security definer set search_path = public
as $$
declare
  v_result jsonb;
  v_24h_ago    timestamptz := now() - interval '24 hours';
  v_7d_ago     timestamptz := now() - interval '7 days';
  v_30d_ago    timestamptz := now() - interval '30 days';
begin
  if not is_super_admin() then
    raise exception 'super admin only';
  end if;

  select jsonb_build_object(
    'pipeline', jsonb_build_object(
      'last_run_at', (select computed_at from flash_reports order by computed_at desc limit 1),
      'last_report_date', (select report_date from flash_reports order by computed_at desc limit 1),
      'runs_last_7d', (
        select count(*) from flash_reports where computed_at >= v_7d_ago
      )
    ),
    'upload_fallback', jsonb_build_object(
      'fired_7d', (
        select count(*) from upload_fallback_fires
        where sent_at >= v_7d_ago and status = 'sent'
      ),
      'skipped_7d', (
        select count(*) from upload_fallback_fires
        where sent_at >= v_7d_ago and status = 'skipped'
      ),
      'last_event', (
        select jsonb_build_object(
          'sent_date', sent_date,
          'status', status,
          'recipient', recipient,
          'skip_reason', skip_reason
        )
        from upload_fallback_fires order by sent_at desc limit 1
      )
    ),
    'email_delivery', jsonb_build_object(
      'sent_30d', (select count(*) from email_deliveries where sent_at >= v_30d_ago and status = 'sent'),
      'failed_30d', (select count(*) from email_deliveries where sent_at >= v_30d_ago and status = 'failed'),
      'latest_failures', (
        select jsonb_agg(row)
        from (
          select jsonb_build_object(
            'sent_at', sent_at,
            'recipient', recipient_email,
            'error', error
          ) as row
          from email_deliveries
          where status = 'failed' and sent_at >= v_30d_ago
          order by sent_at desc limit 10
        ) t
      )
    ),
    'fam_trip_parses', jsonb_build_object(
      'total_with_pdf', (select count(*) from fam_trips where pdf_path is not null),
      'parsed_ok', (select count(*) from fam_trips where itinerary_by_day is not null and itinerary_parse_error is null),
      'parse_errors', (select count(*) from fam_trips where itinerary_parse_error is not null),
      'latest_errors', (
        select jsonb_agg(row)
        from (
          select jsonb_build_object(
            'name', name,
            'error', itinerary_parse_error,
            'at', itinerary_parsed_at
          ) as row
          from fam_trips
          where itinerary_parse_error is not null
          order by itinerary_parsed_at desc nulls last limit 10
        ) t
      )
    ),
    'recent_errors', (
      select jsonb_agg(row)
      from (
        select jsonb_build_object(
          'at', created_at,
          'user_id', user_id,
          'path', path,
          'target', target,
          'metadata', metadata
        ) as row
        from platform_events
        where event_type = 'error' and created_at >= v_7d_ago
        order by created_at desc limit 50
      ) t
    )
  ) into v_result;

  return v_result;
end $$;
grant execute on function super_admin_health() to authenticated;


-- ─── Convenience: raw activity log query for the Overview tab's drawer ───
create or replace function super_admin_activity(p_limit int default 100, p_offset int default 0)
  returns table (
    event_id     uuid,
    user_id      uuid,
    user_email   text,
    session_id   text,
    event_type   text,
    path         text,
    target       text,
    metadata     jsonb,
    created_at   timestamptz
  )
  language sql stable security definer set search_path = public
as $$
  select
    e.id,
    e.user_id,
    u.email,
    e.session_id,
    e.event_type::text,
    e.path,
    e.target,
    e.metadata,
    e.created_at
  from platform_events e
  left join auth.users u on u.id = e.user_id
  where is_super_admin()
  order by e.created_at desc
  limit greatest(1, least(p_limit, 500))
  offset greatest(0, p_offset);
$$;
grant execute on function super_admin_activity(int, int) to authenticated;
