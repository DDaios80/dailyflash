# Phase 61 — Pool Cleaning panel

A new dashboard panel for the maintenance coordinator: how many pools
need cleaning today + a 7-day forecast. Distinct from the heating grid:
heating only applies to 47 rooms; cleaning applies to all 137 rooms with
private pools.

## What's new in `flash_reports.payload`

One new field, populated by tonight's cron (no migration needed). Sample
shape from today's dry-run (5/9):

```json
"pool_cleaning": {
  "window": {
    "start": "2026-05-09",
    "end":   "2026-05-15",
    "days":  7
  },
  "today": {
    "date": "2026-05-09",
    "count": 100,
    "by_category": {
      "CJSTEP": 12, "CPJSTP": 4, "CPRESP": 5, "CSTEP": 2,
      "DLXP":   44, "JSTEP":  5, "STEP":   3,
      "V1":     10, "V2":     6, "V3":     1, "VW":     8
    }
  },
  "forecast": [
    { "date": "2026-05-10", "count": 99,  "by_category": { ... } },
    { "date": "2026-05-11", "count": 95,  "by_category": { ... } },
    { "date": "2026-05-12", "count": 110, "by_category": { ... } },
    { "date": "2026-05-13", "count": 98,  "by_category": { ... } },
    { "date": "2026-05-14", "count": 108, "by_category": { ... } },
    { "date": "2026-05-15", "count": 112, "by_category": { ... } }
  ],
  "all_days": [...all 7 days including today...]
}
```

Notes on the data:
- **`count`** = number of distinct occupied rooms with private pools that
  day. One pool per room, regardless of how many reservations or guests.
- **`by_category`** = breakdown by physical room category (where the
  pool actually is). DLXP and DJSTEP collapse to "DLXP" because Opera
  uses the same actual category for both rate plans. Coordinator gets a
  clean operational view: "44 DLXP pools, 10 V1 pools, ..."
- Total of `by_category` always equals `count`.
- Categories never exceed their inventory max (DLXP ≤ 49, V1 ≤ 11, etc.).

## Inventory reference (137 rooms)

For sanity, here's the universe of categories that can appear in
`by_category`:

| Category | Max rooms | Description |
|---|---:|---|
| DLXP | 49 | Deluxe Sea View 42sqm with Individual Pool (HB & RC bookings collapse here) |
| JSTEP | 5 | Premium Junior Suite 42sqm with Private Pool |
| STEP | 3 | One Bedroom Suite Sea View with Private Pool 65sqm |
| V1 | 11 | One Bedroom Waterfront Villa with Private Pool 95sqm |
| V2 | 14 | Two Bedroom Villa with Private Pool 115sqm |
| VW | 11 | Two Bedroom Wellness Villa with Private Pool 125sqm |
| V3 | 2 | Three Bedroom Villa with Private Pool 130sqm |
| MANSION | 1 | The Mansion 550sqm |
| CJSTEP | 13 | Collection Junior Suite Sea View 42sqm with private pool |
| CPJSTP | 7 | Collection Premium Junior Suite 42sqm with private pool |
| CPRESP | 14 | Collection Premium One Bedroom Suite 85sqm with private pool |
| CSTEP | 7 | Collection One Bedroom Suite 65sqm with private pool |

C2BSP (Collection Two Bedroom) is a combination of CJSTEP+CPRESP rooms
under one booking — no separate physical inventory. Counted via the
component room number in whichever category that room belongs to.

## Required UI

A new panel titled **"Pool Cleaning"** below the existing pool heating
section. Three blocks:

### Block 1 — today's headline number

Large heading: **"X pools to clean today"** where X = `pool_cleaning.today.count`.
Subtitle: the date.

### Block 2 — by-category breakdown

A two-column grid or compact table showing each `by_category` entry
sorted alphabetically. Examples:

```
DLXP 44     V1 10
CJSTEP 12   VW  8
CPRESP  5   V2  6
JSTEP   5   STEP 3
CSTEP   2   V3   1
CPJSTP  4   MANSION 1*
```

(\* MANSION only appears when the Mansion is occupied, so most days it
won't be in the breakdown.)

If a category has zero today, omit it. Don't pad with zeros for
unoccupied categories.

### Block 3 — 7-day forecast

A small bar chart or compact list:

```
Today    May 9   ▓▓▓▓▓▓▓▓▓▓ 100
Sat      May 10  ▓▓▓▓▓▓▓▓▓▓  99
Sun      May 11  ▓▓▓▓▓▓▓▓▓▓  95
Mon      May 12  ▓▓▓▓▓▓▓▓▓▓▓ 110
Tue      May 13  ▓▓▓▓▓▓▓▓▓▓  98
Wed      May 14  ▓▓▓▓▓▓▓▓▓▓▓ 108
Thu      May 15  ▓▓▓▓▓▓▓▓▓▓▓ 112
```

Hover/click on a future day → expand to show that day's `by_category`
breakdown (same table style as Block 2 but for that future date).

Source: `pool_cleaning.all_days[]` (7 entries, today + 6 forward).

### Empty state

If `pool_cleaning.today.count == 0` (unlikely outside off-season): show
"No pools to clean today" with a celebratory tone.

## Verification SQL

Once tonight's cron runs:

```sql
select
  payload->'pool_cleaning'->'today'->>'count' as today_count,
  payload->'pool_cleaning'->'today'->'by_category' as today_breakdown,
  jsonb_array_length(payload->'pool_cleaning'->'forecast') as forecast_days
from flash_reports
where report_date = current_date;
```

Expected: `today_count` ~95-115, `forecast_days = 6`, breakdown total
matches `today_count`.

## Out of scope (for v1)

- HB-vs-RC rate-plan split on DLXP rooms (Opera collapses these in
  `room_category_label`; would need to use `booked_room_category_label`
  separately for that view). Add later if the coordinator needs it.
- Same-day turnover counting (a room departing AM + arriving PM is two
  cleanings; we count it as 1). Add later if ops asks.
- Historical "pools cleaned yesterday" — for now the panel is forward-
  looking only.
