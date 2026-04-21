# Daios Cove — Daily Flash Dashboard

Build a web app for Daios Cove (5-star resort, Crete) management. It's the live version of the one-page "Daily Flash" morning briefing currently produced manually. Backend (Supabase) is already wired up and populated.

## Who uses this

Four roles; enforced with Supabase Auth (magic link email → role looked up in `user_roles`):

| Role | What they see | What they edit |
|---|---|---|
| `front_office` | Flash dashboard **without** A-lister panel | Nothing |
| `guest_relations` | Everything | Can promote rooms to special attention |
| `management` | Everything | Nothing |
| `admin` | Everything | Daily briefing (MOD, weather, events, pool heating) |

A-lister data is automatically redacted server-side for `front_office`.

---

## Tech stack

- **React / TypeScript** (Vite or Next App Router — your call)
- **Tailwind CSS + shadcn/ui**
- **Supabase client** (`@supabase/supabase-js`) using the **anon key** (not service role)
- **react-to-print** (or server-rendered PDF) for the "Download PDF" button
- Single-page, information-dense, printable. The target aesthetic is the attached sample PDF: minimal, elegant, serif-ish for headings, subtle dividers, no heavy gradients or cards.

## Supabase connection

```
URL: https://iylnwafwrvzwkhhskazu.supabase.co
Anon key: <paste in the Lovable env setup — available in Supabase dashboard → Settings → API>
```

---

## Data access pattern

**Use Supabase RPCs, not raw table queries, for reads.** The schema is complex; RPCs return exactly what each screen needs.

### Primary RPC — full daily flash

```ts
const { data } = await supabase.rpc('get_daily_flash', { p_date: '2026-04-20' })
// Returns the full payload (jsonb). null if no flash for that date.
// alister_findings is an empty array if the user's role lacks A-lister access.
```

**Payload shape** (TypeScript):

```ts
type DailyFlash = {
  report_date: string                    // 'YYYY-MM-DD'
  computed_at: string                    // ISO timestamp

  occupancy: [OccupancyRow, OccupancyRow, OccupancyRow]
  // index 0 = TODAY, 1 = TOMORROW, 2 = FOLLOWING

  weather: WeatherDay[]                  // 3 days aligned with occupancy.
                                         // Auto-fetched from Open-Meteo each run.

  special_attention_arrivals: FlashGuest[]
  special_attention_departures: FlashGuest[]
  complimentary_partner_arrivals: FlashGuest[]
  pep_arrivals: FlashGuest[]
  birthdays_in_house: FlashGuest[]
  allergies_in_house: FlashGuest[]

  alister_findings: AListerPanelRow[]    // empty for front_office
  alister_findings_count: number

  pool_heating: PoolHeating[]
  daily_briefing: DailyBriefing | null
  promoted_rooms: string[]

  totals: {
    reservations: number
    extractions: number
    alister_researched: number
    alister_notable: number
  }
}

type OccupancyRow = {
  label: 'TODAY' | 'TOMORROW' | 'FOLLOWING'
  report_date: string
  occ_rooms: number
  guests_inh: number
  arrivals: number
  departures: number
  occupancy_pct: number                  // already multiplied (e.g. 17.05)
}

type FlashGuest = {
  room: string | null
  name: string                           // 'Mr Manuel Magistro'
  reason: string | null                  // 'REPEATERS - LOYAL GUESTS / Airtours'
  vip: string | null                     // 'GLP' | 'V1' | 'V2' | null
  travel_agent: string | null
  group_name: string | null
  accompanying: string | null            // 'Magistro, Jasmin / Magistro, Romeo / ...'
  nationality: string | null
  allergy_flag: boolean                  // quick keyword prefilter
  honeymoon: boolean
  resv_name_id: number | null
  extraction: CommentExtraction | null   // detailed LLM extraction
  alister: AListerRow[]                  // per-person findings for this booking
}

type CommentExtraction = {
  allergies_present: boolean
  allergies_text: string | null          // verbatim allergy sentence
  pool_fence: boolean
  pool_heating: boolean
  free_transfer: boolean
  free_upgrade: boolean
  lco: boolean                           // late checkout
  honeymoon: boolean
  amenities: string[]                    // ['Raki 200ml', 'in-room breakfast', ...]
  ops_notes: string | null               // payment/tax note or summary
}

type AListerRow = {
  matched_name: string
  relationship: 'self' | 'partner' | 'accompanying'
  confidence: number                     // 0..100
  category: string | null                // 'CEO/Founder' | 'Athlete' | ...
  summary: string | null
  evidence_urls: string[]
}

type AListerPanelRow = AListerRow & {
  room: string | null
  guest_on_booking: string | null
}

type PoolHeating = {
  room: string
  service: 'HP' | 'PF' | 'HP/PF'
  end_date: string | null                // 'YYYY-MM-DD'
  source: 'manual' | 'comments'
}

type DailyBriefing = {
  report_date: string
  mod_name: string | null
  mod_phone: string | null
  hotel_events: string | null
  site_inspections: string | null
  group_events: string | null
  show_rooms: string | null
  notes: string | null
}
// NB: Weather is NOT in DailyBriefing anymore — it's top-level `flash.weather`
// and auto-fetched by the pipeline; admins don't edit it.

type WeatherDay = {
  label: 'TODAY' | 'TOMORROW' | 'FOLLOWING'
  date: string                           // 'YYYY-MM-DD'
  day_name: string                       // 'Mon' / 'Tue' / 'Wed'
  high: number | null                    // °C, rounded to int
  low: number | null                     // °C, rounded to int
  condition: string                      // 'Clear' / 'Partly cloudy' / 'Rain' / …
  icon: 'sun' | 'cloud-sun' | 'cloud' | 'rain' | 'drizzle' | 'snow' | 'fog' | 'storm'
  code: number                           // WMO weather code (for fine-grained mapping)
}
```

