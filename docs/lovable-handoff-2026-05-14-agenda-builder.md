# Lovable handoff — Monday Agenda Builder (Phase 70 UI)

DB layer shipped at commit `946948f` (Phase 70). RPCs + tables exist. This doc is the UI spec for Lovable to ship.

---

## Backend reference (already live after Phase 70 SQL applies)

**Tables:**
- `monday_agendas` (extended) — meeting metadata, published markdown, pre_reads jsonb, duration_minutes (default 120)
- `excom_status_snapshots` — Block A bullets per (meeting_date, member_user_id)
- `agenda_items` — Block B-F structured items with owner, time, type, position

**RPCs:**
- `excom_submit_status(meeting_date, bullets text[])` — upsert self status, returns id
- `agenda_add_item(meeting_date, block, title, description, owner_label, owner_user_id, item_type, time_minutes, source_idea_id)` — append item, auto-positions, returns id
- `agenda_remove_item(item_id)` — void
- `agenda_reorder_block(meeting_date, block, ordered_item_ids uuid[])` — void
- `agenda_publish(meeting_date, rendered_markdown)` — lock + snapshot, void
- `monday_agenda_full_view(meeting_date)` — returns jsonb with full structured agenda + time budget

**Existing from Phase 25 (do not duplicate):**
- `next_monday()` — returns next Monday's date
- `idea_to_monday(idea_id, reason)` — flag idea for Monday
- `idea_decide_at_monday(idea_id, outcome, notes, action_assignee)` — record decision
- `current_committee_chair_id()` — returns current chair's user_id (null if not set)

**Permission gates (DB-enforced):**
- Read: any admin/management role
- Submit status: self only (or super_admin override)
- Manage agenda items + publish: current chair OR super_admin

---

## What to build

Two new pages on the dashboard.

### Page 1: `/excom/status` — ExCom member status submission

**Who sees it**: any admin/management user. Linked from a nav item visible to all admins.

**Two-state lifecycle (Phase 70a)**: bullets can exist in two states:
- **Draft** (`is_finalized = false`) — member's workspace. NOT visible to the chair's assembled agenda. Member can save, leave, come back, edit freely.
- **Submitted** (`is_finalized = true`) — official input. Visible in chair's Block A. Editing automatically reverts to Draft (member must re-submit).

**Layout**:

```
Page title: Your status update for the Monday meeting

Card header: "Monday <DD Month YYYY> · 13:00 Athens · 120 min"
             Due: Sunday <DD Month> 18:00 Athens

Status indicator (top of form, prominent):
  - If never saved:        "📝 Not started"
  - If draft (saved):      "📝 Draft saved · last edited <relative time> · NOT YET SUBMITTED"
  - If submitted:          "✅ Submitted to Chair <relative time> · visible in agenda"
  - If submitted + edited: "⚠️ Edits pending · please re-submit"

Form section: "Your status bullets"
  Bullet 1: [textarea, ~80 chars wide, growable]
  Bullet 2: [textarea]
  Bullet 3: [textarea]
  Bullet 4: [textarea]
  Bullet 5: [textarea]
  [+ Add another] (max 10)

Button row (state-dependent):
  - Never started state:
    [Save draft]
  - Draft state:
    [Save draft]   [Review & submit →]
  - Submitted state (clean, no edits):
    [Edit (will revert to draft)]    [Withdraw submission]
  - Submitted state (edits pending):
    [Save draft]   [Review & re-submit →]
```

**Review & submit flow**:

Click "Review & submit" opens a modal:

```
┌─────────────────────────────────────────────────┐
│  Preview your status as it will appear in       │
│  Monday's agenda                                │
│                                                 │
│  ## A. Άνοιγμα · Status snapshot                │
│                                                 │
│  **<Your name>**: <bullet 1> | <bullet 2> |     │
│                   <bullet 3>                    │
│                                                 │
│  ───────────────────────────────────────        │
│                                                 │
│  Once submitted, this becomes visible to the    │
│  Chair. You can still edit later (reverts to    │
│  draft, requires re-submit).                    │
│                                                 │
│  [← Back to edit]    [Submit to Chair ✓]        │
└─────────────────────────────────────────────────┘
```

**Withdraw flow**:

Click "Withdraw submission" → confirmation dialog → calls `excom_unfinalize_status(meeting_date)`. Bullets stay (still as draft); member can edit and re-submit.

