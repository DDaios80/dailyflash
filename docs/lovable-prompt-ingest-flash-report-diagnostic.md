# Lovable diagnostic — ingest-flash-report comment_extractions upsert is still dropping writes

You confirmed the fix is deployed, but the database state is unchanged
after a fresh cron run. The edge function returns success but no rows
are actually written or updated.

## Empirical evidence

I just triggered a manual cron run at ~12:29 UTC today (2026-05-09):

- xlsx parsed: 4039 reservations
- LLM extraction: `276/276 ok, 0 failed` (fresh Claude calls, all succeeded)
- Bridge POST envelope contained: `comment_extractions: 276`
- Edge function response: `counts.comment_extractions: 276` (looks like success)
- Upload ID for that run: `0f64ca93-b20e-48ce-8b03-c588f10ee862`

But Postgres says:

```sql
select count(*), min(extracted_at)::date, max(extracted_at)::date
from comment_extractions;
-- count: 214, min == max == 2026-04-21
```

The numbers are byte-for-byte identical to what they were yesterday and
the day before. No new rows were inserted, no existing rows were updated.

Specific test cases that should have been written and weren't:

| Reservation | Comment fragment | Expected | Actual |
|-------------|------------------|----------|--------|
| Mueller (room 210, arrival 5/9) | "please make sure that the pool is heated" | row exists, `pool_heating=true` | NO ROW EXISTS |
| Yuval Ron (room 206, arrival 5/8) | "requested a pool heating - denied" | row exists, `pool_heating=false` | NO ROW EXISTS |
| Clapham (room 204, arrival 5/8) | "1 kid = ... Free Transfers" | row exists | NO ROW EXISTS |

## Three things I need from you

### 1. Show me the actual deployed upsert code

Paste the exact current code for the `comment_extractions` upsert step in
the deployed `ingest-flash-report` function. Not what was proposed — the
actual deployed version. I want to see the exact ON CONFLICT clause and
the column list. If you're calling Supabase's `.upsert(...)`, paste the
options object including `onConflict` and `ignoreDuplicates`.

### 2. Confirm the deploy timestamp

When was the function last redeployed? If less than 5 minutes ago, the
cache may not have invalidated. If hours ago, it's not a cache issue.

### 3. Check Supabase function logs for upload_id `0f64ca93-b20e-48ce-8b03-c588f10ee862`

In the Supabase Functions dashboard, find the invocation log for that
upload_id. Specifically:

- Did the function actually execute the upsert SQL?
- What did Postgres return as the affected-row count?
- Were any errors raised and silently caught (e.g., `try { upsert } catch { return success }` swallowing the real error)?

If the function is calling Postgres via a Supabase client, the response
object from `.upsert()` includes `count` and any `error`. Both fields
should be logged.

## Plus — investigate Postgres-side blockers

Even if the function code is correct, the upsert can be silently rejected
or swallowed by:

- **RLS policy** on `comment_extractions` — the service role used by the
  edge function may not have INSERT/UPDATE permission. Check
  `pg_policies where tablename = 'comment_extractions'`.
- **A BEFORE INSERT/UPDATE trigger** that returns NULL silently (which
  cancels the operation without erroring).
- **A unique constraint mismatch** — if `onConflict` references a column
  that doesn't have a unique constraint, Postgres raises an error
  (`42P10: there is no unique or exclusion constraint matching the ON
  CONFLICT specification`). If the function catches that and returns
  success, this would match the symptoms exactly.
- **A check constraint** that fails for new rows but is swallowed.

Run this in Supabase SQL editor:

```sql
-- RLS policies on comment_extractions
select * from pg_policies where tablename = 'comment_extractions';

-- All triggers on comment_extractions
select tgname, tgtype, tgenabled, pg_get_triggerdef(oid)
from pg_trigger
where tgrelid = 'public.comment_extractions'::regclass
  and not tgisinternal;

-- Constraints
select conname, contype, pg_get_constraintdef(oid)
from pg_constraint
where conrelid = 'public.comment_extractions'::regclass;
```

Look for anything that could be silently rejecting the upserts.

## Verification once you find the cause

After whatever fix you make:

```sql
select count(*), max(extracted_at)::date as last
from comment_extractions;
```

Expected: count > 214, last = today. That single query is the entire
truth signal.
