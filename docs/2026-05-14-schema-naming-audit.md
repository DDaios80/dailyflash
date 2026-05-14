# Schema naming audit — actor and timestamp columns (2026-05-14)

Survey of every `_user_id` / `_by` / `_by_user_id` / `_at` column across `public` schema. 120 columns inspected. Goal: identify naming-convention inconsistencies and prescribe a canonical pattern for future migrations.

This audit is the structural follow-up to today's Phase 67.1 column-name hotfix (where Phase 66 + 67 assumed `assigned_user_id` but the actual column was `assigned_to_user_id`). The root cause of that bug class is the inconsistent naming surveyed here.

---

## Executive summary

**Four actor-naming patterns coexist in the schema:**

| Pattern | Example | Type | Count | Origin |
|---|---|---|---|---|
| `{verb}_by_user_id` | `rejected_by_user_id` | uuid | 6 | Phase 64 + 67 (new convention) |
| `{verb}_by` (uuid) | `acknowledged_by`, `created_by`, `updated_by` | uuid | ~14 | Pre-Phase 64 (legacy) |
| `{verb}_by` (text) | `approved_by`, `inspection_performed_by` | text | ~5 | Free-form name capture (different concept) |
| `{role}_user_id` | `approver_user_id`, `assigned_to_user_id`, `author_user_id` | uuid | ~10 | Descriptive role (also valid pattern) |

**The inconsistency causes**: (1) Phase 67.1's column-name bug (assumed pattern mismatch with reality); (2) confusion when reading code — is `_by` a uuid or a text name? You have to look at the schema each time.

**Recommendation**: canonicalize on **`{verb}_by_user_id`** for uuid actor columns and **`{verb}_by_name`** / **`{verb}_by_email`** for text columns. Existing inconsistencies queued for rename migrations.

Timestamps (`_at` suffix) are already universally consistent — no changes needed there.

---

## Canonical convention going forward

Use these patterns for new columns. Old columns can be renamed gradually (see "Migration plan" section).

### For uuid actor columns

**Pattern**: `{verb}_by_user_id`

Examples:
```sql
approved_by_user_id   uuid references auth.users(id),
rejected_by_user_id   uuid references auth.users(id),
created_by_user_id    uuid references auth.users(id),
updated_by_user_id    uuid references auth.users(id),
submitted_by_user_id  uuid references auth.users(id),
acknowledged_by_user_id uuid references auth.users(id),
```

**Why `_user_id` suffix is non-negotiable**:
- Type is immediately clear from the column name (uuid, not text)
- Distinguishes from `_by` text fields (free-form name capture)
- Pairs cleanly with the `_at` sibling column (`approved_at + approved_by_user_id`)
- Pattern-matches the Phase 64 + Phase 67 convention which we're standardizing

### For descriptive role columns (uuid, where the noun matters)

**Pattern**: `{role}_user_id`

Examples:
```sql
approver_user_id      uuid references auth.users(id),   -- the user assigned to approve
assigned_to_user_id   uuid references auth.users(id),   -- the user the work is assigned to
author_user_id        uuid references auth.users(id),   -- the original author
submitter_user_id     uuid references auth.users(id),   -- the user who submitted the idea
actor_user_id         uuid references auth.users(id),   -- generic "the actor in this event"
target_user_id        uuid references auth.users(id),   -- generic "the target of this event"
recipient_user_id     uuid references auth.users(id),   -- who received the notification
```

**When to use this vs `{verb}_by_user_id`**:
- Use **role-named** when the column represents an ongoing assignment / role (assignee, approver, author, submitter — these persist across state transitions)
- Use **verb-named** when the column captures who performed a specific action (approved_by_user_id, rejected_by_user_id — these are tied to a single event)

The two CAN coexist on the same table. Example: `fam_trips` has both:
- `approver_user_id` — the assigned approver (set at submit time)
- `approved_by_user_id` — who actually approved (set at approval time, may differ from approver)

This distinction is intentional and useful.

### For text actor columns (legacy or free-form)

**Pattern**: `{verb}_by_name` (preferred) or `{verb}_by_email`

Examples:
```sql
approved_by_email     text,    -- email of approver (e.g., for external approval flows)
generated_by_label    text,    -- system label like "ai-pipeline-v3"
inspection_performed_by_name text,   -- free-form name from the inspection PDF
```

**When to use this**: when the actor is NOT a registered user — external party, system label, free-text capture from a document.

**When NOT to use**: when the actor IS a registered user. In that case, store the uuid (`_by_user_id`) and join to `auth.users` for the display name. Denormalizing the name leads to stale data when users update their profile.

### For timestamps

**Pattern**: `{verb}_at` (already universally consistent — no changes needed)

Examples (already correct):
```sql
approved_at, rejected_at, acknowledged_at, resolved_at, closed_at,
created_at, updated_at, submitted_at, sent_at, dismissed_at, generated_at
```

