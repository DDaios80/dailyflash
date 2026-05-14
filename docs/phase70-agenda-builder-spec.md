# Phase 70 — Monday Agenda Builder (spec)

Date: 2026-05-14, hour 14 of a long session. Built end-to-end (DB + RPCs + Lovable prompt) in one go per user request. May need refinement tomorrow with fresh eyes — see "Known late-night-velocity caveats" at the bottom.

---

## Goal

Convert the manual chair workflow (sample: 11 May 2026 agenda PDF — ExCom_Meeting_11.05_Agenda.pdf) into a tooled flow:

- ExCom members submit their status snapshots by Sunday via a form
- Chair assembles the agenda blocks (A through F) via drag-and-drop, drawing from submitted status + `for_monday` ideas + custom items
- Chair publishes the agenda → snapshot stored in DB, optionally rendered as markdown to send
- Post-meeting: chair records each decision via existing `idea_decide_at_monday()` RPC

Builds on **Phase 25 scaffolding** (`monday_agendas` table, `generate_monday_agenda()`, `monday_agenda_view()`, `idea_decide_at_monday()`) rather than replacing it.

---

## Schema additions

### Extend `monday_agendas`

```sql
alter table monday_agendas
  add column if not exists published_at timestamptz,
  add column if not exists published_by_user_id uuid references auth.users(id),
  add column if not exists published_markdown text,
  add column if not exists pre_reads jsonb default '[]'::jsonb,
  add column if not exists duration_minutes int not null default 120;
```

### New table: `excom_status_snapshots`

One row per ExCom member per Monday meeting. Status updates for Block A.

```sql
create table if not exists excom_status_snapshots (
  id uuid primary key default gen_random_uuid(),
  meeting_date date not null references monday_agendas(meeting_date) on delete cascade,
  member_user_id uuid not null references auth.users(id),
  member_name text not null,           -- denormalized for display
  bullets text[] not null default '{}',-- 2-5 status update bullets
  submitted_at timestamptz not null default now(),
  updated_at timestamptz,
  revision_count int not null default 0,
  unique (meeting_date, member_user_id)
);
```

### New table: `agenda_items`

The structured agenda content. Each row = one item in one block.

```sql
create table if not exists agenda_items (
  id uuid primary key default gen_random_uuid(),
  meeting_date date not null references monday_agendas(meeting_date) on delete cascade,
  block text not null check (block in ('A_opening','B_strategic','C_carryover','D_new','E_headsup','F_close')),
  position int not null,               -- ordering within block
  title text not null,
  description text,                    -- optional context paragraph
  owner_label text not null,           -- "Μαρία / Ολιβιέ / Δημήτρης" or single name
  owner_user_id uuid references auth.users(id),  -- nullable for joint-owner items
  item_type text check (item_type in (
    'heads_up','decision','input','scoping','apply_it','walkthrough','close'
  )),
  time_minutes int not null check (time_minutes between 1 and 60),
  source_idea_id uuid references ideas(id) on delete set null,
  created_at timestamptz not null default now(),
  created_by_user_id uuid references auth.users(id),
  unique (meeting_date, block, position)
);
```

---

## New RPCs

### `excom_submit_status(p_meeting_date date, p_bullets text[]) returns uuid`

Upsert current user's status for that meeting. Self-only edit. Increments revision_count on re-submit.

### `agenda_add_item(...) returns uuid`

Append item to a block. Auto-assigns next `position` value. Chair / super_admin only.

### `agenda_remove_item(p_item_id uuid) returns void`

Delete item. Position gaps left; reorder fixes them.

### `agenda_reorder_block(p_meeting_date date, p_block text, p_ordered_item_ids uuid[]) returns void`

Bulk re-order items within a block. Chair / super_admin only.

### `agenda_publish(p_meeting_date date, p_rendered_markdown text) returns void`

Set `published_at`, `published_by_user_id`, store the markdown snapshot. Chair / super_admin only.

### `monday_agenda_full_view(p_meeting_date date) returns jsonb`

Returns the structured agenda: meta + pre_reads + Block A status snapshots + Blocks B-F items (grouped) + total time vs target. Used by both chair view and ExCom member preview.

---

## Lovable UI design

### ExCom member view at `/excom/status`

Visible to any authenticated admin/management user.

```
+--------------------------------------------------+
| Your status update for Monday 18 May 2026, 13:00 |
| Due: Sunday 17 May, 18:00 Athens                 |
|                                                  |
| Bullet 1: [_________________________________]    |
| Bullet 2: [_________________________________]    |
| Bullet 3: [_________________________________]    |
| Bullet 4: [_________________________________]    |
| Bullet 5: [_________________________________]    |
|                                                  |
| Status: Not yet submitted                        |
| (or: Submitted 16 May 14:32 · revised 1 time)    |
|                                                  |
| [Save draft]    [Submit / Update]                |
+--------------------------------------------------+
```

