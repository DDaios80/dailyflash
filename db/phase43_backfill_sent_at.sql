-- Phase 43 — backfill sent_at for already-approved fam_trips and
-- site_inspections that were stuck at sent_at=NULL because the old
-- approve-fam-trip edge function never stamped it.
--
-- The new approve-fam-trip and approve-site-inspection edge functions
-- (Phase 43 fix) stamp sent_at = approved_at on every approval. This
-- backfill closes out the historical rows so dashboard filters and
-- audit queries don't keep flagging them as "stuck approved".
--
-- Idempotent — only updates rows where status='approved' AND sent_at IS NULL.
-- Sets sent_at = approved_at (the moment approval happened).

update fam_trips
set sent_at = approved_at,
    updated_at = now()
where status = 'approved'
  and approved_at is not null
  and sent_at is null;

update site_inspections
set sent_at = approved_at,
    updated_at = now()
where status = 'approved'
  and approved_at is not null
  and sent_at is null;

-- Verify
select 'fam_trips' as tbl,
       count(*) filter (where status = 'approved' and sent_at is null) as still_null,
       count(*) filter (where status = 'approved') as total_approved
from fam_trips
union all
select 'site_inspections',
       count(*) filter (where status = 'approved' and sent_at is null),
       count(*) filter (where status = 'approved')
from site_inspections;