---

## Inconsistencies catalogued

### 14 columns to rename: `{verb}_by` (uuid) → `{verb}_by_user_id`

These are uuid columns that follow the legacy `_by` pattern. Rename to add the `_user_id` suffix for type clarity.

| Table | Current | Target | Notes |
|---|---|---|---|
| `ideas` | `acknowledged_by` | `acknowledged_by_user_id` | Phase 25 + 56 chair-desk RPCs reference this |
| `app_admins` | `added_by` | `added_by_user_id` | **WAIT**: this is text per the audit. Confirm type before renaming. |
| `excom_rotation` | `added_by` | `added_by_user_id` | uuid |
| `external_visitors` | `added_by` | `added_by_user_id` | uuid |
| `special_attention_overrides` | `added_by` | `added_by_user_id` | uuid |
| `zoho_taxonomy` | `approved_by` | `approved_by_user_id` | uuid |
| `exco_decision_history` | `changed_by` | `changed_by_user_id` | uuid |
| `credential_email_batches` | `created_by` | `created_by_user_id` | uuid |
| `exco_decisions` | `created_by` | `created_by_user_id` | uuid |
| `exco_meeting_evaluations` | `created_by` | `created_by_user_id` | uuid (also has `updated_by`) |
| `excom_rotation_overrides` | `created_by` | `created_by_user_id` | uuid |
| `alister_dismissals` | `dismissed_by` | `dismissed_by_user_id` | uuid |
| `alister_summaries` | `generated_by` | `generated_by_user_id` | uuid (but `executive_briefings.generated_by` is also uuid — same fix) |
| `executive_briefings` | `generated_by` | `generated_by_user_id` | uuid |
| `exco_member_ratings` | `rated_by` | `rated_by_user_id` | uuid |
| `idea_anonymity_reveals` | `revealed_by` | `revealed_by_user_id` | uuid |
| `pool_heating_overrides` | `set_by` | `set_by_user_id` | uuid |
| `flash_reissue_log` | `triggered_by` | `triggered_by_user_id` | uuid |
| `onedrive_sync_runs` | `triggered_by` | `triggered_by_user_id` | uuid |
| `agent_settings` | `updated_by` | `updated_by_user_id` | uuid |
| `app_settings` | `updated_by` | `updated_by_user_id` | uuid |
| `daily_briefing` | `updated_by` | `updated_by_user_id` | uuid |
| `exco_meeting_evaluations` | `updated_by` | `updated_by_user_id` | uuid |
| `exco_member_context` | `updated_by` | `updated_by_user_id` | uuid |
| `zoho_classification_rules` | `updated_by` | `updated_by_user_id` | uuid |
| `exco_meeting_transcripts` | `uploaded_by` | `uploaded_by_user_id` | uuid |
| `exco_member_context_files` | `uploaded_by` | `uploaded_by_user_id` | uuid |
| `uploads` | `uploaded_by` | `uploaded_by_user_id` | uuid |

That's 27 rename candidates after re-counting. Bigger than I initially estimated.

### 5 columns to rename: `{verb}_by` (text) → `{verb}_by_email` or `{verb}_by_name`

These are text columns where the legacy `_by` pattern is ambiguous (looks like a uuid pattern). Rename for type clarity.

| Table | Current | Target | Notes |
|---|---|---|---|
| `app_admins` | `added_by` | `added_by_email` | Likely email; verify by inspecting a sample row |
| `flash_email_approvals` | `approved_by` | `approved_by_email` | Likely email |
| `flash_email_approvals` | `rejected_by` | `rejected_by_email` | Likely email |
| `exco_meeting_evaluations` | `generated_by` | `generated_by_label` | "ai-v3" or similar system label, not a user |
| `monday_agendas` | `generated_by` | `generated_by_label` | Same pattern |
| `exco_member_ratings` | `generated_by` | `generated_by_label` | Same pattern |
| `site_inspections` | `inspection_performed_by` | `inspection_performed_by_name` | Free-form name from inspection PDF |

### Columns that are already correct (no change needed)

These follow the new convention or use the role-named pattern correctly:

- `rejected_by_user_id` (fam_trips, groups, site_inspections) — Phase 64
- `created_by_user_id` (fam_trips, groups, site_inspections)
- `submitted_by_user_id` (idea_responses) — Phase 67
- `approver_user_id` (fam_trips, site_inspections) — role-named, correct
- `assigned_to_user_id` (ideas) — role-named, correct
- `author_user_id` (idea_comments, idea_responses) — role-named, correct
- `action_assignee_user_id` (ideas) — role-named, correct (Monday meeting action)
- `actor_user_id`, `target_user_id` (admin_user_events, role_audit_log) — role-named, correct
- `submitter_user_id` (ideas) — role-named, correct
- `recipient_user_id` (idea_reminder_fires) — role-named, correct

