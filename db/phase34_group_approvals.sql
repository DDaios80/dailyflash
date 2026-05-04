-- Phase 34 — Approval workflow on groups.
--
-- Phase 14 added the groups table (weddings, corporate retreats, incentives,
-- conferences) with a simple operational status (planned -> confirmed ->
-- in_progress -> completed/cancelled). No approval workflow.
--
-- This phase brings the FAM-trip / site-inspection approval pattern across
-- to groups. After admin approval, the structured group event can be
-- distributed to staff (housekeeping, F&B, banquet, GR) the same way FAM
-- trips and inspections are.
--
-- Design decisions (defaults from session context):
--   - Optional PDF attachment (BEO, contract, itinerary) — storage bucket
--     `group-pdfs`. Groups can be pure-form too.
--   - OneDrive auto-import deferred (v2). Manual creation via dashboard
--     for now.
--   - Distribution list after approval mirrors FAM trips.
--   - Approval-status enum is PARALLEL to the existing group_status enum,
--     not merged. This keeps operational state (planned, in_progress) and
--     approval workflow (draft, pending_approval, approved, rejected, sent)
--     orthogonal — a wedding can be in_progress AND approved AND sent, all
--     three independent facts.

-- ─── New approval-status enum ──────────────────────────────────────────
do $$ begin
  create type group_approval_status as enum (
    'draft', 'pending_approval', 'approved', 'rejected', 'sent'
  );
exception when duplicate_object then null; end $$;

-- ─── Extend groups table with approval fields ─────────────────────────
alter table groups
  add column if not exists approval_status   group_approval_status not null default 'draft',
  add column if not exists created_by_user_id uuid references auth.users(id),
  add column if not exists created_by_name    text,
  add column if not exists approver_user_id   uuid references auth.users(id),
  add column if not exists approver_name      text,
  add column if not exists approval_token     text unique,
  add column if not exists submitted_at       timestamptz,
  add column if not exists approved_at        timestamptz,
  add column if not exists rejected_at        timestamptz,
  add column if not exists rejection_reason   text,
  add column if not exists sent_at            timestamptz,
  -- Optional PDF attachment (BEO, contract, itinerary, etc.)
  add column if not exists pdf_path           text,        -- storage key, e.g. "auto/<uuid>.pdf"
  add column if not exists pdf_filename       text,
  add column if not exists pdf_size_bytes     int;

create index if not exists groups_approval_status_idx
  on groups (approval_status, start_date)
  where approval_status in ('pending_approval','approved','sent');


-- ─── Storage bucket for group PDFs ────────────────────────────────────
insert into storage.buckets (id, name, public)
values ('group-pdfs', 'group-pdfs', false)
on conflict (id) do nothing;

-- Storage policies — same shape as fam-trip-pdfs bucket (Phase 9).
drop policy if exists "group_pdfs_authenticated_insert" on storage.objects;
create policy "group_pdfs_authenticated_insert"
  on storage.objects for insert
  with check (bucket_id = 'group-pdfs' and auth.role() = 'authenticated');

drop policy if exists "group_pdfs_authenticated_select" on storage.objects;
create policy "group_pdfs_authenticated_select"
  on storage.objects for select
  using (bucket_id = 'group-pdfs');

drop policy if exists "group_pdfs_authenticated_update" on storage.objects;
create policy "group_pdfs_authenticated_update"
  on storage.objects for update
  using (bucket_id = 'group-pdfs');

drop policy if exists "group_pdfs_admin_delete" on storage.objects;
create policy "group_pdfs_admin_delete"
  on storage.objects for delete
  using (bucket_id = 'group-pdfs' and exists (
    select 1 from user_roles
    where user_id = auth.uid() and role = 'admin'
  ));


-- ─── Submit-for-approval RPC (mirrors submit_fam_trip_for_approval) ────
create or replace function submit_group_for_approval(
  p_group_id         uuid,
  p_approver_user_id uuid
) returns void
  language plpgsql security definer set search_path = cron_private, public
