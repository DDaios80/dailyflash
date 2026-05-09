# Phase 60 — Pool Heating dashboard redesign (with fence — v60.1)

Replaces the existing **Pool Heating (Auto)** card with a richer two-panel
component: a 14-day calendar (Gantt) on top, a 47-button grid below. Plus
a small auxiliary panel for fence requests in non-heatable rooms.

## What's changing

**Before:** flat list of rooms that need heating action today, derived from
`flash_reports.payload.pool_heating[]`.

**After:**
- **Calendar (top):** 14-day fortnight window (yesterday + today + 12 days).
  Each stay with heating OR fence shows as a horizontal bar spanning
  arrival → departure.
- **Grid (bottom):** all 47 heatable rooms grouped by type. Each room is a
  button with TWO independent indicators: heating (red background) and
  fence (icon overlay, e.g. fence pictogram or border).
- **Other-rooms fence panel (below the grid):** small list of rooms with
  fence requests that are NOT in the heatable grid (DLXP, DJSTEP,
  Collection categories like CSTEP, CPJSTP). These rooms have private
  pools but aren't part of the master heatable inventory.

## Heating rule

Phase 60 changes the rule. Heating service follows what the guest **booked**,
not what they got upgraded into. So:

- Booked V1, assigned V2 (upgrade within villa tier) → heated
- Booked DLX, upgraded to V1 → **not** heated
- Booked JSTEP, comment requests heating → heated
- Booked JSTEP, comment doesn't mention pool → not heated
- Booked Collection (always-on package), upgraded to V1 → not heated

## Fence rule

Pool fence is independent of heating. It comes from
`comment_extractions.pool_fence` (parsed from housekeeping comments
mentioning "pool fence", "kid-proofing", etc.). A fence request can exist
on:
- A room in the heatable grid → flag is on the grid button (`is_fence_today`)
  AND the stay appears in `pool_heating_calendar.stays` with `fence: true`
- A room NOT in the heatable grid (DLXP, DJSTEP, Collection) → the stay
  appears in `pool_fence_other_rooms` only

A stay can be `heated: true, fence: false` (most villa stays),
`heated: false, fence: true` (fence-only) or `heated: true, fence: true`
(both).

Heating implementation lives in `src/daily.py`. Fence extraction lives in
`comment_extractions` (LLM-parsed). The Lovable side just renders.

## New JSON fields in `flash_reports.payload`

The 2026-05-09 and 2026-05-10 rows already have these fields after the
manual run today.

### `pool_heating_grid` — the 47-button grid

Always exactly 47 entries, one per heatable room, in source-of-truth order
(JSTEP → STEP → V1 → V2 → VW → V3 → MANSION).

```json
"pool_heating_grid": [
  {
    "room": "329",
    "type_code": "JSTEP",
    "description": "Premium Junior Suite 42sqm with Private Pool",
    "is_heated_today": false,
    "is_fence_today": false
  },
  {
    "room": "525",
    "type_code": "JSTEP",
    "description": "Premium Junior Suite 42sqm with Private Pool",
    "is_heated_today": true,
    "is_fence_today": true
  }
]
```

`is_heated_today` = there's at least one heated stay where the report_date
falls in `[arrival, departure)`.

`is_fence_today` = at least one in-house guest tonight has a pool-fence
request. Independent from heating — both flags can be true.

### `pool_heating_calendar` — Gantt source data

```json
"pool_heating_calendar": {
  "window": {
    "start": "2026-05-09",
    "end": "2026-05-22",
    "anchor": "2026-05-10",
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
      "room_category_label": "JSTEP",
      "heated": true,
      "fence": false
    }
  ]
}
```

Stays only appear if `heated OR fence` is true. Both can be true; at
least one will be true. Bar styling can encode the combination (red for
heated, fence icon overlay for fence, both visual cues for both).

### `pool_fence_other_rooms` — fence in non-heatable rooms

