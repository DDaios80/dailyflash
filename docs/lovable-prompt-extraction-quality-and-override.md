# Lovable prompt — extraction prompt strengthening + manual override UI

**Send this AFTER the `ingest-flash-report` upsert fix is deployed and
verified.** Without the upsert fix, prompt improvements have no effect
because the writes don't land.

This prompt has two parts. They can be implemented in one Lovable session
or split.

## Part A — Strengthen the LLM extraction prompt

The current prompt for `pool_heating` and `pool_fence` is too narrow:

```
pool_fence — True if the comment mentions a pool fence
             (e.g. 'POOL FENCE', 'pool fence free').
pool_heating — True if pool heating is mentioned
               (e.g. 'HP', 'heated pool', 'pool heating').
```

Add the following clarifications. The extraction code is in
`src/extract.py` (Python) — these go in the `Field(description=...)`
strings of the `CommentExtraction` Pydantic model.

### `pool_heating` — replace description with:

> True if pool heating is requested or arranged for this stay. Set to
> True when the comment expresses ANY of: "HP", "heated pool", "pool
> heating", "please heat the pool", "make sure the pool is heated", or
> the Greek equivalent ("ζεστή πισίνα", "θέρμανση πισίνας", "να ζεσταθεί
> η πισίνα"). Set to **False** if the comment indicates the request was
> declined: "pool heating - denied", "heating not approved", "denied",
> "declined", or any explicit rejection. If the comment is silent on
> pool heating, set False. Don't infer from "villa booking" or "private
> pool" — those don't imply heating.

### `pool_fence` — replace description with:

> True if a pool safety fence is requested or arranged. Set to True when
> the comment expresses any of: "pool fence", "pool fence free", "fence
> around the pool", "kid-proof the pool", "pool safety", "pool gate", or
> the Greek equivalent ("κάγκελο πισίνας", "φράχτης πισίνας"). Children
> mentioned WITHOUT a fence request (e.g. "1 kid = 4 y.o.", "child age
> 3") do NOT trigger fence — only an explicit request or arrangement
> does. Set False if the request was denied or declined.

### Verification after deploy

Run the cron, then check these specific cases:

| Reservation | Comment fragment | Expected `pool_heating` | Expected `pool_fence` |
|-------------|------------------|------------------------:|----------------------:|
| Mueller (210, 5/9) | "please make sure that the pool is heated" | **true** | false |
| Yuval Ron (206, 5/8) | "requested a pool heating - denied" | **false** | false |
| Clapham (204, 5/8) | "1 kid = ... Free Transfers" | false | false |
| Foot (404, 5/8) | "Group of 5 friends — annual trip" | false | false |

Eyeball ~10 random extractions from the previous night's batch and look
for obvious misses. If any look wrong, share the comment text and the
extracted booleans here and we can iterate.

## Part B — Manual-override UI on the pool heating grid

Even after Part A, the LLM will sometimes miss things or the comment
itself will be silent. Housekeeping needs a manual override.

### Data model

Add a small table:

```sql
create table if not exists pool_heating_overrides (
  id uuid primary key default gen_random_uuid(),
  reservation_id uuid references reservations(id) on delete cascade,
  override_type text not null check (override_type in ('heating', 'fence')),
  -- value: NULL means "auto" (defer to LLM); true/false means force
  forced_value boolean not null,
  set_by uuid references auth.users(id),
  set_at timestamptz not null default now(),
  unique (reservation_id, override_type)
);
```

When housekeeping clicks a button on the grid, the click upserts a row
in `pool_heating_overrides` for that reservation_id (if the room has an
in-house guest) or stores the override at the room level (if empty —
applies to next arrival).

Keep it simple for v1: store override per **reservation_id**, not per
room. When a new guest arrives, the override resets (because it's a new
reservation_id).

### Read path

The Phase 60 grid component reads `pool_heating_grid[]` from
`flash_reports.payload`. Each entry currently has `is_heated_today` and
`is_fence_today` derived from comment_extractions. The new logic:

```
is_heated_today = override_heating ?? auto_heating
is_fence_today  = override_fence   ?? auto_fence
```

Add `override_heating: bool | null` and `override_fence: bool | null`
fields to each grid entry, populated from `pool_heating_overrides`. The
backend (Python `src/daily.py`) needs to:

1. Query `pool_heating_overrides` joined to current/upcoming reservations.
2. Merge into the grid: if an override row exists for an in-house
   reservation in a room, use the override.
3. Same for the calendar `stays` — each stay carries its
   `override_heating` / `override_fence` if present.

### UI behavior

On each grid button:

- Default: render based on `is_heated_today` / `is_fence_today` (which
  now reflect override-if-present-else-auto).
- Click the button → small popup with three radio options:
  - **Auto** — clear the override, fall back to LLM extraction
  - **Force ON** — set `forced_value = true`
  - **Force OFF** — set `forced_value = false`
- After click, optimistic update + write to `pool_heating_overrides` via
  a new edge function (e.g. `set-pool-heating-override`).
- Visual marker on overridden buttons: a small dot or border so the team
  can see at a glance which buttons are auto vs forced.

Apply the same pattern to fence (separate sub-popup or two-state toggle
within the same popup).

### Permissions

- Read: anyone who can read flash_reports.
- Write: limit to housekeeping role / specific user list (Thelxi, d.daios,
  whoever is on the housekeeping rota). Use existing role gating.

### Audit

`set_by` and `set_at` track who did what. No history table needed for
v1; just the latest override.

## Notes

- This pairs with the freshness alert being added to the Python cron —
  if the LLM extraction goes stale again (Lovable AI rewrites edge
  function and breaks it), housekeeping can keep operating via manual
  overrides while the auto path is fixed.
- The override applies to the heating GRID and the calendar BARS. The
  legacy `pool_heating` field will be retired after this lands; make
  sure the new component is the primary surface before removing it.
