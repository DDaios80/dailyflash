-- Phase 5 — email dispatch & expanded role set.
--
-- Adds the 12 new departmental roles from the real recipient list and updates
-- every role-permission helper. Existing `admin`, `management`, `guest_relations`,
-- and `front_office` values are preserved so existing users don't break.
--
-- Content tiering (enforced in the edge function, surfaced via the helpers below):
--   Tier A — full guest detail (allergies, VIPs, A-lister findings):
--     admin, management, guest_relations, front_office, housekeeping, fnb,
--     maintenance, reservations, kepos, kids_club, sales
--   Tier B — metrics only (occupancy, weather, briefing; no guest PII):
--     marketing, accounting, it, call_center, general
--
-- Safe to run multiple times (idempotent).

-- ─── Enum extension ─────────────────────────────────────────────────────────
-- Postgres doesn't let ALTER TYPE ADD VALUE run inside a multi-statement
-- transaction that also uses the new value. So we add the values first in
-- their own block. If Lovable's SQL runner executes statement-by-statement
-- this is fine; if it wraps everything in a single transaction, run this
-- file once to add the values, then re-run it to pick up the rest.
alter type user_role add value if not exists 'sales';
alter type user_role add value if not exists 'marketing';
alter type user_role add value if not exists 'accounting';
alter type user_role add value if not exists 'it';
alter type user_role add value if not exists 'call_center';
alter type user_role add value if not exists 'general';
alter type user_role add value if not exists 'housekeeping';
alter type user_role add value if not exists 'fnb';
alter type user_role add value if not exists 'maintenance';
alter type user_role add value if not exists 'reservations';
alter type user_role add value if not exists 'kepos';
alter type user_role add value if not exists 'kids_club';


-- ─── Role-permission helpers (refreshed for the expanded enum) ─────────────

-- Tier A: sees allergies, VIP names, A-lister, birthdays, etc.
create or replace function can_see_guest_detail() returns boolean
  language sql stable security definer set search_path = public
as $$
  select coalesce(current_user_role() in (
    'admin','management','guest_relations','front_office',
    'housekeeping','fnb','maintenance','reservations',
    'kepos','kids_club','sales'
  ), false);
$$;

-- A-lister findings and reasoning — restricted to roles that work with
-- the information directly. `sales` included per user's explicit request.
create or replace function can_see_alister() returns boolean
  language sql stable security definer set search_path = public
as $$
  select coalesce(current_user_role() in (
    'admin','management','guest_relations','sales'
  ), false);
$$;

-- admin — unchanged but redeclared for completeness
create or replace function can_admin() returns boolean
  language sql stable security definer set search_path = public
as $$
  select coalesce(current_user_role() = 'admin', false);
$$;


-- ─── RLS refresh: tighten guest-detail tables behind can_see_guest_detail() ─
-- alister_findings already filtered by can_see_alister() in phase4. We leave
-- that as-is (it's stricter than guest_detail, which is correct).
--
-- No other tables need RLS changes — flash_reports payload is role-filtered
-- by the email edge function before sending, not by SQL.


-- ─── Helper: current active recipient list (drives the send-flash-email fn) ─
-- Returns every user with an assigned role, plus their email. Used by the
-- edge function to fan out nightly emails. security definer so the edge
-- function's service-role key can read auth.users.
create or replace function email_recipients()
  returns table(user_id uuid, email text, role user_role)
  language sql stable security definer set search_path = public
as $$
  select u.id, u.email, ur.role
  from auth.users u
  inner join user_roles ur on ur.user_id = u.id
  where u.email is not null;
$$;

revoke all on function email_recipients() from public;
-- Only the service role should be calling this.
grant execute on function email_recipients() to service_role;


-- ─── Approval gate — Rooms Division Manager signs off before fan-out ──────
-- Flow:
--   06:00 Athens  Railway → send-flash-email (mode=preview)
--                   → single email to APPROVER_EMAIL with Approve/Reject links
--                   → row inserted into flash_email_approvals with a random token
--   (human click) approve-flash-email endpoint validates the token, marks
--                   approved, then triggers the full fan-out.
create table if not exists flash_email_approvals (
  report_date date primary key,
  approval_token text not null unique,
  preview_sent_at timestamptz not null default now(),
  approved_at timestamptz,
  approved_by text,
  rejected_at timestamptz,
  rejected_by text,
  fanned_out_at timestamptz,
  notes text
);
create index if not exists flash_email_approvals_token_idx
  on flash_email_approvals(approval_token);

alter table flash_email_approvals enable row level security;
drop policy if exists read_flash_email_approvals_admin on flash_email_approvals;
create policy read_flash_email_approvals_admin on flash_email_approvals
  for select using (can_admin());
-- No write policy — edge functions use service role.


-- ─── Delivery log so we can see what went out ──────────────────────────────
create table if not exists email_deliveries (
  id uuid primary key default gen_random_uuid(),
  sent_at timestamptz not null default now(),
  report_date date not null,
  recipient_email text not null,
  role user_role not null,
  resend_message_id text,       -- returned by Resend API on success
  status text not null,         -- 'sent' | 'failed' | 'skipped'
  error text,                   -- null unless status = 'failed'
  created_at timestamptz not null default now()
);
create index if not exists email_deliveries_by_date_idx
  on email_deliveries (report_date desc, sent_at desc);
create index if not exists email_deliveries_by_recipient_idx
  on email_deliveries (recipient_email, sent_at desc);

alter table email_deliveries enable row level security;
drop policy if exists read_email_deliveries_admin on email_deliveries;
create policy read_email_deliveries_admin on email_deliveries
  for select using (can_admin());
-- No write policy — edge function uses service role.


-- ─── Grants for the expanded enum ──────────────────────────────────────────
grant execute on function can_see_guest_detail() to authenticated;
grant execute on function can_see_alister() to authenticated;
grant execute on function can_admin() to authenticated;
