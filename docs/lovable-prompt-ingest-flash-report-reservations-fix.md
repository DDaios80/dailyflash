# Lovable prompt — `ingest-flash-report` reservations upsert is also dropping data

This is the same shape of bug as the `comment_extractions` upsert you just
fixed, but on the `reservations` table this time. Symptom: bridge POST
succeeds with `counts.reservations: 4039`, response shows a verification
upload object, but the actual `reservations` rows in Postgres don't
reflect today's data.

## Empirical evidence

I just (12:29 UTC, 2026-05-09) triggered a manual cron run with full
extraction. The post returned:

```json
"counts": {
  "reservations": 4039,
  "comment_extractions": 276,
  ...
},
"verification": {
  "upload_id": "0f64ca93-b20e-48ce-8b03-c588f10ee862",
  "upload": { ... "row_count": 4039 ... }
}
```

But Postgres direct query says:

```sql
-- Reservations table count
select count(*) from reservations;
-- result: 3685 (NOT 4039)

-- 60% have NULL rooms — these are likely older bookings whose room
-- assignments came in updates that never landed
select count(*) from reservations where room is null;
-- result: 2207

-- Latest entry in the uploads metadata table
select uploaded_at, filename, row_count from uploads
order by uploaded_at desc limit 5;
-- result: only ONE row, dated 2026-04-21,
--         "Daily Flash 20.04.2026.xlsx", row_count=3685
```

The `uploads` table appears frozen at April 21. Either:
- (a) The uploads table insert silently fails on each cron, OR
- (b) Each new upload INSERT immediately gets DELETED by Phase 41/52
  cleanup logic that's misfiring, OR
- (c) The reservations upsert+uploads insert happens in a transaction
  that rolls back at the end

## Specific test cases

Two reservations from today's xlsx that should be in the DB but aren't:

**Bardonnet (resv_name_id 3307173):** xlsx says room=209, booked V1, in-house
5/7→5/10. DB has him but with `room = NULL`.

```sql
select id, resv_name_id, room, room_category_label, booked_room_category_label,
       arrival, departure
from reservations where guest_name ilike '%Bardonnet%';
-- expected: room='209', booked='V1'
-- actual:   room=NULL
```

**Terrasson (resv_name_id 3308422):** xlsx says room=220, booked V1, actual
VW, in-house 5/7→5/13. DB doesn't have him at all.

```sql
select id, resv_name_id, room from reservations
where guest_name ilike '%errasson%';
-- expected: 1 row, room='220'
-- actual:   0 rows
```

These two are NOT outliers — they're representative. 60% of `reservations`
rows have NULL rooms because ROOM ASSIGNMENTS are exactly the kind of data
that arrives on each daily upload as guests are placed. If updates aren't
landing, room assignments never propagate.

## What to investigate

### 1. The reservations upsert in `ingest-flash-report`

Paste the actual deployed code for the `reservations` upsert step. Same
checks as for the comment_extractions fix:

- What's the `onConflict` column?
- Is there a UNIQUE constraint matching that column?
- Is it `ON CONFLICT DO UPDATE` or `DO NOTHING`?
- Which columns get overwritten on conflict? (room must be in the list)
- Is the function using SUPABASE_SERVICE_ROLE_KEY? (RLS bypass is critical
  if the table has RLS like comment_extractions did)

### 2. The uploads table behavior

```sql
-- RLS check
select * from pg_policies where tablename = 'uploads';
select tablename, rowsecurity from pg_tables
where schemaname = 'public' and tablename = 'uploads';

-- Was the upload from my run actually inserted?
select id, report_date, filename, uploaded_at, row_count
from uploads
where id = '0f64ca93-b20e-48ce-8b03-c588f10ee862';
-- expected: 1 row
-- if 0 rows, the insert never landed (despite the response saying success)
```

