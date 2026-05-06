-- Phase 45 — auto-reassign new FAM trips and site inspections to Thelxi.
--
-- The Lovable edge functions (ingest-fam-trip-from-onedrive and any
-- equivalent for inspections) hardcode the default approver to
-- d.daios (UUID a116987e-4351-459f-8347-14fa6cfdf5ae). Phase 34.1
-- ran a one-shot reassignment to Thelxi, but new rows imported by
-- the cron each day come back as d.daios and Thelxi never sees them
-- in her queue.
--
-- Fix: a BEFORE INSERT trigger that catches the d.daios default and
-- swaps it to Thelxi (UUID d58e34cb-1d2e-492d-bbae-987fc0a80176).
-- INSERT-only — manual UPDATEs that explicitly set d.daios as approver
-- are preserved (in case CEO-level approval is ever needed).
--
-- Idempotent: drops any existing trigger first.

create or replace function reassign_default_approver_to_thelxi()
returns trigger
language plpgsql
as $$
begin
  -- Only swap if the import is using d.daios as default approver.
  if new.approver_user_id = 'a116987e-4351-459f-8347-14fa6cfdf5ae' then
    new.approver_user_id := 'd58e34cb-1d2e-492d-bbae-987fc0a80176';
    new.approver_name    := 'thelxi.smyrnaki@daioshotels.com';
  end if;
  return new;
end;
$$;

-- FAM trips
drop trigger if exists fam_trip_default_approver_to_thelxi on fam_trips;
create trigger fam_trip_default_approver_to_thelxi
  before insert on fam_trips
  for each row
  execute function reassign_default_approver_to_thelxi();

-- Site inspections (when sync ships, this catches it too)
drop trigger if exists site_inspection_default_approver_to_thelxi on site_inspections;
create trigger site_inspection_default_approver_to_thelxi
  before insert on site_inspections
  for each row
  execute function reassign_default_approver_to_thelxi();
