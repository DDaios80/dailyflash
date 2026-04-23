-- Phase 10 — A-lister false-positive hardening.
--
-- Extends the A-lister tables with fields produced by the new adversarial
-- second pass:
--   photo_url           — required when is_notable=true (human visual check)
--   disprove_confidence — 0-100 score from the adversarial pass
--   disprove_reasoning  — concrete reasons counter-evidence (or lack of it)
--   nationality_aligned — 'yes' | 'unknown' | 'no'
--   review_status       — 'confirmed' | 'needs_review' | 'rejected'
--   schema_version      — bumped on prompt/schema changes to invalidate cache
--
-- Also flushes the researched_subjects cache so every guest gets re-researched
-- with the hardened prompt. ~hundred rows max, so the cost is negligible.

-- ─── alister_findings ───────────────────────────────────────────────────────
alter table alister_findings
  add column if not exists photo_url text,
  add column if not exists disprove_confidence int default 0
      check (disprove_confidence between 0 and 100),
  add column if not exists disprove_reasoning text,
  add column if not exists nationality_aligned text
      check (nationality_aligned in ('yes', 'no', 'unknown') or nationality_aligned is null),
  add column if not exists review_status text default 'needs_review'
      check (review_status in ('confirmed', 'needs_review', 'rejected')),
  add column if not exists reasoning text;  -- was researched_subjects-only; useful for audit

-- Keep the hot-path index useful — include review_status for filtering
create index if not exists alister_findings_surfacing_idx
  on alister_findings (review_status, confidence desc)
  where is_notable = true and review_status in ('confirmed', 'needs_review');


-- ─── researched_subjects (cache) ────────────────────────────────────────────
alter table researched_subjects
  add column if not exists photo_url text,
  add column if not exists disprove_confidence int default 0
      check (disprove_confidence between 0 and 100),
  add column if not exists disprove_reasoning text,
  add column if not exists nationality_aligned text
      check (nationality_aligned in ('yes', 'no', 'unknown') or nationality_aligned is null),
  add column if not exists review_status text default 'needs_review'
      check (review_status in ('confirmed', 'needs_review', 'rejected')),
  add column if not exists schema_version int default 1;

-- Flush cache: force re-research with the hardened prompt. Python-side filters
-- on schema_version, but clearing the table keeps things tidy and avoids
-- stale auditable rows that never got a disprove pass.
delete from researched_subjects where (schema_version is null or schema_version < 2);
