-- Phase 64 — close the reject-path audit-trail gap (2026-05-14)
--
-- Follow-up to the UPDATE statement code-review audit
-- (docs/2026-05-14-update-statement-audit.md). The audit found that
-- admin_review_fam_trip / admin_review_inspection / admin_review_group
-- reject branches set `status='rejected', rejected_at=now(), rejection_reason`
-- but DO NOT set `rejected_by_user_id` — and the column doesn't even
-- exist on any of the three tables. If admin A rejects a fam trip, the
-- system records the originally-assigned approver (set at submit time),
-- not the actual rejecter.
--
-- This migration:
--   1. Adds `rejected_by_user_id` column to fam_trips, site_inspections, groups
--   2. Patches the three admin_review_* RPCs to write the new column atomically
--   3. Re-grants execute on the patched functions per Phase 51 convention
--
-- Idempotent. Safe to re-run. Historical reject rows are left with NULL
-- in the new column (no backfill — we can't reconstruct who rejected
-- them retroactively).
--
-- Apply via Supabase SQL editor.

-- ─── 1. Add rejected_by_user_id columns ─────────────────────────────────

alter table fam_trips
  add column if not exists rejected_by_user_id uuid references auth.users(id);

alter table site_inspections
  add column if not exists rejected_by_user_id uuid references auth.users(id);

alter table groups
  add column if not exists rejected_by_user_id uuid references auth.users(id);

-- ─── 2. Patch admin_review_fam_trip ─────────────────────────────────────

create or replace function admin_review_fam_trip(
  p_trip_id    uuid,
  p_action     text,                 -- 'approve' | 'reject'
  p_reason     text default null     -- optional rejection reason
) returns jsonb
  language plpgsql security definer set search_path = public
as $$
declare
  v_trip fam_trips%rowtype;
  v_role text;
begin
  if p_action not in ('approve','reject') then
    raise exception 'p_action must be approve or reject (got %)', p_action;
  end if;

  -- Caller must be admin or management
  select ur.role into v_role from user_roles ur where ur.user_id = auth.uid();
  if v_role not in ('admin','management') then
    raise exception 'not allowed — admin or management role required';
  end if;

  select * into v_trip from fam_trips where id = p_trip_id;
  if not found then raise exception 'fam trip not found'; end if;
  if v_trip.status not in ('pending_approval') then
    raise exception 'only pending_approval trips can be reviewed (current: %)', v_trip.status;
  end if;

  if p_action = 'approve' then
    update fam_trips set
      status = 'approved',
      approved_at = now(),
      approver_user_id = auth.uid()  -- record who actually approved
    where id = p_trip_id;
    return jsonb_build_object('ok', true, 'action', 'approved', 'trip_id', p_trip_id);
  else
    update fam_trips set
      status = 'rejected',
      rejected_at = now(),
      rejected_by_user_id = auth.uid(),  -- Phase 64: record who rejected
      rejection_reason = coalesce(p_reason, 'Rejected by admin from dashboard')
    where id = p_trip_id;
    return jsonb_build_object('ok', true, 'action', 'rejected', 'trip_id', p_trip_id);
  end if;
end $$;

grant execute on function admin_review_fam_trip(uuid, text, text)
  to authenticator, anon, authenticated, service_role;

-- ─── 3. Patch admin_review_inspection ───────────────────────────────────

create or replace function admin_review_inspection(
  p_inspection_id uuid,
  p_action        text,                 -- 'approve' | 'reject'
  p_reason        text default null
) returns jsonb
  language plpgsql security definer set search_path = public
as $$
declare
  v_inspection site_inspections%rowtype;
  v_role text;
begin
  if p_action not in ('approve','reject') then
    raise exception 'p_action must be approve or reject (got %)', p_action;
  end if;

  select ur.role into v_role from user_roles ur where ur.user_id = auth.uid();
  if v_role not in ('admin','management') then
    raise exception 'not allowed — admin or management role required';
  end if;

  select * into v_inspection from site_inspections where id = p_inspection_id;
  if not found then raise exception 'site inspection not found'; end if;
  if v_inspection.status not in ('pending_approval') then
    raise exception 'only pending_approval inspections can be reviewed (current: %)', v_inspection.status;
  end if;

  if p_action = 'approve' then
    update site_inspections set
      status = 'approved',
      approved_at = now(),
      approver_user_id = auth.uid()
    where id = p_inspection_id;
    return jsonb_build_object('ok', true, 'action', 'approved', 'inspection_id', p_inspection_id);
  else
    update site_inspections set
      status = 'rejected',
      rejected_at = now(),
      rejected_by_user_id = auth.uid(),  -- Phase 64: record who rejected
      rejection_reason = coalesce(p_reason, 'Rejected by admin from dashboard')
    where id = p_inspection_id;
    return jsonb_build_object('ok', true, 'action', 'rejected', 'inspection_id', p_inspection_id);
  end if;
end $$;

grant execute on function admin_review_inspection(uuid, text, text)
  to authenticator, anon, authenticated, service_role;

-- ─── 4. Patch admin_review_group ────────────────────────────────────────

create or replace function admin_review_group(
  p_group_id uuid,
  p_action   text,                 -- 'approve' | 'reject'
  p_reason   text default null
) returns jsonb
  language plpgsql security definer set search_path = public
as $$
declare
  v_group groups%rowtype;
  v_role text;
begin
  if p_action not in ('approve','reject') then
    raise exception 'p_action must be approve or reject (got %)', p_action;
  end if;

  select ur.role into v_role from user_roles ur where ur.user_id = auth.uid();
  if v_role not in ('admin','management') then
    raise exception 'not allowed — admin or management role required';
  end if;

  select * into v_group from groups where id = p_group_id;
  if not found then raise exception 'group not found'; end if;
  if v_group.approval_status <> 'pending_approval' then
    raise exception 'only pending_approval groups can be reviewed (current: %)',
      v_group.approval_status;
  end if;

  if p_action = 'approve' then
    update groups set
      approval_status = 'approved',
      approved_at = now(),
      approver_user_id = auth.uid()
    where id = p_group_id;
    return jsonb_build_object('ok', true, 'action', 'approved', 'group_id', p_group_id);
  else
    update groups set
      approval_status = 'rejected',
      rejected_at = now(),
      rejected_by_user_id = auth.uid(),  -- Phase 64: record who rejected
      rejection_reason = coalesce(p_reason, 'Rejected by admin from dashboard')
    where id = p_group_id;
    return jsonb_build_object('ok', true, 'action', 'rejected', 'group_id', p_group_id);
  end if;
end $$;

grant execute on function admin_review_group(uuid, text, text)
  to authenticator, anon, authenticated, service_role;

-- ─── 5. Force PostgREST cache reload ────────────────────────────────────

notify pgrst, 'reload schema';

-- ─── 6. Verification ────────────────────────────────────────────────────
-- The three tables should now each have rejected_by_user_id column.

select
  table_name,
  count(*) filter (where column_name = 'rejected_by_user_id') as has_rejected_by_user_id,
  count(*) filter (where column_name = 'approver_user_id') as has_approver_user_id,
  count(*) filter (where column_name = 'rejected_at') as has_rejected_at,
  count(*) filter (where column_name = 'rejection_reason') as has_rejection_reason
from information_schema.columns
where table_schema = 'public'
  and table_name in ('fam_trips', 'site_inspections', 'groups')
group by table_name
order by table_name;

-- Expected output: 3 rows (fam_trips, groups, site_inspections), each
-- with all four columns showing count = 1. If rejected_by_user_id shows
-- 0 for any row, the ALTER TABLE didn't apply — investigate.
