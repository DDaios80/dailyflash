# Lovable — send-flash-email REGRESSED within 24 hours

Yesterday (5/11) you fixed `send-flash-email` to read from
`pool_heating_grid` / `pool_fence_grid` / `pool_cleaning.today.count` —
verified working, the PDF for 5/11 showed 8 heated, 3 fence, 97 cleaning.

Tonight's 5/12 evening flash went out at 20:37 with **Pool heating (0)
/ Pool fence (0) / Pool cleaning — 0 pools today**.

The payload is fine. The function regressed.

## Proof the payload is correct

```sql
select
  report_date,
  jsonb_array_length(payload->'pool_heating_grid') as grid_count,        -- 47
  (select count(*) from jsonb_array_elements(payload->'pool_heating_grid') g
   where (g->>'is_heated_today')::bool) as heated_today,                  -- 16
  (select count(*) from jsonb_array_elements(payload->'pool_fence_grid') g
   where (g->>'is_fence_today')::bool) as fence_today,                    -- 3
  payload->'pool_cleaning'->'today'->>'count' as cleaning_count,          -- 111
  payload->>'computed_at' as computed_at
from flash_reports where report_date = '2026-05-12';
-- result: 47 / 16 / 3 / 111 / 2026-05-11T20:19:13.248091Z
```

Data is correct, fresh, populated. The email rendered nothing.

## What I need NOW

### 1. Show me the ACTUAL deployed code

Open the current `send-flash-email` edge function and paste here, in
this chat, the literal current production code for the pool heating /
fence / cleaning rendering block. Not the proposed code. Not what you
wrote yesterday. The code running right now.

I expect to see references to `pool_heating_grid`, `pool_fence_grid`,
`pool_cleaning.today` — like the code you pasted yesterday:

```js
const heatingGrid = Array.isArray(payload?.pool_heating_grid) ? payload.pool_heating_grid : [];
const fenceGrid = Array.isArray(payload?.pool_fence_grid) ? payload.pool_fence_grid : [];
const heated = heatingGrid.filter((g) => g?.is_heated_today === true).map((g) => String(g.room)).sort();
const fence = fenceGrid.filter((g) => g?.is_fence_today === true).map((g) => String(g.room)).sort();
const cleaning = payload?.pool_cleaning?.today ?? { count: 0, by_category: {} };
```

If the current code references `payload.pool_heating` (the legacy
field) — that's the regression. Some later edit reverted it.

### 2. Fix and redeploy

Restore the field references to the new fields exactly as yesterday.
Redeploy.

### 3. Trigger a test re-send to d.daios@daioshotels.com

Send a fresh `send-flash-email` invocation for `report_date =
2026-05-12` with `only_recipients = ['d.daios@daioshotels.com']`. I'll
check my inbox within 60 seconds. Expected sections:
- Pool heating (16 rooms)
- Pool fence (3 rooms)
- Pool cleaning — 111 pools today

If the test email still shows 0/0/0 after the fix and re-send, the
deploy didn't take. Show me the edge function deploy log timestamp.

### 4. Tomorrow's 08:00 morning briefing

The `send-executive-briefing-email` function uses the same data shape
and same fields. Confirm it's not regressed too. Trigger a test for
2026-05-12 to me only.

If that's also broken, fix in parallel.

## Stop the pattern

This is the THIRD time this exact function's field references have
regressed:

1. Saturday — comment_extractions upsert claimed fixed, wasn't
2. Yesterday morning — send-flash-email field swap claimed fixed,
   wasn't (you admitted: "I should not have stated it as fact")
3. Tonight — same function regressed to the legacy field after a
   subsequent edit

We've spent ~12 hours of operational time across two days on what is
fundamentally a one-line code stability problem. Two structural asks:

**A.** Whatever edit triggered tonight's regression — find it in the
edge function's revision history and tell me what it was. If you can't
see the history, say so.

**B.** Stop touching `send-flash-email` for anything except this fix
unless you can show test output for both the pool sections and the
recipient handling. The function gets rewritten too aggressively for
unrelated work and the pool-sections render keeps breaking as
collateral damage.

## Verification SQL after deploy + re-send

```sql
-- Confirms the payload is what gets rendered (sanity check; should
-- match what the email shows you):
select
  (select count(*) from jsonb_array_elements(payload->'pool_heating_grid') g
   where (g->>'is_heated_today')::bool) as expected_heated,
  (select count(*) from jsonb_array_elements(payload->'pool_fence_grid') g
   where (g->>'is_fence_today')::bool) as expected_fence,
  payload->'pool_cleaning'->'today'->>'count' as expected_cleaning
from flash_reports where report_date = '2026-05-12';
```

The test email I receive should show:
- Pool heating = expected_heated (16)
- Pool fence = expected_fence (3)
- Pool cleaning = expected_cleaning (111)

If they match, fix is real. If not, something else is broken.
