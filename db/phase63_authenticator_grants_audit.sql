-- Phase 63 — defensive authenticator grants audit (2026-05-14)
--
-- Audit finding: 108 functions in public schema have `grant execute ... to
-- authenticated` but NOT to `authenticator`. They currently work only because
-- of implicit Postgres/Supabase default grants on the public schema. If those
-- defaults change (project re-creation, security hardening, Supabase config
-- changes), ALL 108 RPCs could fail PostgREST cache discovery simultaneously
-- — the Phase 51 bug at 108x scale.
--
-- Phase 51's convention for new grants:
--   grant execute on function <fn>(...) to authenticator, anon, authenticated, service_role;
-- That convention wasn't applied to functions defined before Phase 51.
--
-- This migration is DEFENSIVE: it walks pg_proc and re-grants EXECUTE on every
-- public non-trigger function (excluding underscore-prefixed internal helpers)
-- to all four PostgREST-relevant roles. Idempotent — safe to re-run anytime.
--
-- Apply via Supabase SQL editor. Listen to the verification output at the end
-- to confirm grant counts match across the four roles.

-- ─── Bulk grant on all qualifying public functions ──────────────────────────

do $$
declare
  r record;
  v_count int := 0;
begin
  for r in
    select
      n.nspname,
      p.proname,
      pg_get_function_identity_arguments(p.oid) as args
    from pg_proc p
    join pg_namespace n on n.oid = p.pronamespace
    where n.nspname = 'public'
      and p.prorettype <> 'trigger'::regtype  -- skip trigger functions
      and p.proname not like '\_%' escape '\'  -- skip _internal helpers
  loop
    execute format(
      'grant execute on function %I.%I(%s) to authenticator, anon, authenticated, service_role',
      r.nspname, r.proname, r.args
    );
    v_count := v_count + 1;
  end loop;

  raise notice 'Phase 63: granted execute on % public functions to authenticator/anon/authenticated/service_role', v_count;
end $$;

-- ─── Re-grant on the Phase 36 view (idempotent, already in Phase 51) ────────

grant select on public.explore_arrival_detail
  to authenticator, anon, authenticated, service_role;

-- ─── Force PostgREST cache reload ───────────────────────────────────────────

notify pgrst, 'reload schema';

-- ─── Verification: count grants by role ─────────────────────────────────────
-- All four PostgREST roles should show identical executable_functions counts.
-- If authenticator's count is lower than the others, something went wrong.

select
  r.rolname,
  count(p.oid) filter (where has_function_privilege(r.oid, p.oid, 'execute')) as executable_functions
from pg_roles r
cross join pg_proc p
join pg_namespace n on n.oid = p.pronamespace
where r.rolname in ('authenticator', 'anon', 'authenticated', 'service_role')
  and n.nspname = 'public'
  and p.prorettype <> 'trigger'::regtype
  and p.proname not like '\_%' escape '\'
group by r.rolname
order by r.rolname;

-- Expected output: 4 rows, all with the same number (~115 public functions).
-- If you see a row missing or a count anomaly, investigate before deploying.
