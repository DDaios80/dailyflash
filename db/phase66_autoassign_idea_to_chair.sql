-- Phase 66 — Auto-assign new ideas to the current Chair (2026-05-14)
--
-- Before this, newly-submitted ideas had `assigned_user_id = NULL`. A chair
-- or admin had to manually assign each idea via committee_update_idea()
-- before anyone was on the hook. The result: ideas pile up showing
-- "Unassigned" in the dashboard, easy to lose track of.
--
-- The Chair rotates weekly (via excom_rotation). The natural default
-- assignee for any new idea is whoever's chair THIS week — they own the
-- first response cycle. Subsequent rotation handovers can reassign
-- explicitly if needed (a future Phase could even auto-reassign open
-- ideas to the new chair every Monday).
--
-- This migration installs a BEFORE INSERT trigger on `ideas` that fills
-- `assigned_user_id` with `current_committee_chair_id()` IF the inserted
-- row has assigned_user_id NULL. Explicit assignment (via submit_idea or
-- direct INSERT with a value) is preserved — the trigger only fires on
-- NULL. If the chair lookup also returns NULL (no chair seeded), the
-- field stays NULL — same as today, no regression.
--
-- Existing ideas left as-is (per user choice; those were submitted under
-- different chairs, backfilling to today's chair would be arbitrary).
--
-- Apply via Supabase SQL editor.

-- ─── 1. Trigger function ───────────────────────────────────────────────

create or replace function _autoassign_idea_to_chair()
  returns trigger
  language plpgsql
  security definer
  set search_path = public
as $$
begin
  if new.assigned_user_id is null then
    new.assigned_user_id := current_committee_chair_id();
    -- current_committee_chair_id() may return NULL if no chair is seeded.
    -- That's fine: assigned_user_id stays NULL, matching prior behavior.
  end if;
  return new;
end $$;

-- ─── 2. Trigger ────────────────────────────────────────────────────────

drop trigger if exists trg_autoassign_idea_to_chair on ideas;

create trigger trg_autoassign_idea_to_chair
  before insert on ideas
  for each row
  execute function _autoassign_idea_to_chair();

-- ─── 3. PostgREST cache reload ─────────────────────────────────────────
-- Not strictly needed (the trigger doesn't change the schema cache view),
-- but harmless and consistent with the convention.

notify pgrst, 'reload schema';

-- ─── 4. Verification ───────────────────────────────────────────────────
-- Confirm the trigger is installed and the current chair is readable.

select
  tgname as trigger_name,
  tgenabled as enabled_flag,
  pg_get_triggerdef(oid) as definition
from pg_trigger
where tgrelid = 'public.ideas'::regclass
  and tgname = 'trg_autoassign_idea_to_chair';

-- Expected: 1 row showing the trigger as enabled ('O' = enabled by default).

-- Also confirm the chair lookup works (returns this week's chair UUID).
select current_committee_chair_id() as current_chair_user_id;

-- Expected: 1 row with the current chair's UUID (or NULL if no chair seeded).
