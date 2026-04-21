# Daios Cove — Daily Flash

## Design Proposal

**Aspiration.** When a General Manager opens this on Monday at 5:45 AM, it should feel less like opening software and more like being handed a crisp white card on a silver tray. It is an operational document with the confidence and restraint of a Michelin tasting menu, the density of a Financial Times front page, and the warmth of a gracious host. Nothing about it should look like Bootstrap, a CRM, or a "dashboard."

---

## 1. Design Principles

**1. Editorial, not dashboard.** We compose pages, not screens. Typography does the heavy lifting; data sits inside a deliberate grid. A VIP guest is an editorial moment, not a "card."

**2. Quiet until it isn't.** Most pixels are still. When something matters — an allergy, an A-lister, a pool-fence for a 10-month-old — the page raises its voice by one notch, never two. Red is reserved. Gold is reserved. The rest is paper, ink, and one deep green.

**3. Instant.** No loading spinners. Skeleton screens only, and only when necessary. Data is pre-computed; the app is a renderer. The 05:30 pipeline is doing the thinking.

**4. Density with air.** A Daily Flash needs to surface 40+ guests plus weather, events, MOD, show rooms, pool heating, and A-listers on a single page. We achieve density through typographic hierarchy and restraint — not compression.

**5. Printable as gospel.** The PDF export is the same layout, flattened. A 5-star hotel still prints morning briefings and hands them around.

---

## 2. Visual Identity

### Palette

Inspired by the Daios Cove brand (olive groves, white stucco, Aegean pine, sea-washed stone).

| Token | Value | Role |
|---|---|---|
| `paper` | `#F8F5EE` | Background — warm off-white, not blue-white |
| `ink` | `#1A1A17` | Primary text |
| `ink.muted` | `#57564F` | Secondary text, labels |
| `ink.whisper` | `#A09E95` | Tertiary text, metadata |
| `green` | `#2E3D2F` | Primary accent (Daios Cove deep olive) |
| `green.soft` | `#E7E5DD` | Subtle fills (accompanying name chips) |
| `gold` | `#A88A4A` | VIP, honeymoon, ceremonial moments |
| `terracotta` | `#C85C3C` | Allergies ONLY — this red is never used decoratively |
| `lavender` | `#6B5B8F` | PEP |
| `rule` | `rgba(26,26,23,0.08)` | Dividers, sub-rules |
| `ink.inverse` | `#0F100D` | Night-mode paper |

**Dark mode** is a proper second theme, not an inverted hack. Paper becomes near-black, ink becomes warm ivory, the same accents carry through at slightly desaturated intensities. 5 AM briefings should not blind a guest-relations manager on a pre-coffee iPad.

### Typography

Two families carry the whole system.

- **Display / headings** → `GT Sectra Display` (editorial serif, magazine-grade). Used for `DAIOS COVE`, the date, the "TODAY / TOMORROW / FOLLOWING" labels, and section titles.
- **Body / data** → `Söhne` (humanist sans, the Soho-House-app feel). For all running text, guest names, reasons, ops copy.
- **Mono** → `GT America Mono` for room numbers only. Room numbers are atomic identifiers and deserve their own voice.

If budget rules out those foundries, the Google equivalents are **Cormorant Garamond** (display) + **Inter** (body) + **JetBrains Mono** (mono). Lovable supports both out of the box.

Scale (rem):

```
display-hero   3.75   (only the "DAIOS COVE" word)
display-large  2.25   (date in header)
display        1.625  (occupancy numbers — TODAY / TOMORROW / FOLLOWING)
title          1.125  (section headings: "Special Attention Arrivals")
body           0.9375 (15px — information dense)
caption        0.8125 (13px — labels, metadata)
micro          0.6875 (11px — room number chip, date tags)
```

Line-height is generous — 1.45 for body, 1.15 for display. Letter-spacing tight on display (-0.01em), positive on small-caps labels (+0.08em).

### Iconography

Custom SVG glyphs — hand-drawn feel, 1.5px stroke, matching the typography's weight. A 10-icon set is enough: allergy, pool-fence, upgrade, late-checkout, honeymoon, VIP, complimentary, PEP, repeater, A-lister. **No Lucide. No Heroicons.** Those libraries signal "dev tool."

Weather glyphs: a matching serif-weight hand-drawn set (sun, partly cloudy, rain, thunder, wind). Never emoji.

### Motion

Nothing bouncy. Nothing spring. Only the calm of `cubic-bezier(0.25, 0.1, 0.25, 1)` at 220ms. Guest rows expand and collapse at 320ms with an ease-out. The page transitions at 180ms as a soft opacity cross-fade.

One signature flourish: the occupancy numbers **count up** from 0 to the real figure on first paint, over 800ms, with an ease-out and a subtle pulse on landing. This is the one place where motion earns its keep — it tells the GM at a glance that "these numbers are fresh."

