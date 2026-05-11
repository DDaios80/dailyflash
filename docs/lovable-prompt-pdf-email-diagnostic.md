# Lovable diagnostic — generate-flash-pdf + send-flash-email still rendering 0/0/0 for pools

Yesterday (10 May) I asked you to swap `generate-flash-pdf` and
`send-flash-email` from the legacy `payload.pool_heating[]` field to
the Phase 60 fields. You confirmed it was done. It wasn't.

## Empirical evidence (5/11 morning)

Today's daily flash PDF (attached to the 21:34 email sent last night)
shows:

```
Pool heating (0)
—
Pool fence (0)
—
Pool cleaning — 0 pools today
```

But the same `flash_reports.payload` for `report_date = 2026-05-11` has:

```sql
select
  jsonb_array_length(payload->'pool_heating_grid') as grid_count,
  (select count(*) from jsonb_array_elements(payload->'pool_heating_grid') g
   where (g->>'is_heated_today')::bool) as heated_today,
  (select count(*) from jsonb_array_elements(payload->'pool_heating_grid') g
   where (g->>'is_fence_today')::bool) as fence_today,
  payload->'pool_cleaning'->'today'->>'count' as cleaning_count,
  jsonb_array_length(payload->'pool_heating') as legacy_count
from flash_reports where report_date = '2026-05-11';
```

Result:
- `grid_count = 47`
- `heated_today = 8` (rooms 202, 203, 204, 206, 210, 217, 220, +1)
- `fence_today = 3`
- `cleaning_count = 97`
- `legacy_count = 35`

The Pool Operations explore-page panel reads `pool_heating_grid` and
shows 8/47 heated tonight correctly. So the data IS in the payload —
the PDF + email functions just aren't reading it.

## What I need from you

### 1. Show me the actual deployed code

Paste the exact code (not what you intended to ship — the literal lines
currently running in production) for both functions, specifically the
pool heating / fence / cleaning rendering blocks:

- `generate-flash-pdf`
- `send-flash-email`

I want to see which `payload.X` accessors they actually use. If they
still reference `payload.pool_heating`, the swap never happened.

### 2. If they DO reference the new fields, show me a sample render call

Run the function manually for `report_date = 2026-05-11`. Show me the
intermediate values:
- Count of heated rooms it computed
- Count of fence rooms it computed
- Cleaning count it used

If those intermediate values are 0 while the SQL above returns 8 / 3 /
97, you have a reading bug (wrong path through the JSON, e.g.
`payload.pool_heating_grid.is_heated_today` instead of
`payload.pool_heating_grid[].is_heated_today`).

### 3. Fix and redeploy

Whatever the gap is, fix it. The render logic should match the
dashboard's Pool Operations panel:

```js
// Pool heating count
const heated = (payload.pool_heating_grid ?? [])
  .filter(g => g.is_heated_today === true);
// Render: heated.length rooms, list heated.map(g => g.room)

// Pool fence count — use pool_fence_grid (Phase 61.2) for unified 137-room view,
// OR keep using pool_heating_grid.is_fence_today + pool_fence_other_rooms
const fence = (payload.pool_fence_grid ?? [])
  .filter(g => g.is_fence_today === true);

// Pool cleaning count
const cleaning = payload.pool_cleaning?.today;
// Render: cleaning.count pools, breakdown from cleaning.by_category
```

### 4. After deploy, trigger a re-run for 5/11

Re-render the PDF and re-send to d.daios@daioshotels.com only (not the
full ~64 recipient list — just to me for verification). Then I can
confirm the next morning briefing (5/12) will be correct without
waiting another full day.

## Plus: don't claim it's deployed unless you can prove it

Saturday's comment_extractions upsert bug had the same pattern — you
told me the fix was deployed; the data showed otherwise. After the
diagnostic queries proved it wasn't, you fixed for real. We lost ~6
hours to the false claim.

When you say a function is updated, verify with:
- Show the deployed code (not the proposed code in chat)
- Show a test invocation with intermediate values

If either is missing, the claim isn't credible.

## Verification SQL

After your fix and the re-run:

```sql
-- The payload data hasn't changed — this is just to confirm what should be rendered
select
  (select count(*) from jsonb_array_elements(payload->'pool_heating_grid') g
   where (g->>'is_heated_today')::bool) as expected_heated,
  payload->'pool_cleaning'->'today'->>'count' as expected_cleaning
from flash_reports where report_date = '2026-05-11';
```

The PDF and email body should match those exact numbers (heated = 8,
cleaning = 97). Not 0.
