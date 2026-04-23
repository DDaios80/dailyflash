-- Phase 8 — Site Inspections.
--
-- A dedicated table to manage site inspection forms: create, approve, send,
-- and have them appear in the daily flash on their inspection_date.
--
-- Flow:
--   1. Creator (sales/GR/mgmt/admin) fills the form → status 'draft'.
--   2. Creator picks an approver from management/admin users and clicks
--      "Submit for approval" → status 'pending_approval' → approval email
--      sent to the approver (similar to the flash email approval gate).
--   3. Approver clicks Approve link → status 'approved', or Reject → back
--      to 'draft' with rejection_reason, creator notified by email.
--   4. Creator clicks "Send" on an approved inspection → the form is
--      distributed to the configured recipients list → status 'sent'.
--   5. At 19:30 Athens the pipeline picks up inspections where
--      inspection_date = report_date AND status IN ('approved','sent')
--      and includes them in flash_reports.payload.site_inspections.

-- ─── Status enum ────────────────────────────────────────────────────────────
do $$ begin
  create type site_inspection_status as enum (
    'draft', 'pending_approval', 'approved', 'rejected', 'sent'
  );
exception when duplicate_object then null; end $$;


-- ─── Reason enum (enumerated here even though FAM / INFO will live in other
--     tables; we still allow the value here so the column is self-documenting
--     and future reclassifications are possible) ──────────────────────────────
do $$ begin
  create type site_inspection_reason as enum (
    'site_inspection', 'fam_trip', 'info_group', 'other'
  );
exception when duplicate_object then null; end $$;


