# Lovable prompt — Pool Fence panel: clone the Pool Heating layout

The explore-page Pool Operations → Pool Fence panel needs the same
14-day Gantt + 137-button grid layout as Pool Heating and Pool Cleaning.
Two new payload fields are now available with identical shape to the
heating equivalents.

## New payload fields

`flash_reports.payload` now includes:

### `pool_fence_grid` (137 entries — mirror of `pool_cleaning_grid`)

```json
"pool_fence_grid": [
  {
    "room": "325",
    "type_code": "DLXP",
    "description": "Deluxe Room Sea View 42sqm with Individual Pool",
    "heatable": false,
    "is_fence_today": false
  },
  ...
]
```

Same 137-room universe as cleaning (every private-pool room). 12 type
codes: DLXP, JSTEP, STEP, V1, V2, VW, V3, MANSION, CJSTEP, CPJSTP,
CPRESP, CSTEP. Fence requests can come from any private-pool room
regardless of whether it's heatable, so the grid is 137 buttons not 47.

`is_fence_today` merges two existing data sources:
- `pool_heating_grid[].is_fence_today` (47 heatable rooms)
- `pool_fence_other_rooms[].in_house_today === true` (non-heatable
  rooms with fence requests, e.g. DLXP, Collection)

In tonight's 5/10 data (after the full extraction cron at 19:30 Athens
completes), expect ~5 rooms with `is_fence_today: true` — Clapham's V1
("FOC Pool Fence" in his Villa Package) and a handful of historical
fence requests if any are still in-house.

### `pool_fence_calendar` (mirror of `pool_heating_calendar`)

```json
"pool_fence_calendar": {
  "window": {
    "start": "2026-05-09",
    "end":   "2026-05-22",
    "anchor": "2026-05-10",
    "days": 14
  },
  "stays": [
    {
      "room": "204",
      "guest_name": "Clapham",
      "guest_full_name": "Robert Clapham",
      "arrival": "2026-05-08",
      "departure": "2026-05-15",
      "nights": 7,
      "type_code": "V1",
      "booked_room_category_label": "V1",
      "room_category_label": "V1",
      "fence": true,
      "heated": true
    }
  ]
}
```

Only stays where fence is requested appear here — significantly fewer
than the cleaning calendar (which includes all in-pool-room stays).

Window is identical to heating + cleaning calendars (yesterday + today
+ 12 days). The three Pool Operations panels (Heating, Fence, Cleaning)
all share the same time axis.

## What to ship

Clone the Pool Heating component (or the Pool Cleaning component once
that's live) and adapt:

| Pool Heating | Pool Fence |
|---|---|
| Reads `pool_heating_grid` | Reads `pool_fence_grid` |
| Reads `pool_heating_calendar` | Reads `pool_fence_calendar` |
| 47 buttons | 137 buttons |
| Grouped by 7 type codes | Grouped by 12 type codes (same as cleaning) |
| Red bg = `is_heated_today` | Red bg = `is_fence_today` |
| Calendar bars = heated stays | Calendar bars = fence stays only |

Group order in the grid (same as cleaning, biggest categories first):
DLXP, JSTEP, STEP, V1, V2, VW, V3, MANSION, CJSTEP, CPJSTP, CPRESP, CSTEP.

Hover on a button shows `description`. Red background when
`is_fence_today === true`.

The calendar will typically have very few bars (fence requests are
rare — historically ~6 per month). Empty state is normal: render the
header row + "No pool fence requests in the next 14 days" caption.

## Drop any live-query logic

The current Pool Fence panel — like the Cleaning one — should NOT do
its own live query against `explore_arrival_detail` or similar. The
payload is authoritative.

## Verification SQL after deploy

```sql
select
  jsonb_array_length(payload->'pool_fence_grid') as grid_count,
  (select count(*) from jsonb_array_elements(payload->'pool_fence_grid') g
   where (g->>'is_fence_today')::bool) as fence_today,
  jsonb_array_length(payload->'pool_fence_calendar'->'stays') as calendar_stays
from flash_reports
where report_date = '2026-05-10';
```

Expected after tonight's 19:30 cron: `grid_count = 137`,
`fence_today` somewhere between 0 and ~10, `calendar_stays` similar.
The dashboard's Pool Fence panel should match those numbers exactly.

## Out of scope

- Pool fence override (manual force-fence toggle) — could be added
  later if ops needs it. Phase 60.7's `pool_heating_overrides` table
  already supports `override_type = 'fence'`, so the data layer is
  ready when the UI catches up.
- Fence-specific by-category breakdown — the grid grouping already
  conveys this visually (which type codes have red buttons).
- Combined "fence in non-heatable rooms" panel from Phase 60.1
  (`pool_fence_other_rooms`) — keep it for backward compat for now,
  but the new grid supersedes it (every non-heatable fence room
  surfaces as a red button in the appropriate category section).
  Drop `pool_fence_other_rooms` rendering after this lands.