### Other RPCs

```ts
// Historical — which dates have a flash?
supabase.rpc('list_flash_dates')
  // → { report_date, computed_at, row_count }[]

// GR promotes a room to special attention for this date
supabase.rpc('promote_room', { p_date, p_room, p_reason })

// Admin upserts the daily briefing
supabase.rpc('upsert_daily_briefing', {
  p_date, p_mod_name, p_mod_phone, p_weather,
  p_hotel_events, p_site_inspections, p_group_events, p_show_rooms, p_notes
})
```

---

## Screens

### 1. Login (`/login`)

- Logo + "Daios Cove — Daily Flash"
- Email input + "Send magic link" button
- `supabase.auth.signInWithOtp({ email })`
- After clicking magic link the user lands on `/`.

### 2. Dashboard (`/`) — the flash

**Layout — single printable page matching the sample PDF:**

- **Header bar** — "DAIOS COVE" wordmark, "Daily Flash", report date (use `?date=YYYY-MM-DD` param, default today), a small date picker that refetches, a "Download PDF" button, the current user email + role chip.

- **Occupancy strip** (top). Three columns side-by-side: `TODAY`, `TOMORROW`, `FOLLOWING`. Each shows Occ.Rooms, Guests INH, Arrivals, Departures, Occupancy %. Large numbers, small labels.

- **Special Attention Arrivals** (left column, takes ~half the page). One row per guest:
  - Room number (bold, small monospace)
  - Guest name
  - Reason tags on the right (pill-shaped): e.g. `REPEATERS`, `Complimentary`, `Airtours`, `PEP`, `Honeymoon`. Color-code: VIP = gold, PEP = purple, Allergy = red.
  - Inline icon badges when extraction flags apply:
    - ⚠️ Allergy (red) — tooltip shows `extraction.allergies_text` verbatim
    - 🧒 Pool Fence (if `extraction.pool_fence`)
    - ⬆️ Upgrade (if `extraction.free_upgrade`)
    - 🕑 LCO (late checkout)
    - 💍 Honeymoon
    - ⭐ A-lister flag (only if `alister.length > 0` AND role can see it)
  - Expandable details (click row): accompanying names, amenities list from `extraction.amenities`, `extraction.ops_notes`, `extraction.allergies_text`, and any `alister` findings with evidence URLs.

- **Special Attention Departures** (same format, below).

- **Complimentary Partner Arrivals** and **PEP Arrivals** — smaller sections.

- **Birthdays / Anniversaries / Honeymoon** — small section. Pull from `birthdays_in_house` (matched from BIRTH_DATE field) plus any guest in `special_attention_arrivals` with `honeymoon=true`.

- **Allergies in House** — dedicated red-accented section. One line per guest with the verbatim allergy text. This is the single most important field in the app.

