# Lovable prompt — fix `generate-flash-pdf` pool heating & fence section

The printable PDF for tonight's flash (10 May 2026) shows ~34 rooms under
"POOL HEATING & FENCE" but the actual operational truth is **8 rooms
heated, 5 rooms fenced**. The PDF is rendering the legacy
`payload.pool_heating[]` field, which is the over-broad Phase 49 list.
The newer Phase 60 fields (`pool_heating_grid`, `pool_fence_other_rooms`)
aren't being read — the empty "POOL HEATING" and "POOL FENCE"
subsections below the top list are evidence the PDF generator was
*supposed* to consume them but the code isn't there.

## What to fix

In `generate-flash-pdf` edge function, replace the pool heating &
fence section logic:

### Old (current, wrong)

Reads `payload.pool_heating[]` (34 rooms tonight) and dumps as a flat
"POOL HEATING & FENCE" list at the top. Empty "POOL HEATING" and "POOL
FENCE" subsections below.

### New

Three separate, fully-populated blocks:

**1. POOL HEATING — rooms to heat tonight**

Source: `payload.pool_heating_grid[]` filtered to `is_heated_today === true`.

```js
const heated = (payload.pool_heating_grid ?? [])
  .filter(g => g.is_heated_today === true)
  .map(g => g.room)
  .sort();
```

Tonight's expected output: `["202","203","204","206","210","217","220","404"]` (8 rooms).

**2. POOL FENCE — rooms to fence tonight**

Source: union of (a) `pool_heating_grid` filtered to `is_fence_today`,
plus (b) `pool_fence_other_rooms[]` filtered to `in_house_today === true`.

```js
const fenceFromGrid = (payload.pool_heating_grid ?? [])
  .filter(g => g.is_fence_today === true)
  .map(g => g.room);
const fenceFromOther = (payload.pool_fence_other_rooms ?? [])
  .filter(o => o.in_house_today === true)
  .map(o => o.room);
const fence = [...new Set([...fenceFromGrid, ...fenceFromOther])].sort();
```

Tonight's expected: 2 from grid (`204`, `217`) plus up to 3 from
non-grid = ~5 rooms.

**3. (NEW) POOL CLEANING — operational total**

Source: `payload.pool_cleaning.today` (Phase 61). Render as a single
headline number plus by-category breakdown.

```js
const cleaning = payload.pool_cleaning?.today ?? { count: 0, by_category: {} };
// "99 pools to clean today" + breakdown table:
// DLXP 44, V1 10, VW 8, CJSTEP 12, V2 6, JSTEP 5, CPRESP 5, CPJSTP 4, STEP 3, CSTEP 2, V3 1
```

Tonight's expected: count = 99, breakdown sums to 99.

## Drop the legacy field rendering

Remove the line that reads `payload.pool_heating[]` for the top combined
list. That field is being kept in the JSON payload only as a transition
safety net during the Phase 60 migration; once this PDF fix lands we'll
drop the field from the payload too. Don't render it.

## Sample PDF layout (after fix)

```
POOL HEATING (8 rooms)
202  203  204  206  210  217  220  404

POOL FENCE (5 rooms)
204  217  346  347  701   ← grid + non-grid combined

POOL CLEANING — 99 pools to clean today
DLXP   44     V1   10
CJSTEP 12     VW    8
V2      6     JSTEP 5
CPRESP  5     STEP  3
CPJSTP  4     CSTEP 2
V3      1
```

## Verification SQL after deploy

Run against the next-day flash (or current 5/10 row):

```sql
select
  jsonb_array_length(payload->'pool_heating') as legacy_count,
  (select count(*) from jsonb_array_elements(payload->'pool_heating_grid') g
   where (g->>'is_heated_today')::bool) as red_today,
  (select count(*) from jsonb_array_elements(payload->'pool_heating_grid') g
   where (g->>'is_fence_today')::bool) as fence_today,
  jsonb_array_length(payload->'pool_fence_other_rooms') as other_fence,
  payload->'pool_cleaning'->'today'->>'count' as cleaning_count
from flash_reports
where report_date = current_date;
```

Then download the PDF and check:
1. POOL HEATING section lists exactly `red_today` rooms (8 tonight).
2. POOL FENCE section lists exactly `fence_today + other_fence` rooms
   (2 + 3 = 5 tonight, deduped if any overlap).
3. POOL CLEANING section shows the count (99 tonight) plus the
   by-category breakdown summing to that count.
4. Legacy combined list of 34 rooms is GONE.

## Out of scope

- Email body for the morning briefing (separate template, may also need
  the same fix).
- Dashboard panel rendering (Phase 60 component is already in flight).
- C2BSP edge case for cleaning count (one pool per combined-suite
  booking is already handled by the count logic; PDF just renders
  whatever the count is).
