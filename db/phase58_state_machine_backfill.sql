-- Phase 58 — backfill historical state-machine inconsistencies on
-- fam_trips and site_inspections.
--
-- Audit (db/phase58_state_machine_audit query) found 4 rows in
-- production with half-finished writes:
--   - fam_trips         approved   approved_at-but-no-approver   (2 rows)
--   - site_inspections  sent       approved_at-but-no-approver   (1 row)
--   - site_inspections  approved   approved-but-no-sent_at       (1 row)
--
-- These are pre-Phase 43/45 era rows that got their approval state
-- partially populated. Repairs:
--
-- 1. Stamp approver_user_id with d.daios (the historical default
--    approver before Phase 45 reassigned to Thelxi). Best guess for
--    rows where the original approver isn't recoverable.
-- 2. Stamp sent_at = approved_at on the one row that Phase 43
--    backfill missed (likely approved after the backfill ran).
--
-- Idempotent. Each UPDATE has narrow WHERE clauses so re-runs are
-- safe.

-- 1. fam_trips: missing approver_user_id where approved_at is set
update fam_trips
set approver_user_id = (
      select id from auth.users
      where email = 'd.daios@daioshotels.com'
      limit 1
    ),
    approver_name = 'd.daios@daioshotels.com',
    updated_at = now()
where approved_at is not null
  and approver_user_id is null;

-- 2. site_inspections: missing approver_user_id where approved_at is set
update site_inspections
set approver_user_id = (
      select id from auth.users
      where email = 'd.daios@daioshotels.com'
      limit 1
    ),
    approver_name = 'd.daios@daioshotels.com',
    updated_at = now()
where approved_at is not null
  and approver_user_id is null;

-- 3. site_inspections: missing sent_at where status is approved
update site_inspections
set sent_at = approved_at,
    updated_at = now()
where status::text = 'approved'
  and sent_at is null
  and approved_at is not null;

-- Verify — should return 0 rows for any remaining issues
with fam_audit as (
  select 'fam_trips' as tbl, status::text as status,
         case
           when status::text = 'approved' and approved_at is null then 'approved-but-no-approved_at'
           when approved_at is not null and approver_user_id is null then 'approved_at-but-no-approver'
           when status::text = 'approved' and sent_at is null then 'approved-but-no-sent_at'
           else null
         end as issue
  from fam_trips
),
inspection_audit as (
  select 'site_inspections' as tbl, status::text as status,
         case
           when status::text = 'approved' and approved_at is null then 'approved-but-no-approved_at'
           when approved_at is not null and approver_user_id is null then 'approved_at-but-no-approver'
           when status::text = 'approved' and sent_at is null then 'approved-but-no-sent_at'
           else null
         end as issue
  from site_inspections
)
select tbl, status, issue, count(*) as remaining
from (select * from fam_audit union all select * from inspection_audit) a
where issue is not null
group by tbl, status, issue
order by tbl;