-- ─── Main table ─────────────────────────────────────────────────────────────
create table if not exists site_inspections (
  id uuid primary key default gen_random_uuid(),

  -- Visit context
  reason_of_visit   site_inspection_reason not null default 'site_inspection',
  travel_agency     text,                      -- e.g. "Reisebüro im Musikviertel TUI TravelStar"
  source_market     text,                      -- "DACH", "UK", "US", etc.
  accompanied_by_dmc boolean,
  attendees         text,                      -- "Kirstin Baugut +1"

  -- Schedule
  arrival_date      date,
  inspection_date   date,                      -- the pipeline join key
  inspection_time   time,

  -- Stay
  stay_at_hotel      boolean,
  number_of_persons  int,
  country_language   text,                     -- "GER", "ENG" etc.

  -- Logistics
  promo_material_provided boolean,
  agency_contact_person   text,
  phone_mobile            text,
  email_address           text,
  inspection_performed_by text,                -- our side: "Analena"
  lunch_dinner            boolean,
  spa_offer               boolean,

  -- Free-text
  comments text,

  -- Meta / approval
  created_by_user_id uuid references auth.users(id),
  created_by_name    text,                     -- denormalized display name
  approver_user_id   uuid references auth.users(id),
  approver_name      text,                     -- denormalized display name
  status             site_inspection_status not null default 'draft',
  approval_token     text unique,              -- random UUID when pending_approval
  submitted_at       timestamptz,
  approved_at        timestamptz,
  rejected_at        timestamptz,
  rejection_reason   text,
  sent_at            timestamptz,
  issue_date         date,                     -- displayed on the form email

  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create index if not exists site_inspections_date_idx
  on site_inspections (inspection_date);
create index if not exists site_inspections_status_idx
  on site_inspections (status, inspection_date);


-- ─── RLS ────────────────────────────────────────────────────────────────────
alter table site_inspections enable row level security;

-- Who can read: anyone authenticated. The daily flash shows excerpts to all
-- Tier A roles; full form detail is typically only viewed by creators +
-- approvers + admin. For simplicity start with read-all-authenticated and
-- tighten later if needed.
drop policy if exists read_site_inspections on site_inspections;
create policy read_site_inspections on site_inspections
  for select using (auth.role() = 'authenticated');

-- Who can insert: roles that create inspections (sales, guest_relations,
-- management, admin).
drop policy if exists insert_site_inspections on site_inspections;
create policy insert_site_inspections on site_inspections
  for insert with check (
    current_user_role() in ('admin','management','guest_relations','sales')
    and (created_by_user_id = auth.uid() or created_by_user_id is null)
  );

-- Who can update: creator while still draft/rejected, approver during
-- pending_approval (to approve/reject), admin/management any time.
drop policy if exists update_site_inspections on site_inspections;
create policy update_site_inspections on site_inspections
  for update using (
    current_user_role() in ('admin','management')
    or (created_by_user_id = auth.uid() and status in ('draft','rejected'))
    or (approver_user_id = auth.uid() and status = 'pending_approval')
  );

-- Who can delete: admin only (soft-delete via status would be better but
-- keep it simple for v1).
drop policy if exists delete_site_inspections_admin on site_inspections;
create policy delete_site_inspections_admin on site_inspections
  for delete using (can_admin());


-- ─── updated_at trigger ────────────────────────────────────────────────────
create or replace function _touch_site_inspections_updated_at()
  returns trigger language plpgsql as
$$ begin new.updated_at := now(); return new; end $$;

drop trigger if exists site_inspections_touch on site_inspections;
create trigger site_inspections_touch
  before update on site_inspections
  for each row execute function _touch_site_inspections_updated_at();


-- ─── Settings: recipients list (shared across all inspections) ─────────────
-- Stored as a newline- or comma-separated list in app_settings so the admin
-- UI can edit it freely. When sending, the edge function splits and sends to
-- each. Can be extended to a proper table later.
insert into app_settings (key, value) values
  ('site_inspection_recipients', '')  -- admin seeds via dashboard
on conflict (key) do nothing;


-- ─── Settings: default approvers list (the management users who can approve)
-- Exposed via an RPC to power the dropdown in the create/submit form.
create or replace function list_inspection_approvers()
  returns table(user_id uuid, email text, display_name text)
  language sql stable security definer set search_path = public
as $$
  select u.id, u.email,
         coalesce(
           (u.raw_user_meta_data ->> 'full_name'),
           (u.raw_user_meta_data ->> 'name'),
           u.email
         ) as display_name
  from auth.users u
  inner join user_roles ur on ur.user_id = u.id
  where ur.role in ('admin','management');
$$;
grant execute on function list_inspection_approvers() to authenticated;


-- ─── RPC: submit for approval ───────────────────────────────────────────────
-- Caller picks an approver_user_id. We:
--   - Validate caller is the creator and status is 'draft' or 'rejected'.
--   - Set status='pending_approval', approver_user_id, submitted_at, and a
--     random approval_token.
--   - Fire the send-inspection-approval edge function via pg_net so the
--     approver gets an email with Approve/Reject links.
create or replace function submit_inspection_for_approval(
  p_inspection_id uuid,
  p_approver_user_id uuid
) returns void
  language plpgsql security definer set search_path = cron_private, public
as $$
declare
  v_inspection site_inspections%rowtype;
  v_approver_name text;
  v_approver_email text;
  v_token text;
  v_url text;
  v_secret text;
begin
  select * into v_inspection from site_inspections where id = p_inspection_id;
  if not found then raise exception 'inspection not found'; end if;
  if v_inspection.created_by_user_id <> auth.uid() and not can_admin() then
    raise exception 'not allowed — only creator or admin may submit';
  end if;
  if v_inspection.status not in ('draft', 'rejected') then
    raise exception 'only draft or rejected inspections can be submitted (current: %)', v_inspection.status;
  end if;

  -- Resolve approver display info
  select
    u.email,
    coalesce((u.raw_user_meta_data ->> 'full_name'), (u.raw_user_meta_data ->> 'name'), u.email)
  into v_approver_email, v_approver_name
  from auth.users u
  inner join user_roles ur on ur.user_id = u.id
  where u.id = p_approver_user_id and ur.role in ('admin','management');
  if v_approver_email is null then
    raise exception 'approver must be a management or admin user';
  end if;

  v_token := replace(gen_random_uuid()::text, '-', '');

  update site_inspections set
    status = 'pending_approval',
    approver_user_id = p_approver_user_id,
    approver_name = v_approver_name,
    approval_token = v_token,
    submitted_at = now(),
    rejected_at = null,
    rejection_reason = null
  where id = p_inspection_id;

  -- Fire the approval email (best-effort — ignore failure so the state
  -- transition sticks). Admin can resend from the UI if needed.
  select value into v_url from cron_private.secrets where key = 'send_inspection_approval_url';
  select value into v_secret from cron_private.secrets where key = 'pipeline_secret';
  if v_url is not null and v_secret is not null then
    perform net.http_post(
      url := v_url,
      headers := jsonb_build_object(
        'Content-Type', 'application/json',
        'Authorization', 'Bearer ' || v_secret
      ),
      body := jsonb_build_object('inspection_id', p_inspection_id::text)
    );
  end if;
end $$;
grant execute on function submit_inspection_for_approval(uuid, uuid) to authenticated;


-- ─── RPC: mark sent (called by send-inspection-to-recipients) ──────────────
-- The edge function uses the service role; this is exposed so callers (e.g.
-- the creator clicking "Send" in the UI) can also call the edge fn directly
-- and have the state transition happen as part of the function's SQL.
create or replace function mark_inspection_sent(p_inspection_id uuid)
  returns void
  language plpgsql security invoker
as $$
begin
  if not (
    can_admin()
    or current_user_role() = 'management'
    or exists(
      select 1 from site_inspections
      where id = p_inspection_id and created_by_user_id = auth.uid()
    )
  ) then
    raise exception 'not allowed';
  end if;
  update site_inspections set
    status = 'sent',
    sent_at = now()
  where id = p_inspection_id and status = 'approved';
end $$;
grant execute on function mark_inspection_sent(uuid) to authenticated;


-- ─── Post-migration secret setup ────────────────────────────────────────────
-- Admin must prime three secrets after deploying the edge functions:
--   insert into cron_private.secrets (key, value) values
--     ('send_inspection_approval_url',
--      'https://wgbghdbfmapuqbfeiygb.supabase.co/functions/v1/send-inspection-approval'),
--     ('approve_inspection_url',
--      'https://wgbghdbfmapuqbfeiygb.supabase.co/functions/v1/approve-inspection'),
--     ('send_inspection_to_recipients_url',
--      'https://wgbghdbfmapuqbfeiygb.supabase.co/functions/v1/send-inspection-to-recipients')
--   on conflict (key) do update set value = excluded.value, updated_at = now();