- **A-lister Panel** (right column, only if role can see it and `alister_findings_count > 0`). Each entry:
  - Room + matched name + relationship (`self` / `partner` / `accompanying`)
  - Category chip
  - One-line summary
  - Small list of evidence URLs (3 max, favicons)

- **Side panel — Hotel ops** (right column):
  - **Weather** from `flash.weather` (top-level, not in daily_briefing).
    Render 3 rows aligned with TODAY / TOMORROW / FOLLOWING; show `day_name`,
    `high`/`low` °C in serif numerals, a single-line `condition`, and the
    `icon` as a hand-drawn SVG (map `sun`/`cloud-sun`/`cloud`/`rain`/`drizzle`/
    `snow`/`fog`/`storm` → your icon set). Subtle separators between rows.
  - **MOD** — name + phone
  - **Hotel Events / Site Inspections / Group Events** — plain text blocks
  - **Show Rooms** — plain text
  - **Pool Heating & Fence** — table: Room, Service, End Date

- **Promote-to-Special-Attention** button (guest_relations + admin only): opens a modal listing all arrivals NOT already in special attention; user picks a room and types a reason; calls `promote_room(date, room, reason)`; page refetches.

### 3. Historical flash (`/flash/:date`)

Same layout as `/`, but the date comes from the URL. Add a "Back to today" button.

### 4. Admin — Daily Briefing (`/admin/briefing?date=YYYY-MM-DD`)

Admin role only (redirect or 403 otherwise). Form fields (weather is NOT here —
auto-fetched from Open-Meteo each morning):
- MOD name, MOD phone
- Hotel Events (textarea)
- Site Inspections (textarea)
- Group Events (textarea)
- Show Rooms (textarea)
- Notes (textarea)

On submit, call `upsert_daily_briefing(p_date, p_mod_name, p_mod_phone, null,
p_hotel_events, p_site_inspections, p_group_events, p_show_rooms, p_notes)`.
The `null` is the weather slot — kept for backward compat; ignored.

### 5. (Optional) Date browser (`/flash`)

Lists `list_flash_dates()` as a simple table — Date, Reservations, Computed at. Clicking a row opens `/flash/:date`.

---

## Auth & role handling

```ts
// After login, look up role
const { data: roleRow } = await supabase
  .from('user_roles')
  .select('role')
  .eq('user_id', user.id)
  .single()

// Stash in React context; use to gate A-lister panel + admin nav
```

For the first user (you): the DB admin will run
```sql
select set_user_role('d.daios@daioshotels.com', 'admin');
```
after that user has signed in at least once.

---

## Visual direction

- **Reference**: the sample one-page Daily Flash PDF — sparse, grid-aligned, reads like a gentleman's newspaper.
- **Typography**: sans-serif body (Inter or similar), serif display for the "DAIOS COVE" header (something like Cormorant or EB Garamond).
- **Colours**: near-white background, deep teal/green accent (Daios Cove brand is olive/teal), red for allergies, gold for VIP, muted purple for PEP.
- **No card shadows, no gradients, no hero images.** This is an ops tool — density and clarity win.
- **Print styles**: `@media print` hides the header bar and side controls; content prints as the one-page flash, fitting on A4 landscape.

## Must-have features (check before shipping)

- [ ] Login via Supabase magic link
- [ ] Role lookup from `user_roles` gates A-lister + admin nav
- [ ] Dashboard renders `get_daily_flash(today)` and shows all sections
- [ ] Date picker changes the `?date=` query param and refetches
- [ ] Download PDF button (react-to-print works; server-side render is better if easy)
- [ ] Allergy icons + verbatim text prominent and red
- [ ] Guest Relations "promote to special attention" modal
- [ ] Admin daily-briefing form persists via RPC
- [ ] Empty states when `get_daily_flash(date) === null` ("No flash for this date yet — the pipeline runs at 05:00 daily")
- [ ] Responsive: works on iPad for GR walkthroughs, prints cleanly on A4
- [ ] Print CSS preserves the single-page flash layout

## Out of scope for Lovable

- The daily xlsx ingestion pipeline — runs server-side on a schedule, already writes `flash_reports` for Lovable to read.
- A-lister research — already runs server-side.
- No xlsx upload UI needed.

## Sample `get_daily_flash('2026-04-20')` return (real data)

Ask Supabase — it's already populated. Use it as the golden fixture while building.