Calls `excom_submit_status(meeting_date, bullets)` on Submit.

### Chair view at `/super-admin/agenda-builder`

Visible to chair / super_admin only.

```
+--------------------------------------------------------------+
| Monday Agenda — 18 May 2026, 13:00 · 120 min                |
| [DRAFT] · Published: not yet                                 |
|                                                              |
| === Block A: Status snapshots (10 min) ===                  |
| ✓ Maria · 4 bullets · submitted 16/5                        |
| ✓ Olivier · 3 bullets · submitted 17/5                      |
| · Kyrillos · NOT SUBMITTED (chase: WhatsApp link)            |
| ✓ Antonios · 5 bullets · submitted 17/5                     |
| · Valia · NOT SUBMITTED                                      |
| ✓ Georgios · 4 bullets · submitted 16/5                     |
| ✓ Dimitrios · 4 bullets · submitted 17/5                    |
|                                                              |
| === Block B: Strategic framework (15 min) ===  [+ add item]  |
| · drag-and-drop reorderable                                  |
|                                                              |
| === Block C: Carry-over decisions (45 min) ===  [+ add item] |
| · drag-and-drop reorderable                                  |
| · sidebar: 7 ideas marked `for_monday` not yet placed        |
|                                                              |
| === Block D: New strategic items (20 min) === [+ add item]   |
| ...                                                          |
|                                                              |
| === Block E: Heads-up batch (12 min) === [+ add item]        |
| ...                                                          |
|                                                              |
| === Block F: Close ===                                       |
| AOB + handover to next chair (auto-filled)                   |
|                                                              |
| Total: 102 / 120 min   ✓ within budget (18 min buffer)       |
|                                                              |
| [Preview Markdown] [Publish & lock]                          |
+--------------------------------------------------------------+
```

The sidebar shows unplaced `for_monday` ideas the chair can drag into Block C. The "+ add item" buttons open a modal: title, description, owner, time, type.

### Markdown preview / publish

Preview opens a modal showing the rendered agenda in the format from `docs/templates/excom-monday-agenda-template.md`. Publish writes the markdown to `monday_agendas.published_markdown`, sets `published_at`, locks further edits. Chair can then copy/paste into email or trigger a Resend send.

---

## Permission model

| Role | Read agenda | Submit status | Manage agenda items | Publish |
|---|---|---|---|---|
| ExCom member (`admin`/`management`) | ✅ | ✅ own only | ❌ | ❌ |
| Current chair | ✅ | ✅ own only | ✅ | ✅ |
| Super admin | ✅ | ✅ for anyone | ✅ | ✅ |

Chair identified via `current_committee_chair_id() = auth.uid()`.

---

## Out of scope for Phase 70 (queued as 70.1, 70.2, 70.3)

- **Email automation** (Thursday call-for-input, Sunday deadline reminder) → Phase 70.1
- **PDF generation** of the published agenda → Phase 70.2 (reuse `generate-flash-pdf` pattern)
- **Resend integration** to email the published agenda to ExCom → Phase 70.3
- **Mobile-friendly status form** → Phase 70.4

The chair can manually copy/paste the markdown for now. Automation later.

---

## Known late-night-velocity caveats

This was built at hour 14 of a long session. Quality checks to do tomorrow with fresh eyes:

1. **Schema column names follow today's audit convention?** Spot-check: `submitted_at`, `updated_at`, `published_by_user_id`, `created_by_user_id`. Should be consistent with the schema-audit conventions (`{verb}_by_user_id`).
2. **RPCs grant to all 4 PostgREST roles?** Yes per Phase 51 convention — confirm in the migration verification.
3. **Permission gates correct?** Chair-only writes via `current_committee_chair_id() = auth.uid()` check.
4. **Foreign key cascades**: `agenda_items.meeting_date references monday_agendas(meeting_date) on delete cascade` — confirm `monday_agendas` has `meeting_date` as primary key (Phase 25 says yes).
5. **`excom_status_snapshots` unique constraint** prevents double-submission. Re-submit goes via UPDATE path (revision_count increments).
6. **Item type and block check constraints** — text vs enum tradeoff. I chose text for flexibility; if you prefer enum types, easy to migrate later.

If any of these feel off tomorrow, the migration is in `db/phase70_agenda_builder.sql` — safe to revise before applying to production.
