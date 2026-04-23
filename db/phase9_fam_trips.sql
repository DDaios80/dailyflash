-- Phase 9 — FAM Trips.
--
-- FAM trips are authored externally (PowerPoint → PDF) and UPLOADED to the
-- app. We don't try to capture the rich itinerary in structured fields; we
-- store 6 pieces of metadata + the PDF in Supabase Storage, and wrap the
-- whole thing in the same approval workflow as Site Inspections.
--
-- Daily flash joins by date range: show fam_trips where
--   start_date <= report_date <= end_date AND status IN ('approved','sent').
-- The card renders a summary line (trip name, pax, day X of Y, in-charge,
-- dietary summary) plus a signed URL to the uploaded PDF.

-- ─── Status enum ────────────────────────────────────────────────────────────
do $$ begin
  create type fam_trip_status as enum (
    'draft', 'pending_approval', 'approved', 'rejected', 'sent'
  );
exception when duplicate_object then null; end $$;


-- ─── Storage bucket for uploaded PDFs ──────────────────────────────────────
-- Private bucket — the app generates signed URLs for viewers. Service role
-- bypasses RLS so the edge functions can attach the PDF to outgoing emails.
insert into storage.buckets (id, name, public)
values ('fam-trip-pdfs', 'fam-trip-pdfs', false)
on conflict (id) do nothing;

-- Authenticated users in roles that create FAM trips can upload/read.
-- Anyone authenticated can read (so the dashboard can render signed URLs).
-- Service role handles everything else unguarded.
do $$ begin
  drop policy if exists fam_trip_pdfs_read on storage.objects;
  create policy fam_trip_pdfs_read on storage.objects
    for select using (
      bucket_id = 'fam-trip-pdfs' and auth.role() = 'authenticated'
    );
  drop policy if exists fam_trip_pdfs_write on storage.objects;
  create policy fam_trip_pdfs_write on storage.objects
    for insert with check (
      bucket_id = 'fam-trip-pdfs'
      and current_user_role() in ('admin','management','guest_relations','sales')
    );
  drop policy if exists fam_trip_pdfs_update on storage.objects;
  create policy fam_trip_pdfs_update on storage.objects
    for update using (
      bucket_id = 'fam-trip-pdfs'
      and current_user_role() in ('admin','management','guest_relations','sales')
    );
  drop policy if exists fam_trip_pdfs_delete on storage.objects;
  create policy fam_trip_pdfs_delete on storage.objects
    for delete using (
      bucket_id = 'fam-trip-pdfs' and can_admin()
    );
exception when others then
  -- In case storage.objects isn't writable (rare permission setups), skip
  -- and let admin configure via Storage UI.
  raise notice 'storage policy setup skipped: %', sqlerrm;
end $$;


-- ─── Main table ────────────────────────────────────────────────────────────
create table if not exists fam_trips (
  id uuid primary key default gen_random_uuid(),

  -- Core metadata
  name                 text not null,                 -- "DC X CARRIER FAM TRIP"
  start_date           date not null,
  end_date             date not null,
  total_pax            int,
  source_market        text,                          -- "UK / CARRIER"
  room_plan_summary    text,                          -- "CJSTE 12 · CPRESP 4 · …"
  in_charge            text,                          -- "Scott Langridge & Lyndsey Berry"
  dietary_summary      text,                          -- bundled per-person allergies

  -- Uploaded itinerary PDF
  pdf_path             text,                          -- storage key, e.g. "trips/<uuid>.pdf"
  pdf_filename         text,                          -- original filename
  pdf_size_bytes       int,

  -- Approval workflow (same pattern as site_inspections)
  created_by_user_id   uuid references auth.users(id),
  created_by_name      text,
  approver_user_id     uuid references auth.users(id),
  approver_name        text,
  status               fam_trip_status not null default 'draft',
  approval_token       text unique,
  submitted_at         timestamptz,
  approved_at          timestamptz,
  rejected_at          timestamptz,
  rejection_reason     text,
  sent_at              timestamptz,
  issue_date           date,

  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),

  check (end_date >= start_date)
);

create index if not exists fam_trips_date_range_idx
  on fam_trips (start_date, end_date);
create index if not exists fam_trips_status_idx
  on fam_trips (status, start_date);


-- ─── RLS ────────────────────────────────────────────────────────────────────
alter table fam_trips enable row level security;

drop policy if exists read_fam_trips on fam_trips;
create policy read_fam_trips on fam_trips
  for select using (auth.role() = 'authenticated');

drop policy if exists insert_fam_trips on fam_trips;
create policy insert_fam_trips on fam_trips
  for insert with check (
    current_user_role() in ('admin','management','guest_relations','sales')
    and (created_by_user_id = auth.uid() or created_by_user_id is null)
  );

