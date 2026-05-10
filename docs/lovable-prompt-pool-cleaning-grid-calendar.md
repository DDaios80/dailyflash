# Lovable prompt — Pool Cleaning panel: clone the Pool Heating layout

The current explore-page Pool Cleaning panel shows "No pools to clean
today 🌴" and "0 / 137" despite 99 pool rooms being occupied today. We
ran into this because the panel was doing its own live query against
`explore_arrival_detail` and the filter logic was off.

Stop debugging the live query. Two new payload fields are now available
that mirror the Pool Heating component's data shape exactly. **Clone the
Pool Heating component for Pool Cleaning, point it at the new fields,
done.**

## New payload fields

`flash_reports.payload` now includes two new fields, populated by tonight's
cron and already in the 2026-05-10 row (just pushed). Both mirror the
Phase 60 heating fields exactly so the Pool Cleaning panel can reuse the
Pool Heating component logic with only the field names + flag name
swapped.

### `pool_cleaning_grid` (137 entries — same shape as `pool_heating_grid`)

```json
"pool_cleaning_grid": [
  {
    "room": "325",
    "type_code": "DLXP",
    "description": "Deluxe Room Sea View 42sqm with Individual Pool",
    "heatable": false,
    "is_to_clean_today": true
  },
  ...
]
```

12 type codes (vs 7 for heating): DLXP (49), JSTEP (5), STEP (3), V1 (11),
V2 (14), VW (11), V3 (2), MANSION (1), CJSTEP (13), CPJSTP (7), CPRESP (14),
CSTEP (7) = 137 total.

`is_to_clean_today` is true when the room has a guest in-house tonight
(any reservation with arrival ≤ today < departure). 99 rooms are true
in tonight's data.

`heatable` (true/false) on each entry tells the UI whether this room
also appears in the heating grid — useful if you want a visual
distinction (e.g., a small dot on rooms that overlap both panels).

### `pool_cleaning_calendar` (same shape as `pool_heating_calendar`)

```json
"pool_cleaning_calendar": {
  "window": {
    "start": "2026-05-09",
    "end":   "2026-05-22",
    "anchor": "2026-05-10",
    "days": 14
  },
  "stays": [
    {
      "room": "349",
      "guest_name": "Cook",
      "guest_full_name": "Russell Cook",
      "arrival": "2026-04-25",
      "departure": "2026-05-09",
      "nights": 14,
      "type_code": "DLXP",
      "booked_room_category_label": "DJSTEP",
      "room_category_label": "DLXP"
    },
    ...
  ]
}
```

326 stays in tonight's 14-day window. Same shape as
`pool_heating_calendar.stays` except no `heated`/`fence` booleans (every
stay represents a pool that needs cleaning while the guest is there —
binary).

Window is identical to the heating calendar: yesterday + today + 12 days.
The two panels share a time axis.

## What to ship

Clone the Pool Heating component and adapt:

| Pool Heating | Pool Cleaning |
|---|---|
| Reads `pool_heating_grid` | Reads `pool_cleaning_grid` |
| Reads `pool_heating_calendar` | Reads `pool_cleaning_calendar` |
| 47 buttons | 137 buttons |
| Grouped by type: JSTEP, STEP, V1, V2, VW, V3, MANSION | Grouped by type: DLXP, JSTEP, STEP, V1, V2, VW, V3, MANSION, CJSTEP, CPJSTP, CPRESP, CSTEP |
| Red bg = `is_heated_today` | Red bg = `is_to_clean_today` |
| Calendar bars = heated stays only | Calendar bars = every stay (cleaning is always required while occupied) |
| Fence indicator overlay | None for cleaning |
| Override dot indicator | None for cleaning |

Specifically for the explore-page Pool Operations → Pool Cleaning tab:

1. **Top: 14-day Gantt calendar** — same component as the Pool Heating
   calendar, but reads `pool_cleaning_calendar.stays`. Render every stay
   as a bar (no fence/heated styling). 326 bars in 14 days will be a lot
   visually — consider compact rows or lazy-loading per category.
2. **Bottom: 137-button grid** — same component as the Pool Heating grid,
   but reads `pool_cleaning_grid`. Group by `type_code` in the order
   above (DLXP first since it's the bulk). Red bg when
   `is_to_clean_today === true`. Hover shows `description`.

## Drop the live-query logic

Whatever real-time `explore_arrival_detail` query the current panel
runs to derive the room list and occupancy — delete it. Use the
payload fields exclusively. Same architectural pattern as Pool Heating.

## Verification SQL after deploy

```sql
select
  jsonb_array_length(payload->'pool_cleaning_grid') as grid_count,
  (select count(*) from jsonb_array_elements(payload->'pool_cleaning_grid') g
   where (g->>'is_to_clean_today')::bool) as to_clean_today,
  jsonb_array_length(payload->'pool_cleaning_calendar'->'stays') as calendar_stays,
  payload->'pool_cleaning_calendar'->'window' as window
from flash_reports
where report_date = '2026-05-10';
```

Expected: `grid_count = 137`, `to_clean_today = 99`, `calendar_stays = 326`,
window = `{start: 5/9, end: 5/22, anchor: 5/10, days: 14}`.

The dashboard's Pool Cleaning panel should match those numbers exactly.

## Out of scope

- Pool cleaning override (manual force-clean toggle) — deferrable v2.
  No `override_*` fields on cleaning yet, doesn't seem operationally
  needed.
- Pool cleaning forecast bar chart — already exists in
  `pool_cleaning.forecast` (Phase 61). Keep that alongside the new
  Gantt+grid; they show different things (forecast = 7 daily counts,
  calendar = per-stay bars).
