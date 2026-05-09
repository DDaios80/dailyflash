# Phase 59 — DB-side cleanup of Zoho housekeeping priority explanations

## What this does

Strips ` (Accepted in 10min & delivered in 30min)` from `zoho_notes.body`
on every INSERT, UPDATE, and (one-time) on existing rows. The outer
`(NORMAL)` priority label stays — only the time-explanation parenthetical
is removed.

Before:
```
1 × Iron / σίδερο (NORMAL (Accepted in 10min & delivered in 30min))
```

After:
```
1 × Iron / σίδερο (NORMAL)
```

## Where the cleanup now lives

DB trigger `zoho_notes_strip_priority` defined in
`db/phase59_zoho_strip_priority_explanation.sql`.

## Important — please do NOT add cleanup logic anywhere else

The user reported this cleanup was done before but didn't persist. That's
because it lived in `ingest-zoho-notes` edge function code, which got
silently overwritten on subsequent refactors.

**Do not add a duplicate strip in any of these places:**

- `ingest-zoho-notes` edge function (let raw body land in DB; trigger cleans it)
- Any compute / merge_zoho_into_flash code path on the Python side
- Any dashboard rendering layer

If the cleanup is added in two places, future debugging gets painful
(double-cleanup, partial cleanup, or worse, conflicts when the patterns
diverge). The DB trigger is the single source of truth.

## If the trigger ever needs to change

Edit `db/phase59_zoho_strip_priority_explanation.sql`, change the regex
pattern in `strip_zoho_priority_explanation`, run the migration. The
trigger uses CREATE OR REPLACE on the function so updates are clean.

## Why DB layer instead of edge function

Lovable's AI tends to refactor edge function code during feature work.
The user has confirmed prior cleanups in code didn't persist. DB
triggers and functions are not typically touched by Lovable's AI on
its own initiative, so this fix survives.

Same persistence pattern as Phase 51's `authenticator` role grants and
Phase 56's atomic acknowledged_at stamping in the RPC — push the
constraint down to the layer that's least likely to be silently
overwritten.

## content_hash safety

The trigger does NOT modify `content_hash`. zoho's ingest dedup compares
incoming raw-body hashes against stored content_hash. If content_hash
were updated to match the cleaned body, future re-ingests of the same
note would no longer match (because they'd compute hash from the
original raw body). Leaving content_hash untouched preserves dedup.

## Backfill is included

The same migration runs an UPDATE that cleans the 341 existing rows
identified by the diagnostic. The trigger also fires on this UPDATE but
is idempotent — `strip(already_stripped) = same`.
