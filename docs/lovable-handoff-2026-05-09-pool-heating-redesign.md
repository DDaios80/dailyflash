# Phase 60 — Pool Heating dashboard redesign

Replaces the existing **Pool Heating (Auto)** card with a richer two-panel
component: a 14-day calendar (Gantt) on top, a 47-button grid below.

## What's changing

**Before:** flat list of rooms that need heating action today, derived from
`flash_reports.payload.pool_heating[]`.

**After:**
- **Calendar (top):** 14-day fortnight window (yesterday + today + 12 days).
  Each heated stay shows as a horizontal bar spanning arrival → departure.
- **Grid (bottom):** all 47 heatable rooms grouped by type. Each room is a
  button. Hover shows the room type description. Red background when the
  room has a guest in-house tonight whose booked category entitles them to
  heated-pool service.

## New rule for "heated"

Phase 60 changes the rule. Heating service follows what the guest **booked**,
not what they got upgraded into. So:

- Booked V1, assigned V2 (upgrade within villa tier) → heated
- Booked DLX, upgraded to V1 → **not** heated
- Booked JSTEP, comment requests heating → heated
- Booked JSTEP, comment doesn't mention pool → not heated
- Booked Collection (always-on package), upgraded to V1 → not heated (the
  upgrade target's pool is treated as a non-paid upgrade)

This is implemented entirely in `src/daily.py` — the Lovable side just
renders the data.

## New JSON fields in `flash_reports.payload`

Both fields are populated by tonight's cron (no migration needed).

### `pool_heating_grid` — the 47-button grid

Always exactly 47 entries, one per heatable room, in source-of-truth order
(JSTEP → STEP → V1 → V2 → VW → V3 → MANSION).

```json
"pool_heating_grid": [
  {
    "room": "329",
    "type_code": "JSTEP",
    "description": "Premium Junior Suite 42sqm with Private Pool",
    "is_heated_today": false
  },
  {
    "room": "525",
    "type_code": "JSTEP",
    "description": "Premium Junior Suite 42sqm with Private Pool",
    "is_heated_today": true
  },
  ...
]
```

`is_heated_today` = there's at least one heated stay where the report_date
falls in `[arrival, departure)` (i.e. the guest is in-house tonight and their
booked category triggers the heating rule above).

### `pool_heating_calendar` — Gantt source data

```json
"pool_heating_calendar": {
  "window": {
    "start": "2026-05-08",
    "end": "2026-05-21",
    "anchor": "2026-05-09",
    "days": 14
  },
  "stays": [
    {
      "room": "525",
      "guest_name": "Smith",
      "guest_full_name": "John Smith",
      "arrival": "2026-05-08",
      "departure": "2026-05-15",
      "nights": 7,
      "booked_room_category_label": "JSTEP",
      "room_category_label": "JSTEP"
    },
    ...
  ]
}
```

`window.start` = yesterday, `window.end` = today + 12 days. `anchor` is
today (the report_date). `stays` only includes heated stays (per the
Phase 60 rule); unheated occupancies in heatable rooms are not in this list.

A stay's bar may extend before `window.start` or after `window.end` if the
stay started earlier or extends later — clip it to the window for display.

## Required UI

### Top: calendar (Gantt)

- Header row: 14 day cells, labelled by day-of-month (use `window.start..end`).
- Highlight the `anchor` column (today).
- One row per stay, sorted by arrival then room.
- Bar spans arrival → departure within the window. Show guest name and room
  inside or beside the bar. On hover: full guest name, room number, type
  code, arrival/departure dates.
- Bar color: a single accent color (red/orange) is fine — the calendar only
  shows heated stays, so all bars represent the same "heated" semantic.

### Bottom: grid

- 7 sections, one per `type_code`, in this order: JSTEP, STEP, V1, V2, VW,
  V3, MANSION. Section header shows the type code + room count.
- Within each section: room buttons in source-of-truth order (the JSON
  `pool_heating_grid` is already sorted).
- Each button: room number as label, no other content visible by default.
- Hover tooltip: the `description` field (e.g. "Two Bedroom Sea View
  Wellness Villa with Private Pool 125sqm").
- Button state:
  - `is_heated_today: true` → red background, white text
  - `is_heated_today: false` → muted/grey background
- Buttons are read-only display elements (NOT clickable to toggle —
  heating state is derived from comment_extractions + booked category, not
  manually edited).

### Empty/edge states

- If `pool_heating_grid` is empty (shouldn't happen; the grid is always
  47 rows): fall back to a "no data" message.
- If `pool_heating_calendar.stays` is empty: render the empty calendar
  (header row only) with a "No heated pools in the next 14 days" caption.

## Migration / backward compat

The legacy `pool_heating` field stays in the payload for one cycle so the
existing card doesn't break mid-deploy. Once the new component is live and
verified, I'll remove `pool_heating` from the payload in a follow-up patch.

## Verification checklist

After the new component ships:

1. Grid renders 47 buttons across 7 sections.
2. Hover on any button shows the full description.
3. At least one button is red if there's a heated stay tonight (verify by
   cross-checking the calendar's bars overlapping today's column).
4. Rooms 600 (MANSION) and 761 (V3) appear in their respective sections.
5. DLXP, DJSTEP, Collection rooms are NOT in the grid.
6. Calendar shows yesterday in the leftmost column, today highlighted, and
   12 days forward.
7. A guest who booked DLX and upgraded to a villa: their stay does NOT
   appear on the calendar, and their assigned villa is NOT red.
