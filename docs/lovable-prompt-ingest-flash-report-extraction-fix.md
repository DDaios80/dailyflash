# Lovable prompt — fix `ingest-flash-report` silently dropping comment_extractions

## Problem

The `ingest-flash-report` edge function returns success counts that match
the post (e.g., `counts.comment_extractions: 252`), but the
`comment_extractions` table hasn't been updated since 2026-04-21. All 214
rows have `extracted_at = 2026-04-21T00:31:48`. New reservations created
after April 21 have ZERO comment_extractions rows.

Confirmed via direct DB query:

```sql
select count(*), min(extracted_at), max(extracted_at) from comment_extractions;
-- count: 214, min == max == 2026-04-21T00:31:48
```

Cron runs full LLM extraction nightly at 19:30 Athens. Each run
successfully extracts 252-276 comments (`252/252 ok, 0 failed`) and posts
them to `ingest-flash-report`. The edge function's response confirms the
correct count was received. But the DB never sees the new data.

Concrete impact today:
- Mueller (room 210, arrival 5/9, comment "please make sure that the
  pool is heated") has NO comment_extractions row at all.
- Yuval Ron (room 206, arrival 5/8, comment "requested a pool heating —
  denied") has NO comment_extractions row.
- Clapham (room 204, arrival 5/8) has NO row.

These should each have a row, with `pool_heating` set per the LLM's
classification. They don't.

## Diagnosis

The edge function appears to be doing `INSERT ... ON CONFLICT DO NOTHING`
on `comment_extractions`, keyed on `reservation_id`. So:
- Reservations that had an extraction row from April 21 stay frozen with
  the April 21 values forever — comment edits never trigger an update.
- Reservations whose IDs didn't exist on April 21 never get a row,
  because Phase 52's reservation-ID preservation logic (resv_name_id
  match) doesn't insert new extraction rows for never-extracted
  reservations.

## Required fix

Change the `comment_extractions` upsert in `ingest-flash-report` to
overwrite on conflict. The reservation_id is the natural key.

The upsert MUST overwrite these columns when an extraction row already
exists for the reservation_id:

- `pool_heating`
- `pool_fence`
- `late_checkout`
- `free_transfer`
- `free_upgrade`
- `honeymoon`
- `vip_flag`
- `already_in_house`
- `allergies` (the boolean)
- `allergies_text`
- `amenities`
- `payment_notes`
- `ops_notes`
- `comment_hash` (Phase 48)
- `extracted_at`

DO NOT overwrite `id` or `reservation_id` (the join keys themselves).

In Postgres terms (target shape):

```sql
INSERT INTO comment_extractions (reservation_id, pool_heating, pool_fence,
  late_checkout, free_transfer, free_upgrade, honeymoon, vip_flag,
  already_in_house, allergies, allergies_text, amenities, payment_notes,
  ops_notes, comment_hash, extracted_at)
VALUES (...)
ON CONFLICT (reservation_id) DO UPDATE SET
  pool_heating     = EXCLUDED.pool_heating,
  pool_fence       = EXCLUDED.pool_fence,
  late_checkout    = EXCLUDED.late_checkout,
  free_transfer    = EXCLUDED.free_transfer,
  free_upgrade     = EXCLUDED.free_upgrade,
  honeymoon        = EXCLUDED.honeymoon,
  vip_flag         = EXCLUDED.vip_flag,
  already_in_house = EXCLUDED.already_in_house,
  allergies        = EXCLUDED.allergies,
  allergies_text   = EXCLUDED.allergies_text,
  amenities        = EXCLUDED.amenities,
  payment_notes    = EXCLUDED.payment_notes,
  ops_notes        = EXCLUDED.ops_notes,
  comment_hash     = EXCLUDED.comment_hash,
  extracted_at     = EXCLUDED.extracted_at;
```

If the function uses Supabase's `.upsert(..., { onConflict: ... })`, set
`onConflict: 'reservation_id'` and ensure `ignoreDuplicates: false` (the
default; do NOT set it to true).

## Verification after deploy

1. Trigger the cron manually OR wait for the 19:30 Athens cron.
2. Run this SQL — both numbers should match today's date:

```sql
select
  count(*) as total,
  min(extracted_at)::date as first_extraction,
  max(extracted_at)::date as last_extraction,
  current_date as today
from comment_extractions;
```

Expected after fix: `last_extraction = today` (UTC date acceptable).
Total should jump from 214 to ~276 (matches the cron's "in-scope
reservations" count).

3. Check Mueller specifically:

```sql
select ce.pool_heating, ce.pool_fence, ce.extracted_at
from comment_extractions ce
join reservations r on r.id = ce.reservation_id
where r.guest_name ilike '%Mueller%'
  and r.room = '210'
  and r.arrival between '2026-05-09' and '2026-05-09';
```

Expected: 1 row, `pool_heating = true`, `extracted_at = today`. Her
comment literally says "please make sure that the pool is heated" — the
LLM should set this.

4. Check Yuval Ron (negation test):

```sql
select ce.pool_heating, r.comments
from comment_extractions ce
join reservations r on r.id = ce.reservation_id
where r.guest_name ilike '%Ron%'
  and r.room = '206'
  and r.arrival between '2026-05-08' and '2026-05-08';
```

Her comment says "requested a pool heating — denied". The LLM probably
correctly returns `pool_heating = false`, but worth eyeballing.

## Out of scope for this fix

- Strengthening the LLM extraction prompt (denial handling, Greek
  phrasings) — separate Lovable prompt to follow.
- Adding a manual-override UI on the heating grid — separate Lovable
  prompt to follow.
- Daily freshness alerting — being added to the Python cron path
  separately (post-cron sanity check that warns if max(extracted_at) is
  older than today).