If the row IS there, then the issue is just my query was missing it (e.g.,
RLS hides it from authenticated reads). If 0 rows, the upload itself isn't
landing.

### 3. Cleanup-on-replace logic

Phase 41/52 was supposed to delete old uploads when a new one arrives for
the same date, while preserving comment_extractions. Verify the cleanup
logic isn't deleting too aggressively:

```sql
select trigger_name, event_manipulation, action_statement, action_timing
from information_schema.triggers
where event_object_table in ('uploads', 'reservations');
```

If there's a trigger that DELETEs the new upload along with the old one,
that would match the symptoms perfectly.

### 4. Function logs for upload `0f64ca93-b20e-48ce-8b03-c588f10ee862`

Same as the comment_extractions diagnostic: pull the Supabase Functions
log for that specific upload_id and check whether the reservations
upsert actually ran and what Postgres returned for affected rows.

## Required fix (probably parallel to the comment_extractions fix)

The reservations upsert needs:

```sql
INSERT INTO reservations (...)
VALUES (...)
ON CONFLICT (resv_name_id) DO UPDATE SET
  room = EXCLUDED.room,
  room_category_label = EXCLUDED.room_category_label,
  booked_room_category_label = EXCLUDED.booked_room_category_label,
  arrival = EXCLUDED.arrival,
  departure = EXCLUDED.departure,
  guest_name = EXCLUDED.guest_name,
  guest_first_name = EXCLUDED.guest_first_name,
  comments = EXCLUDED.comments,
  -- ALL the columns daily.py sends, especially:
  --   room (the field most likely to change)
  --   booked_room_category_label
  --   room_category_label
  --   comments
  --   arrival / departure (rebooking, extension)
  -- AND DO NOT overwrite id (the UUID PK).
  ...
;
```

The CONFLICT key must be a column (or column set) with a real UNIQUE
constraint. Most likely candidate: `resv_name_id` (Opera's natural key).
If `resv_name_id` doesn't have a unique constraint:

```sql
alter table reservations add constraint reservations_resv_name_id_unique
  unique (resv_name_id);
```

Phase 53 in our backlog explicitly notes this issue ("bridge UPSERT fix —
reservations on resv_name_id"), so this constraint may genuinely not
exist yet.

## Verification after deploy

```sql
-- Sanity: latest upload should be today
select max(uploaded_at)::date as last_upload from uploads;
-- expected: 2026-05-09

-- Sanity: reservations with NULL rooms should drop dramatically
select count(*) filter (where room is null) as null_rooms,
       count(*) as total
from reservations;
-- expected: null_rooms much lower than 2207 (most rooms are now assigned)

-- Bardonnet specifically
select id, resv_name_id, room, booked_room_category_label, arrival
from reservations where guest_name ilike '%Bardonnet%';
-- expected: 1 row, room='209'

-- Terrasson specifically
select id, resv_name_id, room, booked_room_category_label, arrival
from reservations where guest_name ilike '%errasson%';
-- expected: 1 row, room='220'
```

Once these all pass, the dashboard's pool heating grid will reflect
operations' truth: 7 heated rooms tonight (currently 5 + Bardonnet 209
+ Terrasson 220).

## Related context for you

- The `comment_extractions` upsert fix you just deployed (this morning,
  2026-05-09) — same architecture, identical symptom shape.
- Today's `comment_extractions` rows are now correctly extracted_at =
  today (276 rows), so that side is healthy.
- The Python pipeline (`src/daily.py` on Railway) is also healthy —
  builds the flash_reports.payload from the xlsx in memory each cron, so
  the dashboard renders live data even with stale `reservations`. But
  any feature that joins through reservations (overrides, audit
  reports, in-house lookups) operates on stale data.
- The pool_heating_overrides table you created references `reservations.id`
  via FK with ON DELETE CASCADE. If reservation_ids change with each
  upload, overrides may get orphaned. Verify the upsert preserves IDs
  rather than deleting+reinserting.
