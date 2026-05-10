# Notion fill-in — 10 May 2026 (Phases 60.8, 61.1, 61.2)

**Target page:** Daily Flash — Shipping Log
https://www.notion.so/34cfd0735bf5814bb425daac35b0bf81

**Notion API status:** writes have been timing out repeatedly across this
weekend. Reads work fine. Saving the content here for retry next time the
API recovers — same pattern as `docs/notion-fill-in-phases-52-58.md`.

**Three updates to apply once writes are working:**

## 1. TOC entry update

Find the existing 9 May TOC entry. Replace with:

```
- [10 May 2026](#10-may-2026) — Phases 60.8, 61.1, 61.2: PDF + send-flash-email field swap to Phase 60 fields (heating/fence/cleaning); morning briefing 50-recipient Resend chunking bug fixed; Pool Operations explore-page panels unified to same 14-day Gantt + 137-button grid layout for cleaning and fence (mirrors heating).
- [9 May 2026](#9-may-2026) — Phases 59, 60.0–60.7, 61: Zoho priority strip in DB trigger, full pool-heating dashboard redesign (47-button grid + 14-day Gantt + fence + override merge), comment_extractions upsert silent-drop bug discovered and fixed by Lovable, in-house re-extraction (Phase 48 brute-force), strengthened LLM prompt for pool flags, pool-cleaning forecast for maintenance coordinator (137-room inventory), CC chair on idea responses, cron freshness sanity check.
```

## 2. Section content insert

Find anchor: `\n---\n## 9 May 2026\n\nMajor day.`

Insert the following section between `---` and `## 9 May 2026`:

---

## 10 May 2026

Cleanup and consistency day. The Phase 60 fields finally reach the PDF and
email surfaces (the legacy `pool_heating[]` is no longer rendered anywhere).
Phase 61.1 + 61.2 add grid+calendar layouts for pool cleaning and fence so
the explore-page Pool Operations tab has three identically-shaped panels
(Heating, Fence, Cleaning). And the morning briefing's Resend 50-recipient
chunking bug surfaces and gets fixed.

### Phase 60.8 (commits `aa827ee`, `7b643a2`, `89e9e4e`) — PDF + email field swap

The printable PDF was rendering ~34 rooms under "POOL HEATING & FENCE" from
the legacy `payload.pool_heating[]` field. Phase 60.0's new fields
(`pool_heating_grid`, `pool_fence_other_rooms`, `pool_cleaning`) had been
live in the payload for a day but `generate-flash-pdf` and
`send-flash-email` hadn't been updated to consume them.

Lovable patched both surfaces in one round-trip: heating reads from
`pool_heating_grid` filtered by `is_heated_today`; fence from
`pool_heating_grid.is_fence_today` + `pool_fence_other_rooms`; cleaning
from `pool_cleaning.today`. Tonight's PDF will show 8 heated, 5 fenced,
99 to clean (vs the 34-room mess yesterday).

Side correction: stale "06:00 Athens" reference in
`src/email_dispatcher.py` docstring corrected to "08:00 Athens" — the
actual scheduled time. The 06:00 was an old comment that had been carried
forward without anyone noticing.

### Morning briefing 50-recipient Resend chunking bug

User noticed they didn't receive today's 8:00 morning briefing. Diagnostic
revealed: `send-executive-briefing-email` ran at 08:00 Athens (Lovable's
Edge functions dashboard showed "Invoked: 1, Succeeded: 1") but inside
the run, only 8 of 58 recipients got the email. The other 50 (including
d.daios) failed with:

```
{"statusCode":422,"name":"validation_error","message":"The total number of recipients cannot exceed 50."}
```

Resend's per-call recipient limit. Phase 54's handoff explicitly called
this out ("chunk to 50 per Resend call (matching the approve-fam-trip /
approve-site-inspection pattern from Phase 46)") but Lovable's edge
function update skipped the chunking step.

Lovable patched: split into ≤49 BCC chunks per Resend call. Tomorrow's
8:00 send will go to all ~58 recipients across 2 chunks. Lovable also
ran an immediate backfill (`only_recipients` = the failed-list for
2026-05-10) so the missed 50 still received today's briefing within
~10 minutes of the discovery.

