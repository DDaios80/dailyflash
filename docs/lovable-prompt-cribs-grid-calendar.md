# Lovable prompt — Cribs panel: clone the Pool Heating layout

New Phase 62 ships three payload fields for cribs operations, with the
same shape as the Pool Heating / Cleaning / Fence triad. The explore page
should get a new "Cribs" tab (probably next to "Kids") that mirrors the
Pool Heating component's layout: 14-day Gantt calendar on top, grid below,
plus a today-summary card.

## New payload fields

Already populated in `flash_reports.payload` for 5/11 (just pushed by the
manual run).

### `cribs.today` and forecast

```json
"cribs": {
  "window": { "start": "2026-05-11", "end": "2026-05-17", "days": 7 },
  "today": {
    "date": "2026-05-11",
    "count": 33,
    "rooms": [
      { "room": "144", "cribs": 1, "children": 2, "guests": ["Laura Hopmann"], "guest_full_name": "Laura Hopmann" },
      ...
    ]
  },
  "forecast": [...],
  "all_days": [
    { "date": "2026-05-11", "cribs": 33, "rooms": 31 },
    { "date": "2026-05-12", "cribs": 38, "rooms": 36 },
    { "date": "2026-05-13", "cribs": 38, "rooms": 36 },
    { "date": "2026-05-14", "cribs": 35, "rooms": 34 },
    { "date": "2026-05-15", "cribs": 46, "rooms": 44 },
    { "date": "2026-05-16", "cribs": 53, "rooms": 51 },
    { "date": "2026-05-17", "cribs": 57, "rooms": 55 }
  ]
}
```

Tonight's truth: 33 cribs in 31 rooms (matches the overview-card "33 cribs"
already shown on the dashboard).

### `cribs_grid` — dynamic 90-room grid

```json
"cribs_grid": [
  {
    "room": "107",
    "is_crib_today": false,
    "cribs_today": 0,
    "children_today": 0,
    "stays_in_window": 1,
    "max_cribs_in_window": 1
  },
  ...
]
```

UNLIKE pool heating/cleaning, the cribs grid is DYNAMIC. The room set is
not a fixed registry — any room can request a crib. The grid contains
ONLY rooms that have a crib stay somewhere in the 14-day window (90 rooms
tonight). Rooms appear sorted by room number.

`is_crib_today` is true when a crib is in that room TONIGHT. `cribs_today`
is the count (typically 1, sometimes 2 for twins). `stays_in_window` and
`max_cribs_in_window` give visual planning context (how often this room
churns through cribs, max simultaneous).

### `cribs_calendar` — 14-day Gantt source

```json
"cribs_calendar": {
  "window": {
    "start": "2026-05-10",
    "end": "2026-05-23",
    "anchor": "2026-05-11",
    "days": 14
  },
  "stays": [
    {
      "room": "144",
      "guest_name": "Hopmann",
      "guest_full_name": "Laura Hopmann",
      "arrival": "2026-05-10",
      "departure": "2026-05-14",
      "nights": 4,
      "cribs": 1,
      "children": 2,
      "room_category_label": "DLX",
      "booked_room_category_label": "DLX"
    },
    ...
  ]
}
```

Same window as the other Pool Operations calendars (heating / cleaning /
fence). 112 crib stays in tonight's 14-day window.

## What to ship

Clone the Pool Heating component for cribs. Add a new "Cribs" tab in the
explore-page Pool Operations area (or the Kids area — wherever fits best
operationally; housekeeping owns cribs, so probably Pool Operations
since the rest of housekeeping ops lives there).

| Pool Heating | Cribs |
|---|---|
| Reads `pool_heating_grid` (47 fixed) | Reads `cribs_grid` (~90 dynamic) |
| Reads `pool_heating_calendar` | Reads `cribs_calendar` |
| `is_heated_today` bool | `is_crib_today` bool |
| — | `cribs_today` count (display next to each red button) |
| — | `cribs.today.count` headline (e.g. "33 cribs in 31 rooms") |
| — | `cribs.all_days` 7-day forecast bar chart |

### Headline today card

```
33 cribs in 31 rooms tonight

Forecast (next 7 days):
   11 May   33 ▓▓▓▓▓▓
   12 May   38 ▓▓▓▓▓▓▓
   13 May   38 ▓▓▓▓▓▓▓
   14 May   35 ▓▓▓▓▓▓▓
   15 May   46 ▓▓▓▓▓▓▓▓▓
   16 May   53 ▓▓▓▓▓▓▓▓▓▓
   17 May   57 ▓▓▓▓▓▓▓▓▓▓▓
```

The week is trending up — useful operational signal.

### Grid

Dynamic, ~90 buttons. Display each room as a button. Red when
`is_crib_today === true`. Show `cribs_today` count badge on red buttons
(for the 2-twins-1-room case). Hover: guest name(s), children count,
nights remaining.

Suggested grouping: sort by room number ascending. Optionally group by
floor (200s, 300s, 400s, 500s, 600s, 700s, 800s) if visually helpful.

### Calendar (Gantt)

Same renderer as the Pool Heating Gantt, just feed it `cribs_calendar.stays`.
Each bar shows guest_full_name + cribs count. Hover for full detail
(arrival, departure, nights, children count).

### Today's room list (under the headline)

Render `cribs.today.rooms` as a table:

| Room | Cribs | Children | Guest |
|------|------:|---------:|-------|
| 144  | 1     | 2        | Laura Hopmann |
| 217  | 1     | 2        | Benjamin Mang |
| ...  | ...   | ...      | ... |

Useful for housekeeping to print or scan before the morning crib rounds.

## Verification SQL after deploy

```sql
select
  payload->'cribs'->'today'->>'count' as today_count,
  jsonb_array_length(payload->'cribs'->'today'->'rooms') as today_rooms,
  jsonb_array_length(payload->'cribs_grid') as grid_size,
  (select count(*) from jsonb_array_elements(payload->'cribs_grid') g
   where (g->>'is_crib_today')::bool) as grid_today,
  jsonb_array_length(payload->'cribs_calendar'->'stays') as calendar_stays
from flash_reports where report_date = '2026-05-11';
```

Expected: today_count=33, today_rooms=31, grid_size=90, grid_today=31,
calendar_stays=112.

## Out of scope

- Crib inventory tracking (e.g., "we have 40 cribs total, 33 are deployed,
  7 in storage"). Would need a separate inventory table. Useful future
  enhancement when the hotel has crib supply concerns.
- Pickup/delivery scheduling (which trolley, what time). Operational
  layer beyond what the daily flash supports.
- Crib age limits / safety expiry dates. Inventory management territory.

## What's the same across heating / cleaning / fence / cribs

All four Pool Operations panels now share:
- 14-day Gantt calendar with same window (yesterday + today + 12 days,
  anchored on report_date)
- Grid (fixed inventory for heating/cleaning/fence; dynamic for cribs)
- Per-day today flag (`is_heated_today` / `is_to_clean_today` /
  `is_fence_today` / `is_crib_today`)

Lovable can build one parameterized component that handles all four
with different `field_name` / `flag_name` props.