### Borderline / discuss

- `ideas.action_assignee_user_id` — could be `action_assigned_to_user_id` for parallelism with `assigned_to_user_id`. Both are valid; minor preference. Skip unless touched for other reasons.

---

## Migration plan (prioritized)

Each rename is roughly 30-60 min of work including:
1. Write the `ALTER TABLE ... RENAME COLUMN` migration
2. Find all RPC functions referencing the column → patch each
3. Find all RLS policies referencing the column → patch each
4. Find all frontend queries (Lovable) referencing the column → patch each (Lovable handoff)
5. Test in production via Supabase SQL editor
6. Verify nothing broke

**Total estimate for full canonicalization**: ~30 columns × ~45 min = **~20 hours of work spread over weeks**.

### Priority 1 (high-touch, fix first) — ~5 columns

Columns referenced by frequently-called RPCs or visible RLS policies. Fixing these first gives the most consistency gain per hour:

1. **`ideas.acknowledged_by`** → `acknowledged_by_user_id` (referenced by `idea_acknowledge`, `committee_update_idea`, RLS policies)
2. **`pool_heating_overrides.set_by`** → `set_by_user_id` (Phase 60.7 — fresh, low risk to touch)
3. **`agent_settings.updated_by` / `app_settings.updated_by`** → `updated_by_user_id` (touched by every admin write)
4. **`daily_briefing.updated_by`** → `updated_by_user_id` (admin-uploaded data, surfaces in dashboard)

### Priority 2 (medium-touch) — ~10 columns

Columns referenced by background jobs / less-frequent RPCs:

- `flash_reissue_log.triggered_by`, `onedrive_sync_runs.triggered_by`
- `uploads.uploaded_by`, `exco_meeting_transcripts.uploaded_by`, `exco_member_context_files.uploaded_by`
- `alister_dismissals.dismissed_by`, `alister_summaries.generated_by`, `executive_briefings.generated_by`
- `idea_anonymity_reveals.revealed_by`
- `excom_rotation.added_by`, `external_visitors.added_by`, `special_attention_overrides.added_by`

### Priority 3 (low-touch) — remaining

- `exco_*` tables (low usage so far)
- `credential_email_batches`, `excom_rotation_overrides` — niche
- `zoho_taxonomy.approved_by`, `zoho_classification_rules.updated_by` — touched only by Zoho ingest pipeline

### Priority 4 (text columns)

The text→`_email`/`_name`/`_label` renames are less critical because they're already type-distinct in practice (you can see "text" in the schema). Defer to last:

- `flash_email_approvals.approved_by` + `rejected_by` (text)
- `app_admins.added_by` (text)
- `*.generated_by` (text in several places)
- `site_inspections.inspection_performed_by` (text)

---

## Suggested migration template

```sql
-- Phase N — Schema naming: rename {table}.{old} → {new}
--
-- Part of the schema-naming canonicalization queued from the 2026-05-14
-- audit. Converts `{verb}_by` (uuid) columns to the `{verb}_by_user_id`
-- convention used by Phase 64 + 67.
--
-- This migration is purely cosmetic at the data layer — the column still
-- references auth.users(id), just with a clearer name. But it requires
-- patching every RPC / RLS / query that references the old name.

alter table {table} rename column {old} to {new};

-- Patch dependent RPCs (find via: grep -rn '{old}' db/*.sql)
create or replace function {rpc_name}(...) ... -- with {new} in body
;

-- PostgREST cache reload (always after schema changes)
notify pgrst, 'reload schema';

-- Verification
select column_name from information_schema.columns
  where table_name = '{table}'
    and column_name in ('{old}', '{new}');
-- Expected: 1 row showing {new}, 0 rows for {old}
```

---

## What NOT to do

- **Don't batch all 30 renames into one migration.** Each one has cascading code changes; bundling makes the rollback story impossible.
- **Don't rename `created_at` / `updated_at` columns** — those are universally consistent and renaming them would invalidate every audit log.
- **Don't rename role-named columns** (`approver_user_id`, `assigned_to_user_id`, etc.) — they're correctly named per the convention. Only the `_by` legacy columns need attention.
- **Don't add NEW columns with the `_by` (no `_user_id`) pattern.** Every new migration goes straight to the canonical convention.

---

## Enforcement going forward

This audit + the recommended convention should be enforced by:

1. **Code review discipline**: any new migration with a `_by` column without the `_user_id` suffix gets pushed back.
2. **The README's schema conventions section** already documents this (Phase 68 update).
3. **Future CI for migrations** (Tier 3 item) should lint new SQL for the pattern.

The Phase 67.1 hotfix bug class (assume column name without inspecting) is a symptom. The cure is naming-pattern consistency: when names follow a pattern, you can predict them. When they don't, you have to look up each one — and that's where assumptions cause bugs.