---

## 3. Layout

### 3.1 Dashboard — single printable page

Target canvas: **1440 × 900** desktop, **1180 × 820** iPad landscape (first-class target — Guest Relations walk the lobby with iPads). Phone is a graceful fallback, not a primary.

```
┌──────────────────────────────────────────────────────────────────────────────┐
│  DAIOS COVE                      Monday, 20 April 2026       d.daios • admin │   (serif logotype — 64pt)
│  ───  Daily Flash  ◀ 20/04 ▶                                  ⎙ PDF          │   (thin rule, tiny meta row)
├──────────────────────────────────────────────────────────────────────────────┤
│                                                                              │
│       TODAY                    TOMORROW                  FOLLOWING           │
│       44 rooms                 47 rooms                  63 rooms            │   (display numerals, count-up)
│       109 guests               118 guests                156 guests          │
│       44 arrivals • 0 depart   7 arrivals • 3 depart     17 arrivals • 1     │
│       17.05% occ.              18.01%                    23.95%              │
│                                                                              │
├──────────────────────────────────────────────────────────────────────────────┤
│                                                                              │
│   Special Attention — Arrivals        │  Weather (Mon–Tue)                   │
│   ─────────────────────────────        │  ─────────────────                   │
│    108  Mr David Conitzer         ☐    │    Mon   13 / 22 °C  Sunny   ☀︎      │
│         Complimentary • ITC +2         │    Tue   14 / 23 °C  Sunny   ☀︎      │
│    112  Mrs Cassandra Frederiksen ⬆    │                                     │
│         PEP • Nyhavn +1           ☐    │  MOD — on duty                      │
│    …                                   │  ─────                              │
│    210  Mr Manuel Magistro    ⚠🧒🕑☐  │   Kyrillos Michailides               │
│         Repeaters • Airtours +4        │   +30 6951 652845                   │
│    …                                   │                                     │
│                                        │  Hotel events                       │
│   Expanded row (on click):             │  ─────────────                      │
│   ┌────────────────────────────────┐   │   10:00 Inspection — Ms. Trampler   │
│   │ ⚠  Nuts allergy (previous…)   │   │                                     │
│   │ 🎁 Raki 200ml • 1L water …     │   │  Site inspections                   │
│   │ 💳 Government tax on c/out    │   │  ─────────────────                  │
│   │ Accompanying (4)              │   │   DER TOUR — Christianna +9         │
│   └────────────────────────────────┘   │                                     │
│                                        │  Pool heating & fence               │
│                                        │  ─────────────────                  │
│   Special Attention — Departures       │   112 HP → 24/04                    │
│   ─────────────────────────────        │   118 HP → 26/04                    │
│   (empty — "All departures routine.")  │   …                                 │
│                                        │                                     │
├────────────────────────────────────────┴─────────────────────────────────────┤
│                                                                              │
│   A-lister intelligence (gold rule, only if user role has access)            │
│   ──────────────────────                                                     │
│   ★  Room 362  Gwendal Le Ruyet                                    conf 90    │
│      Senior Consultant Chef, Ducasse Conseil — long-standing partner of …    │
│      ducasse-conseil.com/team  ·  linkedin.com/in/gwendal-leruyet             │
│                                                                              │
│   ★  Room 331  Geoffroy Verzat      (partner on booking)          conf 85    │
│      Co-Founder of teale.io — Paris-based B2B mental health platform, €10M   │
│      raise in 2023.                                                          │
│      crunchbase/geoffroy-verzat · theorg.com · teale.io                       │
│                                                                              │
│   ★  Room 339  Emanuel Janisch                                    conf 78    │
│      Managing Director, Realeyes Gruppe — Munich ophthalmology group         │
│      unternehmeredition.de · presseportal.de                                 │
│                                                                              │
├──────────────────────────────────────────────────────────────────────────────┤
│                                                                              │
│   Complimentary partner arrivals   │  PEP  │  Birthdays · Anniversaries      │
│   (compact rows)                   │  ...  │  (compact rows)                 │
│                                                                              │
├──────────────────────────────────────────────────────────────────────────────┤
│                                                                              │
│   Allergies & dietary               (red-rule, 3px terracotta)               │
│   ───────────────                                                            │
│    202  Nordbaeck    “Our daughter has a peanut allergy. Remove all…”        │
│    210  Magistro     “Dr. Magistro is allergic to nuts.”                     │
│    503  Achmueller   “Mr. Achmueller is vegetarian.”                         │
│                                                                              │
└──────────────────────────────────────────────────────────────────────────────┘
```

