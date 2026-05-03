// Flash PDF HTML template — renders the Daily Flash one-pager at A4 landscape.
//
// Matches the existing Daios Cove Daily Flash PDF layout users are accustomed to:
//   - Top bar: 3-column occupancy (TODAY / TOMORROW / FOLLOWING) · DAIOS COVE title
//              · calendar · weather · MOD card · special request · show rooms · pool heating
//   - Middle: Special Attention Arrivals + Departures · Complimentary Partner Arrivals
//              · Allergies in-house · A-lister (conditional)
//   - Bottom: Hotel Events · Site Inspections · Group Events · Birthdays
//
// Designed to be rendered once per date (identical for all recipients) with one
// exception: the A-lister block is omitted when `includeAlister=false`.

// ─── Types ──────────────────────────────────────────────────────────────────

interface OccupancyRow {
  label?: string;
  date?: string;
  rooms_occupied?: number | string;
  occupied?: number | string;
  guests_inh?: number | string;
  arrivals?: number | string;
  departures?: number | string;
  occupancy_pct?: number | string;
  total_rooms?: number | string;
}

interface GuestRow {
  room?: string | number;
  room_number?: string | number;
  guest_name?: string;
  name?: string;
  reason?: string;
  notes?: string;
  nationality?: string;
  vip_status?: string;
  allergies?: string;
  allergy?: string;
}

interface AlisterRow {
  room?: string | number;
  subject?: string;
  name?: string;
  category?: string;
  classification?: string;
  summary?: string;
  finding?: string;
  reasoning?: string;
}

interface PoolRow { room?: string | number; status?: string; heating?: string; end_date?: string }
interface WeatherDay { date?: string; label?: string; temp_min?: number | string; temp_max?: number | string; emoji?: string }

interface Briefing {
  mod_name?: string;
  mod_phone?: string;
  hotel_events?: string;
  site_inspections?: string;
  group_events?: string;
  show_rooms?: string;
  notes?: string;
}

interface FlashPayload {
  occupancy?: OccupancyRow[];
  special_attention_arrivals?: GuestRow[];
  special_attention_departures?: GuestRow[];
  complimentary_partner_arrivals?: GuestRow[];
  pep_arrivals?: GuestRow[];
  birthdays_in_house?: GuestRow[];
  allergies_in_house?: GuestRow[];
  alister_findings?: AlisterRow[];
  pool_heating?: PoolRow[];
  daily_briefing?: Briefing | null;
  weather?: WeatherDay[];
}

// ─── Escaping ──────────────────────────────────────────────────────────────