drop policy if exists update_fam_trips on fam_trips;
create policy update_fam_trips on fam_trips
  for update using (
    current_user_role() in ('admin','management')
    or (created_by_user_id = auth.uid() and status in ('draft','rejected'))
    or (approver_user_id = auth.uid() and status = 'pending_approval')
  );

drop policy if exists delete_fam_trips_admin on fam_trips;
create policy delete_fam_trips_admin on fam_trips
  for delete using (can_admin());


-- ─── updated_at trigger ────────────────────────────────────────────────────
create or replace function _touch_fam_trips_updated_at()
  returns trigger language plpgsql as
$$ begin new.updated_at := now(); return new; end $$;

drop trigger if exists fam_trips_touch on fam_trips;
create trigger fam_trips_touch
  before update on fam_trips
  for each row execute function _touch_fam_trips_updated_at();


-- ─── Settings: separate recipients list for FAM trips ─────────────────────
insert into app_settings (key, value) values
  ('fam_trip_recipients', '')                          -- admin seeds via dashboard
on conflict (key) do nothing;


-- ─── Approvers RPC (shared shape with list_inspection_approvers) ──────────
-- Points at the same underlying set (admin/management users). Named
-- differently for discoverability from the FAM-trip UI.
create or replace function list_fam_trip_approvers()
  returns table(user_id uuid, email text, display_name text)
  language sql stable security definer set search_path = public
as $$
  select u.id, u.email,
         coalesce(
           (u.raw_user_meta_data ->> 'full_name'),
           (u.raw_user_meta_data ->> 'name'),
           u.email
         )
  from auth.users u
  inner join user_roles ur on ur.user_id = u.id
  where ur.role in ('admin','management');
$$;
grant execute on function list_fam_trip_approvers() to authenticated;


-- ─── RPC: submit for approval ───────────────────────────────────────────────
create or replace function submit_fam_trip_for_approval(
  p_trip_id uuid,
  p_approver_user_id uuid
) returns void
  language plpgsql security definer set search_path = cron_private, public
as $$
declare
  v_trip fam_trips%rowtype;
  v_approver_name text;
  v_approver_email text;
  v_token text;
  v_url text;
  v_secret text;
begin
  select * into v_trip from fam_trips where id = p_trip_id;
  if not found then raise exception 'fam trip not found'; end if;
  if v_trip.created_by_user_id <> auth.uid() and not can_admin() then
    raise exception 'not allowed — only creator or admin may submit';
  end if;
  if v_trip.status not in ('draft', 'rejected') then
    raise exception 'only draft or rejected trips can be submitted (current: %)', v_trip.status;
  end if;

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

  update fam_trips set
    status = 'pending_approval',
    approver_user_id = p_approver_user_id,
    approver_name = v_approver_name,
    approval_token = v_token,
    submitted_at = now(),
    rejected_at = null,
    rejection_reason = null
  where id = p_trip_id;

  -- Fire the approval email
  select value into v_url from cron_private.secrets where key = 'send_fam_trip_approval_url';
  select value into v_secret from cron_private.secrets where key = 'pipeline_secret';
  if v_url is not null and v_secret is not null then
    perform net.http_post(
      url := v_url,
      headers := jsonb_build_object(
        'Content-Type', 'application/json',
        'Authorization', 'Bearer ' || v_secret
      ),
      body := jsonb_build_object('trip_id', p_trip_id::text)
    );
  end if;
end $$;
grant execute on function submit_fam_trip_for_approval(uuid, uuid) to authenticated;


-- ─── RPC: mark sent ────────────────────────────────────────────────────────
create or replace function mark_fam_trip_sent(p_trip_id uuid)
  returns void
  language plpgsql security invoker
as $$
begin
  if not (
    can_admin()
    or current_user_role() = 'management'
    or exists(select 1 from fam_trips where id = p_trip_id and created_by_user_id = auth.uid())
  ) then
    raise exception 'not allowed';
  end if;
  update fam_trips set status = 'sent', sent_at = now()
  where id = p_trip_id and status = 'approved';
end $$;
grant execute on function mark_fam_trip_sent(uuid) to authenticated;


-- ─── Post-migration: admin primes 3 secrets ────────────────────────────────
-- insert into cron_private.secrets (key, value) values
--   ('send_fam_trip_approval_url',
--    'https://wgbghdbfmapuqbfeiygb.supabase.co/functions/v1/send-fam-trip-approval'),
--   ('approve_fam_trip_url',
--    'https://wgbghdbfmapuqbfeiygb.supabase.co/functions/v1/approve-fam-trip'),
--   ('send_fam_trip_to_recipients_url',
--    'https://wgbghdbfmapuqbfeiygb.supabase.co/functions/v1/send-fam-trip-to-recipients')
-- on conflict (key) do update set value = excluded.value, updated_at = now();
