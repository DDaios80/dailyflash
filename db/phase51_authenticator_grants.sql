-- Phase 51 — fix PostgREST schema cache visibility by granting access
-- to the authenticator role for objects PostgREST should expose.
--
-- Root cause discovered 2026-05-08: PostgREST connects to Postgres as
-- the `authenticator` role and uses it to walk the schema for cache
-- discovery. If `authenticator` can't SELECT a view (or EXECUTE a
-- function), PostgREST excludes it from the cache — and any dependent
-- function fails the same way via dependency walk.
--
-- The Phase 36 migration (db/phase36_kid_ages_in_explore.sql) created
-- explore_arrival_detail with only `grant select on ... to authenticated`.
-- Older migrations got authenticator access via Postgres defaults at
-- the time, but this one slipped. Phase 49's explore_pool_heating
-- function inherited the visibility issue because it selects from the
-- view.
--
-- Fix: grant the four PostgREST-relevant roles access to all RPCs and
-- views in our codebase, idempotent. Going forward, every new view /
-- RPC migration must include all four roles in the GRANT.
--
-- Convention for future migrations:
--   grant select on <view>     to authenticator, anon, authenticated, service_role;
--   grant execute on function <fn>(...) to authenticator, anon, authenticated, service_role;

-- ─── Phase 36 view (the one that broke) ──────────────────────────────────
grant select on public.explore_arrival_detail
  to authenticator, anon, authenticated, service_role;

-- ─── Phase 49 function (depends on the view above) ──────────────────────
grant execute on function public.explore_pool_heating(date, date)
  to authenticator, anon, authenticated, service_role;

-- ─── Force PostgREST cache reload ───────────────────────────────────────
notify pgrst, 'reload schema';

-- ─── Verify all roles can access the objects PostgREST exposes ──────────
select
  rolname,
  has_table_privilege(rolname, 'public.explore_arrival_detail', 'select') as view_select,
  has_function_privilege(rolname, 'public.explore_pool_heating(date, date)', 'execute') as fn_exec
from pg_roles
where rolname in ('authenticator', 'service_role', 'anon', 'authenticated')
order by rolname;