function esc(s: unknown): string {
  return String(s ?? "")
    .replace(/&/g, "&amp;").replace(/</g, "&lt;")
    .replace(/>/g, "&gt;").replace(/"/g, "&quot;");
}

function formatAthensDate(iso: string): string {
  const [y, m, d] = iso.split("-");
  return `${d}/${m}/${y}`;
}

// ─── Renderers ──────────────────────────────────────────────────────────────

function renderOccupancyColumn(label: string, row: OccupancyRow | undefined): string {
  if (!row) return "";
  const occ = row.occupancy_pct;
  const occStr = typeof occ === "number" ? `${occ.toFixed(2)}%` : (occ ?? "—");
  return `<div class="occ-col">
  <div class="occ-label">${esc(label)}</div>
  <div class="occ-row"><span class="occ-k">Occ.Rooms</span><span class="occ-v">${esc(row.rooms_occupied ?? row.occupied ?? "")}</span></div>
  <div class="occ-row"><span class="occ-k">Guests INH</span><span class="occ-v">${esc(row.guests_inh ?? "")}</span></div>
  <div class="occ-row"><span class="occ-k">Arrivals</span><span class="occ-v">${esc(row.arrivals ?? "")}</span></div>
  <div class="occ-row"><span class="occ-k">Departures</span><span class="occ-v">${esc(row.departures ?? "")}</span></div>
  <div class="occ-row"><span class="occ-k">Occupancy</span><span class="occ-v">${esc(occStr)}</span></div>
</div>`;
}

function renderCalendar(isoDate: string): string {
  // Minimal month grid — highlights the target date.
  const [yearS, monthS, dayS] = isoDate.split("-");
  const year = parseInt(yearS), month = parseInt(monthS) - 1, day = parseInt(dayS);
  const first = new Date(year, month, 1);
  const daysInMonth = new Date(year, month + 1, 0).getDate();
  // Monday = 0 column (match the sample's M T W T F S S layout)
  const jsDay = first.getDay(); // 0=Sun
  const mondayOffset = (jsDay + 6) % 7; // 0=Mon .. 6=Sun
  const monthName = new Date(year, month, 1).toLocaleString("en-US", { month: "long" });
  const cells: string[] = [];
  for (let i = 0; i < mondayOffset; i++) cells.push(`<td></td>`);
  for (let d = 1; d <= daysInMonth; d++) {
    const hl = d === day ? ' class="cal-hl"' : "";
    cells.push(`<td${hl}>${d}</td>`);
  }
  while (cells.length % 7 !== 0) cells.push(`<td></td>`);
  const rows: string[] = [];
  for (let i = 0; i < cells.length; i += 7) rows.push(`<tr>${cells.slice(i, i + 7).join("")}</tr>`);
  return `<div class="cal">
  <div class="cal-title">${esc(monthName)}</div>
  <table class="cal-grid">
    <thead><tr><th>M</th><th>T</th><th>W</th><th>T</th><th>F</th><th>S</th><th>S</th></tr></thead>
    <tbody>${rows.join("")}</tbody>
  </table>
</div>`;
}

function renderWeather(rows: WeatherDay[] | undefined): string {
  if (!rows?.length) return `<div class="wx"><div class="wx-title">Weather Forecast</div></div>`;
  const items = rows.slice(0, 3).map((d) => {
    const label = d.label ?? d.date ?? "";
    const min = d.temp_min ?? "—";
    const max = d.temp_max ?? "—";
    const emoji = d.emoji ?? "";
    return `<div class="wx-row"><span class="wx-day">${esc(label)}</span><span class="wx-temp">${esc(min)}...${esc(max)} °C</span><span class="wx-emoji">${esc(emoji)}</span></div>`;
  }).join("");
  return `<div class="wx"><div class="wx-title">Weather Forecast</div>${items}</div>`;
}

function renderModCard(b: Briefing | null | undefined): string {
  if (!b?.mod_name) return `<div class="mod"><div class="mod-title">MOD</div></div>`;
  const phone = b.mod_phone ?? "";
  return `<div class="mod">
  <div class="mod-title">MOD</div>
  <div class="mod-name">${esc(b.mod_name)}</div>
  ${phone ? `<div class="mod-phone">${esc(phone)}</div>` : ""}
</div>`;
}

// Phase 28.7 (3 May 2026) — removed renderSpecialRequest() which duplicated
// allergies_in_house at the top of the page AND in the main guest-table
// section. User report: "allergies in-house appear in two separate places.
// should be only one." The canonical surface is now renderGuestTable
// "Allergies in-house" in the middle section. If a top-of-page allergy
// summary tile is wanted later, build it from a DIFFERENT slice (e.g.,
// only severe / first-night allergies) so it isn't a duplicate.

function renderShowRooms(b: Briefing | null | undefined): string {
  const text = b?.show_rooms ?? "";
  return `<div class="showrooms"><div class="showrooms-title">Show Rooms (from arrivals)</div><div class="showrooms-body">${esc(text)}</div></div>`;
}

function renderPoolHeating(rows: PoolRow[] | undefined): string {
  if (!rows?.length) return `<div class="pool"><div class="pool-title">Pool Heating &amp; Fence</div></div>`;
  const header = `<div class="pool-row pool-head"><span>Room</span><span>temp/fence</span><span>End Date</span></div>`;
  const body = rows.map((r) => {
    const room = r.room ?? "—";
    const status = r.status ?? r.heating ?? "";
    const end = r.end_date ?? "";
    return `<div class="pool-row"><span>${esc(room)}</span><span>${esc(status)}</span><span>${esc(end)}</span></div>`;
  }).join("");
  return `<div class="pool"><div class="pool-title">Pool Heating &amp; Fence</div>${header}${body}</div>`;
}

function renderGuestTable(title: string, rows: GuestRow[] | undefined): string {
  if (!rows?.length) return `<div class="gt"><div class="gt-title">${esc(title)}</div></div>`;
  const items = rows.map((r) => {
    const room = r.room ?? r.room_number ?? "—";
    const name = r.guest_name ?? r.name ?? "";
    const extras = [r.reason, r.notes, r.nationality, r.vip_status]
      .filter((x) => x != null && x !== "")
      .map((x) => esc(x))
      .join(" / ");
    return `<div class="gt-row"><span class="gt-room">${esc(room)}</span><span class="gt-name">${esc(name)}${extras ? ` - ${extras}` : ""}</span></div>`;
  }).join("");
  return `<div class="gt"><div class="gt-title">${esc(title)}</div>${items}</div>`;
}

function renderBirthdays(rows: GuestRow[] | undefined): string {
  // Phase 28.8 — line-by-line layout. Each guest gets its own row with the
  // age and departure date called out, instead of the generic dot-separated
  // soup that renderGuestTable produced.
  const title = "Birthdays - Anniversaries - Honeymoon";
  if (!rows?.length) return `<div class="gt"><div class="gt-title">${esc(title)}</div></div>`;
  const items = rows.map((r) => {
    const room    = r.room ?? r.room_number ?? "";
    const name    = r.guest_name ?? r.name ?? "";
    const ageNum  = typeof (r as any).age === "number" ? (r as any).age : ((r as any).age ? Number((r as any).age) : null);
    const ageText = ageNum && Number.isFinite(ageNum) ? `turns ${ageNum} today` : "birthday today";
    const dep     = (r as any).departure ?? "";
    const depText = dep ? `dep ${formatPdfDate(dep)}` : "";
    const tail    = [ageText, depText].filter(Boolean).join(" · ");
    return `<div class="gt-row"><span class="gt-room">${esc(room)}</span><span class="gt-name">${esc(name)}${tail ? ` - ${esc(tail)}` : ""}</span></div>`;
  }).join("");
  return `<div class="gt"><div class="gt-title">${esc(title)}</div>${items}</div>`;
}

function formatPdfDate(s: string): string {
  try {
    const d = new Date(s);
    if (isNaN(d.getTime())) return s;
    return d.toLocaleDateString("en-GB", { day: "2-digit", month: "short" });
  } catch {
    return s;
  }
}

function renderAlister(rows: AlisterRow[] | undefined): string {
  if (!rows?.length) return "";
  const items = rows.map((r) => {
    const room = r.room ?? "";
    const subject = r.subject ?? r.name ?? "";
    const category = r.category ?? r.classification ?? "";
    const summary = r.summary ?? r.finding ?? r.reasoning ?? "";
    return `<div class="al-row">
  <div class="al-head"><span class="al-subject">${esc(subject)}</span>${room ? `<span class="al-room">Room ${esc(room)}</span>` : ""}${category ? `<span class="al-cat">${esc(category)}</span>` : ""}</div>
  ${summary ? `<div class="al-summary">${esc(summary)}</div>` : ""}
</div>`;
  }).join("");
  return `<div class="al"><div class="al-title">A-lister Research</div>${items}</div>`;
}

function renderBriefingSection(title: string, value: string | undefined): string {
  if (!value) return `<div class="bs"><div class="bs-title">${esc(title)}</div></div>`;
  return `<div class="bs"><div class="bs-title">${esc(title)}</div><div class="bs-body">${esc(value)}</div></div>`;
}

// ─── Main renderer ─────────────────────────────────────────────────────────

export function renderFlashPdfHtml(
  payload: FlashPayload,
  reportDate: string,
  includeAlister: boolean,
): string {
  const occ = payload.occupancy ?? [];
  const briefing = payload.daily_briefing ?? null;

  return `<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Daios Cove Daily Flash — ${esc(formatAthensDate(reportDate))}</title>
<style>
  @page { size: A4 landscape; margin: 10mm; }
  * { box-sizing: border-box; }
  body { margin: 0; padding: 0; font-family: Arial, Helvetica, sans-serif; font-size: 9pt; color: #1a1a1a; background: #f5f5f0; }
  .page { width: 277mm; min-height: 190mm; background: #f5f5f0; padding: 4mm 6mm; }

  /* Top strip: 3-col occupancy + title + calendar + weather + MOD + special request + show rooms + pool */
  .top { display: grid; grid-template-columns: repeat(3, 1fr) 1.2fr 0.9fr 1fr 1.1fr 1.1fr; gap: 4mm; align-items: start; margin-bottom: 4mm; }
  .occ-col { font-size: 9pt; }
  .occ-label { font-weight: bold; font-size: 8pt; margin-bottom: 3px; color: #666; text-transform: uppercase; letter-spacing: 0.5px; }
  .occ-row { display: flex; justify-content: space-between; border-bottom: 1px dotted #e0e0e0; padding: 1px 0; }
  .occ-k { color: #555; }
  .occ-v { font-weight: 600; color: #1a1a1a; }

  .brand { display: flex; align-items: center; justify-content: center; }
  .brand-text { font-size: 28pt; font-weight: bold; color: #7a8064; letter-spacing: 2px; font-family: Georgia, 'Times New Roman', serif; }

  .cal { background: #fafaf5; border: 1px solid #d8d4c4; padding: 4px; font-size: 7pt; }
  .cal-title { font-weight: bold; text-align: center; margin-bottom: 2px; color: #7a8064; }
  .cal-grid { width: 100%; border-collapse: collapse; }
  .cal-grid th, .cal-grid td { width: calc(100%/7); text-align: center; padding: 1px; font-size: 7pt; }
  .cal-grid th { color: #888; font-weight: normal; }
  .cal-hl { color: #a33; font-weight: bold; border: 1px solid #a33; border-radius: 2px; }

  .wx { background: #eaf2f7; border: 1px solid #b8d4e3; padding: 4px 6px; font-size: 8pt; }
  .wx-title { font-weight: bold; text-align: center; color: #3a6b85; margin-bottom: 3px; font-size: 8pt; text-decoration: underline; }
  .wx-row { display: flex; justify-content: space-between; gap: 4px; padding: 1px 0; }
  .wx-day { font-weight: 600; min-width: 30px; }

  .mod { text-align: center; padding: 4px; font-size: 8pt; }
  .mod-title { text-decoration: underline; font-weight: bold; margin-bottom: 4px; }
  .mod-name { font-weight: bold; font-size: 9pt; }
  .mod-phone { font-size: 8pt; margin-top: 2px; }

  .sreq { font-size: 8pt; }
  .sreq-title { font-weight: bold; text-decoration: underline; margin-bottom: 3px; text-align: right; }
  .sreq-row { display: flex; gap: 4px; padding: 1px 0; font-size: 7.5pt; }
  .sreq-room { font-weight: 600; min-width: 22px; }

  .showrooms { font-size: 8pt; }
  .showrooms-title { font-weight: bold; text-decoration: underline; margin-bottom: 3px; text-align: right; }
  .showrooms-body { font-size: 7.5pt; line-height: 1.3; }

  .pool { font-size: 8pt; }
  .pool-title { font-weight: bold; text-decoration: underline; margin-bottom: 3px; text-align: right; }
  .pool-row { display: grid; grid-template-columns: 1fr 1fr 1fr; padding: 1px 0; font-size: 7.5pt; }
  .pool-head { font-weight: 600; border-bottom: 1px solid #ccc; }

  /* Middle: guest lists */
  .middle { display: grid; grid-template-columns: 1fr 1fr 1fr 1fr; gap: 4mm; margin-bottom: 4mm; }
  .gt-title, .al-title, .bs-title { font-size: 9pt; font-weight: bold; border-bottom: 1px solid #7a8064; padding-bottom: 2px; margin-bottom: 4px; color: #1a1a1a; }
  .gt-row { display: flex; gap: 4px; padding: 1px 0; border-bottom: 1px dotted #eee; font-size: 8pt; }
  .gt-room { font-weight: 600; min-width: 28px; color: #7a8064; }
  .gt-name { flex: 1; }

  /* A-lister */
  .al { margin-bottom: 4mm; }
  .al-row { border-left: 2px solid #7a8064; padding: 2px 6px; margin-bottom: 4px; background: #fafaf6; font-size: 8pt; }
  .al-head { display: flex; gap: 6px; align-items: baseline; }
  .al-subject { font-weight: 600; }
  .al-room { color: #7a8064; font-size: 7.5pt; }
  .al-cat { color: #888; font-size: 7pt; text-transform: uppercase; letter-spacing: 0.5px; }
  .al-summary { font-size: 7.5pt; color: #444; margin-top: 1px; line-height: 1.3; }

  /* Bottom: briefing sections */
  .bottom { display: grid; grid-template-columns: 1fr 1fr 1fr 1fr; gap: 4mm; }
  .bs-title { font-size: 9pt; }
  .bs-body { font-size: 7.5pt; line-height: 1.3; white-space: pre-wrap; }
</style>
</head>
<body>
<div class="page">

  <div class="top">
    ${renderOccupancyColumn("TODAY", occ[0])}
    ${renderOccupancyColumn("TOMORROW", occ[1])}
    ${renderOccupancyColumn("FOLLOWING", occ[2])}
    <div class="brand"><span class="brand-text">DAIOS COVE</span></div>
    ${renderCalendar(reportDate)}
    ${renderWeather(payload.weather)}
    ${renderModCard(briefing)}
  </div>

  <div class="top" style="grid-template-columns: 1fr 1fr; gap: 4mm; margin-bottom: 4mm;">
    ${renderShowRooms(briefing)}
    ${renderPoolHeating(payload.pool_heating)}
  </div>

  <div class="middle">
    ${renderGuestTable("Special Attention Arrivals", payload.special_attention_arrivals)}
    ${renderGuestTable("Special Attention Departures", payload.special_attention_departures)}
    ${renderGuestTable("Complimentary / Partner Arrivals", payload.complimentary_partner_arrivals)}
    ${renderGuestTable("Allergies in-house", payload.allergies_in_house)}
  </div>

  ${includeAlister ? renderAlister(payload.alister_findings) : ""}

  <div class="bottom">
    ${renderBriefingSection("Hotel Events", briefing?.hotel_events)}
    ${renderBriefingSection("Site Inspections - Fam Trips - Info Groups", briefing?.site_inspections)}
    ${renderBriefingSection("Group Events", briefing?.group_events)}
    ${renderBirthdays(payload.birthdays_in_house)}
  </div>

</div>
</body>
</html>`;
}