Key compositional choices:
- **Horizontal rules replace cards.** Thin 1px `rule` lines define sections; no boxes, no shadows, no rounded corners.
- **Radius = 0** across the system. Buttons are typographic; chips are minimal outlines with `4px` padding; the one exception is the room-number mono chip (`2px` radius, subtle fill).
- **Two-column main body** (arrivals + side panel). A-lister gets a full-width strip with a **gold top rule** — the only coloured rule in the composition — because those three names are the one thing in the whole app someone might miss and regret.
- **Allergies section gets a terracotta rule** at the bottom — it's the last thing you see before closing the page. Deliberate.

### 3.2 Guest row anatomy

Collapsed state:
```
 [ROOM]  Guest Name                     Reason tags        Badges   ▾
  ↑                                       ↑                  ↑
 mono chip              sans 15pt, ink   outlined pills   icons
```

- Room number: mono, 11pt, uppercase, 2px radius, tight green-soft fill.
- Name: sans 15pt.
- Reason tags: outlined pills, 0.5px stroke, tiny — space-separated, never comma-listed.
- Badges: right-aligned, icon-only at 14pt, colours from the palette (allergy = terracotta, pool = ink, upgrade = green, LCO = ink, honeymoon = gold, VIP-note = gold, A-lister = gold ★).
- Hover: entire row picks up a subtle `green.soft` background (`rgba(46, 61, 47, 0.04)`).
- Click or keyboard `Enter` expands the detail.

Expanded state:
```
 [ROOM]  Guest Name                      Reason tags    Badges   ▴
 ─────────────────────────────────────────────────────────────────
 ⚠  Allergy:  “Dr. Magistro is allergic to nuts…”          (quoted, terracotta quote mark)
 🎁  Amenities prepared:
     Raki 200ml · 1L Water · Mini Rusks · Thermal Spa Suite · in-room breakfast
 💳  Payment:  Government tax on check-out
 ♚  Accompanying:
     Magistro, Jasmin · Magistro, Romeo · Magistro, Phelia · Magistro, Antonia
 Nationality  Germany · TA: TUI Deutschland · Group: Airtours 2026 DE
```

Tasteful. No borders around each line — just vertical rhythm and a subtle left rule.

### 3.3 A-lister card

```
 ┌─ gold top rule ──────────────────────────────────────────────┐
 │  ★  Room 331   Geoffroy Verzat           (partner)   conf 85 │
 │     CEO/Founder                                              │
 │                                                              │
 │     Co-Founder of teale (ENSOPCO), a Paris-based B2B         │
 │     mental-health platform. €10M Series A in 2023.           │
 │                                                              │
 │     Sources:                                                 │
 │     crunchbase.com/person/geoffroy-verzat                    │
 │     theorg.com/org/teale-1/org-chart/geoffroy-verzat         │
 │     teale.io/en/terms-conditions-practitioners               │
 └──────────────────────────────────────────────────────────────┘
```

- `★` in gold. Room chip in mono. Confidence as a small gold-filled bar at the right edge (a tiny 40×6px bar that fills 85%, labelled `conf 85`).
- Evidence URLs are links, not buttons. Small favicon + hostname. Click opens in a new tab.
- Relationship chip (`self` / `partner` / `accompanying`) sits inline next to the name in `ink.whisper`.

### 3.4 Empty states (these matter)

Not "no data." Prose.

- **No flash yet** (date picked has no data): *"The 05:30 pipeline hasn't run yet for 21 April. This should be ready by 06:00."*
- **No A-listers**: *"A quiet morning — no public figures expected."*
- **No departures of note**: *"All departures routine."*
- **No allergies**: *"No allergies or dietary requirements flagged — please still ask on seating."* (never pretend the field is irrelevant)
- **No hotel events**: *"No scheduled events. The calendar is clear."*

---

## 4. Signature interactions

These are the four details that turn "nice" into "they'll show it to everyone."

### 4.1 The morning greeting

At the top of the page, above the header, a single quiet line that changes through the day:

- 04:00–06:59 — *"Καλημέρα. 44 arrivals expected today. 18 dietary notes. 3 notable guests."*
- 07:00–11:59 — *"Good morning. 12 guests have checked in so far."*
- 12:00–16:59 — *"Mid-day update — 28 arrivals complete, 16 remaining."*
- 17:00+ — *"Evening — all arrivals checked in. Prepared for tomorrow."*

Generated server-side from the payload. Small serif italic. Completely ignorable but unmistakeably *there*.

### 4.2 The promote-to-special-attention moment

When Guest Relations spots a name the system missed, they click a small `+` at the end of any arrival row. A sheet slides from the right with a single field — *"Why this guest deserves attention"* — and a save button. On save, the guest lifts into the Special Attention list with a soft highlight animation. No modal with backdrop blur. No "Are you sure?" It's a hotel, not a bank.

### 4.3 The PDF that people actually want