**Architectural takeaway**: same pattern as the `comment_extractions`
upsert bug from yesterday. The function reported success because the
top-level Resend call returned 200 (with 8 sent + 50 failed in the
response body), but the function didn't treat per-recipient failures
as a function-level failure. Same false-success-counter class of bug.
The dashboard "Invoked: 1, Succeeded: 1" was technically true but
operationally misleading. Worth a freshness/sanity-check on this
function similar to Phase 60.3's pattern; queued for after tomorrow's
08:00 confirms the chunking fix.

### Phase 61.1 (commit `6440939`) — Pool cleaning grid + calendar

The Phase 61 cleaning summary fields (count + 7-day forecast) were live in
the payload, but the explore-page Pool Cleaning panel was doing its own
live query against `explore_arrival_detail` and showing 0/137 due to a
filter bug — the upgraded/assigned room flag dropped most occupied pool
rooms.

Rather than debug the live query, mirror the Phase 60 heating layout:
14-day Gantt + 137-button grid grouped by type code.

Adds two new payload fields with the SAME SHAPE as `pool_heating_grid` /
`pool_heating_calendar`:

- `pool_cleaning_grid` — 137 entries (12 type codes: DLXP, JSTEP, STEP,
  V1, V2, VW, V3, MANSION, CJSTEP, CPJSTP, CPRESP, CSTEP). Each entry
  carries `room`, `type_code`, `description`, `heatable` (bool),
  `is_to_clean_today` (bool — true when arrival ≤ today < departure for
  any reservation in that room).
- `pool_cleaning_calendar` — 14-day window (yesterday + today + 12 days,
  matches heating window) with all in-pool-room stays. No heated/fence
  flags; every stay represents a pool that needs cleaning while the
  guest is in-house.

Verified for 5/10 dry-run: `grid = 137` buttons (99 occupied today),
`calendar = 326` stays. Type breakdown — DLXP heaviest at 44/49 occupied,
JSTEP and STEP 100% occupied, MANSION empty.

`src/daily.py`, `docs/lovable-prompt-pool-cleaning-grid-calendar.md`.

### Phase 61.2 (commit `ce7db20`) — Pool fence grid + calendar

User: "we need teh exact same thing for pool fences." Adds the third
identically-shaped panel.

Two new payload fields:

- `pool_fence_grid` — 137 entries with `is_fence_today` (bool). Merges
  Phase 60.1's two fence sources (`pool_heating_grid[].is_fence_today`
  for 47 heatable rooms + `pool_fence_other_rooms[].in_house_today` for
  non-heatable rooms like DLXP/Collection) into a single 137-button grid.
- `pool_fence_calendar` — 14-day window with fence stays only (deduped
  by `(room, arrival, departure)` when both sources have the same stay).
  Will be sparse — fence requests are rare, ~5–10/day max historically.

The three Pool Operations panels (Heating, Fence, Cleaning) now share the
same data shape and time axis — Lovable can use a single parameterized
component with field-name + boolean-name swaps.

`src/daily.py`, `docs/lovable-prompt-pool-fence-grid-calendar.md`.

### State at end of day

- Tonight's 19:30 Athens cron is the first end-to-end run with all three
  Pool Operations data structures populated. Fence values will populate
  properly once Phase 60.5 in-house re-extraction runs (Clapham's "FOC
  Pool Fence" should flip `is_fence_today=true` tomorrow morning).
- Lovable to-do: ship the Pool Cleaning + Pool Fence panels using the new
  fields. Three handoff docs delivered today.
- Re-preview emails to Thelxi: 6+ today across the manual production
  pushes for verification. She's been a champion.
- Pending: PostgREST restart from Lovable infra (long-pending — affects
  zoho_notes ingest + override merge into Phase 60.7). Daioscove.com
  Resend domain verification (Phase 57). Notion API write outage today
  blocked this same shipping-log update for hours.

---

(Then the existing `## 9 May 2026` section continues as-is.)

---

## 3. Phases 52–58 fill-in (still pending from yesterday)

These also still need to land in the 8 May 2026 (continued) section. Full
content is in `docs/notion-fill-in-phases-52-58.md` (commit `9d8202b`).
Apply that doc's two updates AS WELL as the two above next time the
Notion write API is responsive.