as $$
declare
  v_group groups%rowtype;
  v_approver_name  text;
  v_approver_email text;
  v_token text;
  v_url text;
  v_secret text;
begin
  select * into v_group from groups where id = p_group_id;
  if not found then raise exception 'group not found'; end if;
  if v_group.created_by_user_id is not null
     and v_group.created_by_user_id <> auth.uid()
     and not can_admin() then
    raise exception 'not allowed — only creator or admin may submit';
  end if;
  if v_group.approval_status not in ('draft', 'rejected') then
    raise exception 'only draft or rejected groups can be submitted (current: %)',
      v_group.approval_status;
  end if;

  select
    u.email,
    coalesce((u.raw_user_meta_data ->> 'full_name'),
             (u.raw_user_meta_data ->> 'name'),
             u.email)
  into v_approver_email, v_approver_name
  from auth.users u
  inner join user_roles ur on ur.user_id = u.id
  where u.id = p_approver_user_id and ur.role in ('admin','management');
  if v_approver_email is null then
    raise exception 'approver must be a management or admin user';
  end if;

  v_token := replace(gen_random_uuid()::text, '-', '');

  update groups set
    approval_status = 'pending_approval',
    approver_user_id = p_approver_user_id,
    approver_name    = v_approver_name,
    approval_token   = v_token,
    submitted_at     = now(),
    rejected_at      = null,
    rejection_reason = null
  where id = p_group_id;

  -- Fire the approval email via cron_private.secrets path.
  -- The send_group_approval_url secret needs to be added once Lovable
  -- ships the send-group-approval edge function.
  select value into v_url from cron_private.secrets where key = 'send_group_approval_url';
  select value into v_secret from cron_private.secrets where key = 'pipeline_secret';
  if v_url is not null and v_secret is not null then
    perform net.http_post(
      url := v_url,
      headers := jsonb_build_object(
        'Content-Type', 'application/json',
        'Authorization', 'Bearer ' || v_secret
      ),
      body := jsonb_build_object('group_id', p_group_id::text)
    );
  end if;
end $$;
grant execute on function submit_group_for_approval(uuid, uuid) to authenticated;


-- ─── In-app admin approval RPC (mirrors Phase 32 admin_review_fam_trip) ─
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
      rejection_reason = coalesce(p_reason, 'Rejected by admin from dashboard')
    where id = p_group_id;
    return jsonb_build_object('ok', true, 'action', 'rejected', 'group_id', p_group_id);
  end if;
end $$;
grant execute on function admin_review_group(uuid, text, text) to authenticated;


-- ─── Extend the unified pending-approvals queue (Phase 32) ──────────────
-- Add a 'group' branch alongside fam_trip + site_inspection.
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
      null::date as inspection_date,
      null::text as group_type
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
      si.inspection_date,
      null::text as group_type
    from site_inspections si
    where si.status = 'pending_approval'
  ),
  grp as (
    select
      'group' as kind,
      g.id,
      g.name as title,
      g.start_date as date_from,
      g.end_date   as date_to,
      g.created_by_name,
      g.submitted_at,
      g.pdf_filename,
      null::text as travel_agency,
      null::date as inspection_date,
      g.type::text as group_type
    from groups g
    where g.approval_status = 'pending_approval'
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
    'inspection_date', x.inspection_date,
    'group_type', x.group_type
  ) order by x.submitted_at desc nulls last), '[]'::jsonb)
  from (select * from fam union all select * from ins union all select * from grp) x;
$$;
grant execute on function pending_approvals_for_admin() to authenticated;


-- ─── List of approvers (mirrors list_fam_trip_approvers) ──────────────
create or replace function list_group_approvers()
returns table (user_id uuid, name text, email text)
  language sql stable security definer set search_path = public
as $$
  select u.id as user_id,
         coalesce((u.raw_user_meta_data ->> 'full_name'),
                  (u.raw_user_meta_data ->> 'name'),
                  u.email) as name,
         u.email
  from auth.users u
  inner join user_roles ur on ur.user_id = u.id
  where ur.role in ('admin','management')
  order by 2;
$$;
grant execute on function list_group_approvers() to authenticated;
