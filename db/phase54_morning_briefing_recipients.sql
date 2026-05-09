-- Phase 54 — bulk recipient list for the morning ExCom briefing.
--
-- Mirror of the fam_trip_recipients / site_inspection_recipients pattern:
-- one row in app_settings keyed by `morning_briefing_recipients`, value is
-- a text blob in the "Name <email>; Name <email>;" format the admin team
-- already uses for FAM trips and inspections.
--
-- The morning briefing currently fires at exec_briefing_send_time_athens
-- to a small set of users (resolved from user_roles / committee_chair_user_id).
-- This adds a SECOND channel: a configurable bulk list that the admin can
-- maintain via the /admin → Distribution UI without touching SQL.
--
-- Post-this-migration:
--   1. The send-exec-briefing edge function (or equivalent) needs to read
--      app_settings.morning_briefing_recipients and add those addresses
--      to the to: list when firing the email.
--   2. Lovable's /admin page needs a new section "Morning Briefing
--      Recipients" mirroring the "FAM Trip Recipients" section.
--   3. Both changes are described in
--      docs/lovable-handoff-2026-05-09-morning-briefing-ui.md.
--
-- Idempotent. Initial value is empty so the admin team owns ownership
-- via the UI.

insert into public.app_settings (key, value, updated_at)
values (
  'morning_briefing_recipients',
  '',
  now()
)
on conflict (key) do nothing;

-- Verify
select key, length(value) as value_len, updated_at
from app_settings
where key = 'morning_briefing_recipients';
