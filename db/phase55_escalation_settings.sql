-- Phase 55 — auto-escalate flash approval to d.daios if not approved
-- by 07:30 Athens, ahead of the 08:00 mass-send window.
--
-- Background: as of Phase 54, the cron generates the flash at 22:00
-- Athens and emails Thelxi a preview/approval link. Mass-send to the
-- ~62 recipients fires only after she clicks Approve. If she misses
-- the email overnight (today's incident), the morning send doesn't go
-- out. Phase 55 adds a 07:30 escalation: if no approval by then, send
-- a fresh approval email to d.daios so he can approve in time.
--
-- Implementation: the escalation address is configurable via
-- app_settings so the team can change it without a code deploy. Same
-- pattern as fam_trip_ingest_approver_email and
-- site_inspection_ingest_approver_email.
--
-- Idempotent.

insert into public.app_settings (key, value, updated_at)
values (
  'flash_escalation_approver_email',
  'd.daios@daioshotels.com',
  now()
)
on conflict (key) do update set
  value = excluded.value,
  updated_at = now();

-- Verify
select key, value, updated_at
from app_settings
where key = 'flash_escalation_approver_email';