**RPC calls**:
- "Save draft" button → `excom_submit_status(meeting_date, bullets)` — saves bullets, leaves is_finalized = false (or auto-reverts if previously submitted)
- "Submit to Chair" button (in Review modal) → `excom_submit_status(meeting_date, bullets)` THEN `excom_finalize_status(meeting_date)` — atomic save-and-finalize
- "Withdraw submission" → `excom_unfinalize_status(meeting_date)` — keeps draft, removes from agenda

**Data fetch on page load**:
- Query: `select * from excom_status_snapshots where meeting_date = (select next_monday()) and member_user_id = auth.uid()`
- Determine state from `is_finalized` flag + comparison of `updated_at` vs `finalized_at` to detect "edits pending after submit"
- Pre-fill bullets if any exist

**Edge cases**:
- Filter out empty bullets before submitting (strip whitespace, ignore empty strings)
- Min 1 bullet, max 10 (DB enforces, but show client-side validation)
- Show relative timestamps ("Saved 2 minutes ago", "Submitted 3 hours ago")
- "Edits pending" state: detect when `is_finalized = false` BUT `revision_count > 0` AND `finalized_at IS NOT NULL` — means member submitted previously, then edited
- Re-submitting an already-finalized snapshot via `excom_finalize_status` is idempotent (just bumps `finalized_at` timestamp)

---

### Page 2: `/super-admin/agenda-builder` — Chair's assembly view

**Who sees it**: super_admin only (frontend gate per the Phase 67-era role-gate convention). DB-layer RPC permissions enforce chair-or-super-admin for writes.

**Top-level layout** (responsive, but desktop-first since chairs typically build this on a laptop):

```
+---------------------------------------------------------------------+
| Monday Agenda — <DD Month YYYY>, 13:00 · 120 min                   |
| [DRAFT | PUBLISHED]   Published: <timestamp or "not yet">           |
| Total allocated: <N> / 120 min · Buffer: <120-N> min                |
+---------------------------------------------------------------------+

Left column (60%): Agenda blocks (drag-and-drop within blocks)
Right column (40%): Sidebar — unplaced for_monday ideas + "Add custom item"
```

**Left column — Agenda blocks**:

Each block as a section with:
- Heading: "A. Opening · 10 min" (fixed time for A and F, computed sum for others)
- For Block A: read-only list combining `block_A_status_snapshots` (finalized) and `pending_status_drafts` (in-progress), showing:
  - ✅ <Member name> · <N bullets> · submitted <relative time> — finalized, bullets visible (click row to expand)
  - 📝 <Member name> · <N bullets drafted> · last edited <relative time> · NOT YET SUBMITTED — chair sees the member is working on it but bullets are not yet official input (do not include in published markdown)
  - ⏳ <Member name> · not started — anyone in the expected ExCom list who has neither a draft nor a submission (frontend computes this from the expected-members list vs the union of finalized + draft)
  - [Remind via WhatsApp link] button for not-started or stale-draft cases
- For Blocks B-E: list of agenda_items in `position` order. Each row:
  - Drag handle ⋮⋮
  - Title (bold)
  - Owner_label (small text)
  - Item type badge + time_minutes (e.g., "Decision · 15 min")
  - Description (if any, smaller)
  - [Edit] [Remove]
  - "+ Add item to this block" button at the bottom of each block
- For Block F: locked single item "AOB + chair handover to <next chair name>", auto-populated, not editable

**Drag-and-drop behavior**:
- Items can be reordered within their block (drag up/down)
- On drop: call `agenda_reorder_block(meeting_date, block, ordered_item_ids)`
- Items CANNOT be dragged between blocks in this version (move = delete + re-add elsewhere)

**Right column — Sidebar**:

Section: "Ideas marked for Monday (<N> unplaced)"
- Query: `select id, body, severity, for_monday_reason from ideas where status = 'for_monday' and monday_meeting_date = <meeting_date> and id not in (select source_idea_id from agenda_items where source_idea_id is not null)`
- Each idea card: severity badge, first 100 chars of body, the for_monday_reason
- Each card has a [Drop into block] dropdown (A/B/C/D/E) — clicking adds via agenda_add_item with `source_idea_id` set

Section: "Add custom item"
- Modal form: Title, Description, Owner (free text label + optional user dropdown), Block (A-E dropdown), Type (heads_up/decision/input/scoping/apply_it/walkthrough), Time (minutes, default 5)
- Save calls agenda_add_item