Pressing `P` or clicking the export icon opens a print preview of the *exact same layout* — but cleaner: serif header, no nav chrome, no hover states. At the bottom, a single grey line: *"Daios Cove Luxury Resort & Villas · Daily Flash · Generated 05:30 UTC · Prepared for d.daios@daioshotels.com"*. It looks like something you'd leave on a pillow.

### 4.4 Keyboard everything

Power users don't touch the mouse.

| Key | Action |
|---|---|
| `G` `T` | Go to today |
| `G` `H` | Historical flashes |
| `J` / `K` | Next / previous guest in the arrivals list |
| `Enter` | Expand / collapse guest |
| `/` | Search by room, name, or allergy |
| `P` | Print / export PDF |
| `A` | Jump to A-lister section |
| `?` | Show shortcut cheat sheet |

Cheat sheet opens as a clean overlay — no modal frills, just a vertically-centered list of shortcuts in the same typography.

---

## 5. Component specs for Lovable

| Component | Notes |
|---|---|
| `Header` | Sticky, paper background, thin bottom rule on scroll. Contains logotype, date picker, user chip, PDF button. |
| `OccupancyStrip` | Three-column grid. `display` numerals with `count-up` animation on mount. |
| `GuestRow` | Collapsed-by-default, expands in place. Framer-motion `layout` transition, 320ms ease-out. |
| `ReasonPill` | Outlined only (0.5px). Never filled. |
| `BadgeIcon` | 14pt SVG, single-colour from palette. Tooltip on hover. |
| `AListerCard` | Gold top rule, content, favicon-link list. No shadow. |
| `SideRail` | Stacked sections: weather / MOD / events / inspections / show rooms / pool heating. Each has its own thin rule. |
| `EmptyState` | Italic prose, `ink.muted`, no illustration. |
| `PromoteSheet` | Right-side sheet (w-96), paper background, single textarea, save button. |
| `ShortcutCheatSheet` | Fullscreen overlay with ambient `paper` at 98% opacity. |

Tailwind tokens for the Lovable prompt — append to the existing `lovable-prompt.md`:

```js
// tailwind.config.ts (additive)
colors: {
  paper: '#F8F5EE',
  ink: {
    DEFAULT: '#1A1A17',
    muted: '#57564F',
    whisper: '#A09E95',
  },
  green: { DEFAULT: '#2E3D2F', soft: '#E7E5DD' },
  gold: '#A88A4A',
  terracotta: '#C85C3C',
  lavender: '#6B5B8F',
  rule: 'rgba(26,26,23,0.08)',
},
fontFamily: {
  display: ['GT Sectra Display', 'Cormorant Garamond', 'serif'],
  sans: ['Söhne', 'Inter', 'sans-serif'],
  mono: ['GT America Mono', 'JetBrains Mono', 'monospace'],
},
borderRadius: { none: '0', sm: '2px' },   // 0 by default
```

---

## 6. What to ship in Lovable v1 vs v2 polish

**v1 (Lovable prompt — now)**
- Dashboard layout + occupancy + all guest sections with expanded detail
- A-lister panel with confidence bar + evidence links
- Weather, MOD, events, pool heating side rail
- Magic-link login, role-gated content
- Promote-to-special-attention sheet
- Admin daily-briefing form
- Print CSS that produces the PDF-grade layout

**v2 (post-Lovable polish — in Cursor / direct code edit)**
- Real custom SVG icon set (10 glyphs — 2–3 days of illustrator time or a commission)
- Custom font licenses (GT Sectra + Söhne + GT America Mono)
- Count-up animation + the morning greeting
- Full keyboard navigation
- Dark mode
- Native iOS/iPadOS webclip with proper splash screen

v1 gets us to "lovely and obviously premium." v2 takes it to "people ask what you built this with."

---

## 7. One-paragraph brief to paste into Lovable's style prompt

> Build this as an editorial single-page briefing, not a dashboard. Warm off-white background (`#F8F5EE`), near-black ink (`#1A1A17`), deep olive green (`#2E3D2F`) as the only accent, with muted gold (`#A88A4A`) reserved for VIP and honeymoon moments and terracotta (`#C85C3C`) reserved exclusively for allergies. No cards, no shadows, no rounded corners — sections are defined by thin horizontal rules (`rgba(26,26,23,0.08)`). Typography carries the hierarchy: serif display (Cormorant Garamond or similar) for "DAIOS COVE" and the date, a clean humanist sans (Inter or Söhne) for body, monospace for room numbers. The page should feel like a Financial Times morning edition crossed with a 5-star hotel's printed menu — dense, confident, quiet until an allergy, pool fence or A-lister requires a second of attention. Match the composition sketched in `design-proposal.md`.

That paragraph is the one thing Lovable needs to get the tone right; the rest of the structural guidance is already in `lovable-prompt.md`.
