-- Phase 48 — re-extract comment_extractions when reservation comments change.
--
-- Background: extraction runs only on first ingest of a reservation. If the
-- comment is edited mid-stay (upsell, late request, ops update), the
-- comment_extractions row stays frozen at the original — pool_heating,
-- allergies, ops_notes etc. all silently drift out of sync with reality.
-- Tonight's incident: Tiago Vidal (room 619) had heated pool added to his
-- comment as part of an upsell after check-in; system never re-extracted,
-- pool heating dashboard missed his villa.
--
-- Fix: store a hash of the source comment text on each extraction row.
-- On every cron run, the Python pipeline compares the current
-- reservations.comments hash vs the stored comment_hash. Mismatches get
-- re-extracted; matches are skipped. Self-healing without burning LLM
-- budget on unchanged comments.
--
-- Idempotent: safe to re-run.

alter table comment_extractions
  add column if not exists comment_hash text;

-- Backfill existing rows so they have non-NULL hashes — otherwise every
-- existing extraction would re-fire on the next cron run. We compute the
-- hash from the current reservations.comments which may not match what
-- the LLM saw originally, but that's fine: it just means we treat the
-- existing extraction as "current" and only re-extract when comments
-- change AFTER this point.
update comment_extractions ce
set comment_hash = encode(digest(coalesce(r.comments, ''), 'sha256'), 'hex')
from reservations r
where ce.reservation_id = r.id
  and ce.comment_hash is null;

-- Verify
select
  count(*) as total,
  count(*) filter (where comment_hash is null) as still_null
from comment_extractions;