```json
"pool_fence_other_rooms": [
  {
    "room": "346",
    "guest_name": "Hadler",
    "guest_full_name": "Jane Hadler",
    "arrival": "2026-04-20",
    "departure": "2026-05-01",
    "nights": 11,
    "booked_room_category_label": "DLXP",
    "room_category_label": "DLXP",
    "heated": false,
    "fence": true,
    "in_house_today": false
  }
]
```

These rooms (DLXP, DJSTEP, CSTEP, CPJSTP, etc.) have private pools but
are not in the heatable grid. The team still needs visibility on fence
requests there. `in_house_today` is true if the guest is in-house tonight
(useful for sorting / highlighting).

May be empty most days (historically rare, ~6 instances over April).

## Required UI

### Top: calendar (Gantt)

- Header row: 14 day cells, labelled by day-of-month (use `window.start..end`).
- Highlight the `anchor` column (today).
- One row per stay, sorted by arrival then room.
- Bar spans arrival → departure within the window. Show guest name and room
  inside or beside the bar. On hover: full guest name, room number, type
  code, arrival/departure dates, heated/fence status.
- Bar style:
  - `heated: true, fence: false` → solid accent color (red/orange)
  - `heated: false, fence: true` → outline-only or muted color with fence icon
  - `heated: true, fence: true` → solid accent + fence icon overlay

### Middle: grid

- 7 sections, one per `type_code`, in this order: JSTEP, STEP, V1, V2, VW,
  V3, MANSION. Section header shows the type code + room count.
- Within each section: room buttons in source-of-truth order (the JSON
  `pool_heating_grid` is already sorted).
- Each button: room number as label.
- Hover tooltip: the `description` field.
- Button visual state — TWO independent indicators:
  - `is_heated_today: true` → red background, white text
  - `is_fence_today: true` → small fence icon (or distinct border) overlaid
    on the button, regardless of heating state
  - Both true → red background AND fence icon
  - Neither → muted/grey background, no icon
- Buttons are read-only display elements (NOT clickable to toggle).

### Bottom: other-rooms fence panel

- Renders only when `pool_fence_other_rooms` is non-empty.
- Title: "Pool fence — other rooms" (or similar).
- One row per stay: room, room_category_label, guest_full_name,
  arrival → departure, in-house badge.
- Sort: in-house today first, then by arrival.

### Empty/edge states

- If `pool_heating_grid` is empty (shouldn't happen): "no data" message.
- If `pool_heating_calendar.stays` is empty: render header row only with
  "No pool heating or fence requests in the next 14 days" caption.
- If `pool_fence_other_rooms` is empty: don't render the panel at all.

## Migration / backward compat

The legacy `pool_heating` field stays in the payload for one cycle so the
existing card doesn't break mid-deploy. Once the new component is live and
verified, the backend team will remove `pool_heating` from the payload in
a follow-up patch.

## Verification SQL

```sql
-- expected: 14 days, 47 buttons, N red, M fence, K calendar stays
select
  payload->'pool_heating_calendar'->'window' as window,
  jsonb_array_length(payload->'pool_heating_grid') as grid_buttons,
  (select count(*) from jsonb_array_elements(payload->'pool_heating_grid') g
   where (g->>'is_heated_today')::bool) as red_today,
  (select count(*) from jsonb_array_elements(payload->'pool_heating_grid') g
   where (g->>'is_fence_today')::bool) as fence_today,
  jsonb_array_length(payload->'pool_heating_calendar'->'stays') as calendar_stays,
  jsonb_array_length(payload->'pool_fence_other_rooms') as other_fence_rooms
from flash_reports where report_date = current_date;
```

UI checks:

1. Grid renders 47 buttons across 7 sections.
2. Hover on any button shows the full description.
3. Tonight's red buttons match the SQL `red_today` count.
4. Fence icons appear on buttons whose `is_fence_today` is true.
5. Calendar bars show heated, fence, and combined stays distinguishably.
6. `pool_fence_other_rooms` panel renders when non-empty.
7. DLXP, DJSTEP, Collection rooms are NOT in the grid (only in
   `pool_fence_other_rooms` if they have a fence request).
