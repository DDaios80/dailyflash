# UPDATE statement code-review audit — 14 May 2026

Audit follow-up to Phase 56 (committee_response without acknowledged_at) and
Phase 58 (approved_at without approver_user_id). Goal: find every UPDATE
statement touching a status enum and verify it atomically writes the full
{status, _at, _by} tuple.

## Scope

- 21 SQL UPDATE statements in `db/*.sql`
- 18 `.update()` calls in edge functions
- 1 `.update()` in Python (not state-related)

## Findings

### Pattern: approve paths atomic, other transitions partial

Approve paths across all three approval flows (groups, fam_trips,
site_inspections) consistently write the full `{status, _at, _by}` tuple.
Other state transitions frequently miss `_by`. This is the same bug class
Phase 56 and Phase 58 patched, but more instances exist.

### Confirmed bugs

| # | File:line | Function | Bug |
|---|-----------|----------|-----|
| 1 | `db/phase25_ideas_chair_desk.sql:329` | `idea_need_committee` | Sets ONLY `status = 'in_discussion'`. No dedicated `in_discussion_at` / `in_discussion_by`. **Mitigated**: `ideas` has a generic `updated_at` trigger (`_touch_ideas_updated_at`) so timestamp is captured; the inserted comment row carries `author_user_id` for actor. Schema is inconsistent with `idea_acknowledge`'s explicit `acknowledged_at/by` pattern, but operational audit trail exists. |
| 2 | `db/phase25_ideas_chair_desk.sql:295` | `idea_solvable_now` | Sets `status, committee_response, resolved_at, closed_at`. Missing `resolved_by` / `closed_by`. |
| 3 | `db/phase25_ideas_chair_desk.sql:357` | `idea_to_monday` | Sets `status, for_monday_at, for_monday_reason, monday_meeting_date`. Missing `for_monday_by`. |
| 4 | `db/phase25_ideas_chair_desk.sql:554` | `idea_decide_at_monday` | Sets decision fields. Missing `decided_by` (which committee member at Monday meeting made the decision). |
| 5 | `db/phase32_admin_inapp_approval.sql:46-49` | `admin_review_fam_trip` reject branch | Sets `status='rejected', rejected_at, rejection_reason`. Missing `rejected_by_user_id` (the approve branch correctly sets `approver_user_id = auth.uid()`). |
| 6 | `db/phase32_admin_inapp_approval.sql:91-94` | `admin_review_inspection` reject branch | Same as 5 for site_inspections. |
| 7 | `db/phase34_group_approvals.sql:191-194` | `admin_review_group` reject branch | Same as 5 for groups. |

### Lower-severity findings (debatable)

| # | File:line | Function | Note |
|---|-----------|----------|------|
| 8 | `db/phase9_fam_trips.sql:247` | `mark_fam_trip_sent` | Sets `status='sent', sent_at=now()`. No `sent_by`. Debatable — "sent" is an automated email pipeline action, not a user choice. |
| 9 | `db/phase8_site_inspections.sql:261` | `mark_inspection_sent` | Same as 8 for inspections. |

### Schema gaps (not bugs, but structural)

The `ideas`, `fam_trips`, `site_inspections`, and `groups` tables have
incomplete `_by` audit columns:

- `ideas`: has `acknowledged_at + acknowledged_by` (Phase 25). Missing
  `resolved_by`, `closed_by`, `for_monday_by`, `decided_by`,
  `in_discussion_at`/`in_discussion_by`.
- `fam_trips` / `site_inspections`: has `approver_user_id` (the assigned
  approver, set at submit time). Missing `rejected_by_user_id` (a separate
  field because the rejecter may be a different admin than the originally
  assigned approver).
- `groups`: same shape as fam_trips.

## Recommended fixes (in priority order)

### P0 — De-prioritized after deeper inspection

`idea_need_committee`'s UPDATE has audit trail via two indirect paths:
- `_touch_ideas_updated_at` trigger captures the timestamp on `updated_at`
- The function inserts a comment in `idea_comments` carrying `author_user_id`

So while the schema lacks dedicated `in_discussion_at` + `in_discussion_by`
columns (inconsistent with `idea_acknowledge`'s `acknowledged_at/by`
pattern), the operational audit is intact. **Not a P0 bug.** Schema
cleanup belongs with P2.

The lesson is more about CONSISTENCY than missing data. Either every
state transition has dedicated `_at`/`_by` columns, or none do (rely on
trigger + comments). Half-and-half is the smell.

### P1 — Patch all reject branches (3 RPCs, 15 min)

Add `rejected_by_user_id` columns to `fam_trips`, `site_inspections`,
`groups`, then update `admin_review_*` RPCs to set
`rejected_by_user_id = auth.uid()` in the reject branch.

```sql
alter table fam_trips
  add column if not exists rejected_by_user_id uuid references auth.users(id);
alter table site_inspections
  add column if not exists rejected_by_user_id uuid references auth.users(id);
alter table groups
  add column if not exists rejected_by_user_id uuid references auth.users(id);

-- In each admin_review_* reject branch:
update <table> set
  status = 'rejected',
  rejected_at = now(),
  rejected_by_user_id = auth.uid(),
  rejection_reason = coalesce(p_reason, '...')
where id = ...;
```

### P2 — Complete the ideas audit columns (10 min)

Add the missing `_by` columns for ideas state transitions, then patch the
three RPCs (`idea_solvable_now`, `idea_to_monday`, `idea_decide_at_monday`)
to write them atomically.

### P3 — Structural pattern enforcement

**The root cause**: SQL doesn't enforce atomic-tuple writes. Status enum +
timestamp + actor are three independent columns; any UPDATE can touch a
subset.

**Recommended convention going forward** (paste into CLAUDE.md / contributor
docs):

> Any UPDATE that changes a status enum value MUST write the corresponding
> `{status}_at` timestamp AND the `{status}_by` user reference in the same
> statement. If `_at` and `_by` columns don't exist for the new status,
> ADD THEM in the same migration that introduces the transition.
>
> Prefer wrapping status transitions in `security definer` RPCs (not
> direct table UPDATEs from client code) so the atomic-write rule is
> enforced in one place and Lovable AI can't write a partial UPDATE.

**Optional stronger enforcement**: a generic trigger function that
validates `OLD.status != NEW.status` implies `NEW.<status>_at IS NOT
NULL AND NEW.<status>_by IS NOT NULL`. Pros: enforces at DB level. Cons:
requires per-table customization and may catch legitimate transitions.

## What's NOT broken

- All 4 approve paths (groups, fam_trips x2, ideas-acknowledge) are
  uniformly atomic. ✅
- All 3 submit-for-approval RPCs correctly write `status='pending_approval',
  approver_user_id, approver_name, submitted_at`. ✅
- Backfill scripts (Phase 28, Phase 34_1, Phase 56, Phase 58) correctly
  fix historical gaps with explicit `*_at` + `*_by` writes. ✅
- Edge function `.update()` calls (18 total) — most touch non-status
  fields. Quick spot-check found no obvious half-state bugs, but not
  exhaustively audited.

## Next steps

1. **Ship P0 fix** as a small migration (`db/phase64_*`) — single RPC patch.
2. **Decide on P1 scope** — separate migration adding `rejected_by_user_id`
   columns and patching 3 RPCs.
3. **Defer P2-P3** unless Lovable / dashboard / committee start surfacing
   "who did this?" questions that audit trail can't answer.

This audit doc itself is the deliverable for the morning's code-review
pass. Fixes are sized and queued; the user picks which to ship.