**Top bar actions**:

- [Refresh] button — refetches monday_agenda_full_view
- [Preview Markdown] — opens modal with rendered agenda in the format from docs/templates/excom-monday-agenda-template.md (mostly Greek with placeholders filled, optional English summary section)
- [Publish & lock] — calls agenda_publish with the rendered markdown, sets state to PUBLISHED, locks the editing UI

**Markdown rendering logic** (client-side, in the Preview modal):

```
Καλησπέρα σε όλους,

Ευχαριστώ θερμά για τα status updates και τα νέα θέματα.
Ακολουθεί η ατζέντα της αυριανής συνάντησης.

Ώρα:     <Day> <DD Month>, 13:00
Διάρκεια: <duration_minutes> λεπτά

## Pre-reads

[for each pre_read in pre_reads jsonb:]
- <title> (<owner>) — <delivery_method>

---

## A. Άνοιγμα · 10 λεπτά

Chair rotation status, Flash Report channel για ερωτήματα μέσα στην εβδομάδα.

### Status snapshot — on-track items

[for each status_snapshot:]
- **<member_name>**: <bullet 1> | <bullet 2> | <bullet 3>

---

## B. Στρατηγικό πλαίσιο · <sum of B time_minutes> λεπτά

[for each item in block B:]
### <position>. <title>
- **OWNER**: <owner_label>
- **TYPE**: <item_type humanized>
- **TIME**: <time_minutes> λεπτά

<description>

---

## C. Carry-over αποφάσεις · <sum> λεπτά
(same pattern)

## D. Νέα στρατηγικά θέματα · <sum> λεπτά
(same pattern)

## E. Heads-up batch · <sum> λεπτά
(same pattern)

## F. Κλείσιμο

### AOB + παράδοση Chair για <next-next-Monday DD/MM>
- **OWNER**: <current chair name>
- **TYPE**: Κλείσιμο

---

Ευχαριστώ. Θα τα πούμε <Δευτέρα> στις 13:00.

<chair name>
Managing Director, Daios Cove
```

The user can copy the markdown from the modal and paste into email. (Resend integration deferred to Phase 70.3.)

---

## Edge cases / UX details

1. **Meeting date selection**: default to `next_monday()`. Chair can use a date picker in the top bar to view past meetings (read-only if published) or skip ahead.

2. **Time budget visualization**: above the blocks list, show a progress bar:
   - Allocated time / 120 min
   - Green if under 102 min (≥18 min buffer)
   - Yellow if 102-120 min
   - Red if over 120 min — block publish button until reduced or duration_minutes is bumped

3. **Status snapshot freshness indicator**: in Block A, members who haven't submitted by 24h before the meeting should have a red ⚠ icon. Members who submitted but then haven't been viewed by the chair should have a blue · indicator.

4. **Locked state after publish**: once `published_at` is set, the entire editing UI becomes read-only. To make further changes, chair clicks an "Unlock for edits" button which sets published_at back to null (requires confirmation modal).

5. **Cron-generated empty agenda**: when `generate_monday_agenda()` runs Friday 09:00, it creates an empty `monday_agendas` row with the `for_monday` ideas already snapshotted into `idea_ids[]`. The chair opens the builder and those ideas are already in the sidebar ready to drag.

---

## Verification protocol (per the Lovable false-claim history)

When you say "shipped":

1. Quote file paths + line numbers you modified (status form, agenda builder, markdown renderer, sidebar component)
2. Paste deployed code for the 6 RPC calls (excom_submit_status, agenda_add_item, agenda_remove_item, agenda_reorder_block, agenda_publish, monday_agenda_full_view subscription/fetch)
3. Screenshots:
   - `/excom/status` page with a partially-filled form
   - `/super-admin/agenda-builder` page showing the layout with at least one item in Block C and Block E (use a test meeting date if no real data yet)
   - The Preview Markdown modal rendering correctly
   - The "Publish" state showing locked editing UI
4. Only then say "shipped"

---

## Related docs in this repo

- `docs/templates/excom-monday-agenda-template.md` — the manual fill-in template (what the markdown output should look like)
- `docs/templates/excom-monday-agenda-workflow.md` — chair's weekly process
- `docs/phase70-agenda-builder-spec.md` — full design spec for this feature
- `db/phase70_agenda_builder.sql` — the migration adding tables + RPCs (must be applied before this UI can work)
