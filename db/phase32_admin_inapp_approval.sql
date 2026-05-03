-- Phase 32 — In-app approve/reject for FAM trips + site inspections.
--
-- Today's email-link approval flow has been a recurring point of failure
-- (broken file:// URLs, env vars unset, Outlook stripping anchors). Admins
-- need an in-app path that's never blocked by email rendering quirks.
--
-- These RPCs accept the row id + action ('approve' | 'reject') from any
-- authenticated user with role 'admin' or 'management', bypassing the
-- token-based email mechanism. Mirrors the logic in the approve-fam-trip
-- and approve-inspection edge functions.

-- ─── FAM trip in-app approval ────────────────────────────────────────────
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
      rejection_reason = coalesce(p_reason, 'Rejected by admin from dashboard')
    where id = p_trip_id;
    return jsonb_build_object('ok', true, 'action', 'rejected', 'trip_id', p_trip_id);
  end if;
end $$;
grant execute on function admin_review_fam_trip(uuid, text, text) to authenticated;


-- ─── Site inspection in-app approval ────────────────────────────────────
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
      rejection_reason = coalesce(p_reason, 'Rejected by admin from dashboard')
    where id = p_inspection_id;
    return jsonb_build_object('ok', true, 'action', 'rejected', 'inspection_id', p_inspection_id);
  end if;
end $$;
grant execute on function admin_review_inspection(uuid, text, text) to authenticated;


-- ─── List of pending approvals across both tables ───────────────────────
-- Single RPC the dashboard can poll to show the admin's review queue.
create or replace function pending_approvals_for_admin()
returns jsonb
  language sql stable security invoker set search_path = public
as $$
  with fam as (
    select
      'fam_trip' as kind,
      ft.id,
      ft.name as title,
      ft.start_date as date_from,
      ft.end_date   as date_to,
      ft.created_by_name,
      ft.submitted_at,
      ft.pdf_filename,
      null::text as travel_agency,
      null::date as inspection_date
    from fam_trips ft
    where ft.status = 'pending_approval'
  ),
  ins as (
    select
      'site_inspection' as kind,
      si.id,
      si.travel_agency as title,
      si.inspection_date as date_from,
      si.inspection_date as date_to,
      si.created_by_name,
      si.submitted_at,
      null::text as pdf_filename,
      si.travel_agency,
      si.inspection_date
    from site_inspections si
    where si.status = 'pending_approval'
  )
  select coalesce(jsonb_agg(jsonb_build_object(
    'kind', x.kind,
    'id', x.id,
    'title', x.title,
    'date_from', x.date_from,
    'date_to', x.date_to,
    'created_by_name', x.created_by_name,
    'submitted_at', x.submitted_at,
    'pdf_filename', x.pdf_filename,
    'travel_agency', x.travel_agency,
    'inspection_date', x.inspection_date
  ) order by x.submitted_at desc), '[]'::jsonb)
  from (select * from fam union all select * from ins) x;
$$;
grant execute on function pending_approvals_for_admin() to authenticated;
