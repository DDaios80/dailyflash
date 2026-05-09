-- Phase 59 — strip the "(Accepted in 10min & delivered in 30min)"
-- explanation from zoho_notes housekeeping bodies.
--
-- User report: this cleanup has been done before but doesn't persist.
-- That's because it lived in the ingest-zoho-notes edge function code,
-- which Lovable's AI overwrites during refactors. The fix moves the
-- cleanup to a DB trigger — Lovable's AI doesn't typically modify
-- triggers, so this stays.
--
-- Sample data (5/9/2026, 341 rows affected):
--   "1 × Iron / σίδερο (NORMAL (Accepted in 10min & delivered in 30min))"
-- After cleanup:
--   "1 × Iron / σίδερο (NORMAL)"
--
-- The regex strips ` (Accepted in <anything> )` keeping the outer
-- "(NORMAL)" parenthetical intact. Works for any priority level —
-- NORMAL, URGENT, HIGH, LOW — because the inner `(Accepted in ...)`
-- pattern is uniform.
--
-- content_hash is intentionally NOT modified by this trigger or the
-- backfill: zoho's ingest dedup compares incoming raw-body hashes
-- against stored content_hash, and we want re-ingests of the same
-- note to keep deduping correctly.

-- 1. Pure helper function. Idempotent — running twice does the same
-- thing as running once.
create or replace function strip_zoho_priority_explanation(input text)
returns text
language sql immutable
as $$
  select regexp_replace(
    coalesce(input, ''),
    ' *\(Accepted in [^)]+\)',
    '',
    'g'
  );
$$;

-- 2. Trigger function — applies the strip to body and subject.
create or replace function zoho_notes_strip_priority_trigger()
returns trigger
language plpgsql
as $$
begin
  new.body    := strip_zoho_priority_explanation(new.body);
  new.subject := strip_zoho_priority_explanation(new.subject);
  return new;
end;
$$;

-- 3. Install the BEFORE INSERT/UPDATE trigger. Drop-then-create makes
-- this idempotent.
drop trigger if exists zoho_notes_strip_priority on zoho_notes;
create trigger zoho_notes_strip_priority
  before insert or update on zoho_notes
  for each row execute function zoho_notes_strip_priority_trigger();

-- 4. Backfill the 341 existing affected rows. (The trigger fires on
-- this UPDATE too but is idempotent — strip(already_stripped) is a
-- no-op.)
update zoho_notes
set body    = strip_zoho_priority_explanation(body),
    subject = strip_zoho_priority_explanation(subject)
where body    ~ '\(Accepted in '
   or subject ~ '\(Accepted in ';

-- 5. Verify — should report 0 remaining rows with the explanation.
select
  count(*) filter (
    where body ~ '\(Accepted in ' or subject ~ '\(Accepted in '
  ) as remaining_with_explanation,
  count(*) as total_rows
from zoho_notes;
