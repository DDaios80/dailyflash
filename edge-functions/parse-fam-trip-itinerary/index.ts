// parse-fam-trip-itinerary — Lovable Cloud edge function.
//
// Given a FAM trip id, downloads its PDF from Supabase Storage, extracts
// the text via pdf-parse, sends the text to Lovable AI Gateway (Gemini
// 2.5 Pro by default) with JSON-mode on, and UPDATEs
// fam_trips.itinerary_by_day with the parsed result.
//
// Rationale for Lovable AI over Anthropic direct:
//   * FAM trip PDFs are well-structured operational documents — clear
//     date headings + time-stamped bullets. Not a nuanced reasoning task
//     where Claude's edge over Gemini matters meaningfully.
//   * Consistent with analyze-idea (also on Lovable AI Gateway).
//   * No separate ANTHROPIC_API_KEY to manage per edge function.
//   * ~60x cheaper per parse (though volume is trivially small anyway).
//
// Contract:
//   POST /functions/v1/parse-fam-trip-itinerary
//   Headers:  Authorization: Bearer <PIPELINE_SECRET>
//   Body:     { "trip_id": "uuid" }

import { createClient } from "jsr:@supabase/supabase-js@2";
import pdfParse from "npm:pdf-parse@1.1.1";

const MODEL = Deno.env.get("ITINERARY_MODEL") ?? "google/gemini-2.5-pro";
const LOVABLE_AI_URL = "https://ai.gateway.lovable.dev/v1/chat/completions";

Deno.serve(async (req) => {
  if (req.method !== "POST") return json({ error: "POST only" }, 405);

  const secret = Deno.env.get("PIPELINE_SECRET");
  const auth = req.headers.get("authorization") ?? "";
  if (!secret || auth !== `Bearer ${secret}`) return json({ error: "unauthorized" }, 401);

  const lovableKey  = Deno.env.get("LOVABLE_API_KEY");
  const supabaseUrl = Deno.env.get("SUPABASE_URL");
  const serviceKey  = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY");
  if (!lovableKey)  return json({ error: "LOVABLE_API_KEY not set" }, 500);
  if (!supabaseUrl || !serviceKey) return json({ error: "Supabase env missing" }, 500);

  const body = await req.json().catch(() => ({}));
  const tripId: string | undefined = body?.trip_id;
  if (!tripId) return json({ error: "trip_id required" }, 400);

  const supa = createClient(supabaseUrl, serviceKey);

  // Fetch the trip
  const { data: trip, error: tErr } = await supa
    .from("fam_trips")
    .select("id, name, start_date, end_date, pdf_path")
    .eq("id", tripId)
    .maybeSingle();
  if (tErr) return json({ error: "lookup failed: " + tErr.message }, 500);
  if (!trip) return json({ error: "fam trip not found" }, 404);
  if (!trip.pdf_path) {
    await recordError(supa, tripId, "no pdf_path on fam_trip row");
    return json({ error: "pdf_path not set" }, 400);
  }

  // Download the PDF
  const { data: blob, error: dlErr } = await supa.storage
    .from("fam-trip-pdfs").download(trip.pdf_path);
  if (dlErr || !blob) {
    const msg = "pdf download failed: " + (dlErr?.message ?? "unknown");
    await recordError(supa, tripId, msg);
    return json({ error: msg }, 502);
  }

  // Extract text
  let pdfText: string;
  try {
    const buf = new Uint8Array(await blob.arrayBuffer());
    // pdf-parse accepts Buffer or Uint8Array
    const parsedPdf = await pdfParse(buf);
    pdfText = (parsedPdf.text ?? "").trim();
  } catch (e) {
    const msg = "pdf text extraction failed: " + String((e as Error)?.message ?? e).slice(0, 300);
    await recordError(supa, tripId, msg);
    return json({ error: msg }, 502);
  }

  if (pdfText.length < 50) {
    await recordError(supa, tripId, `extracted text too short (${pdfText.length} chars)`);
    return json({ error: "PDF has no extractable text (scan-only?)" }, 422);
  }

  // Call Lovable AI
  let parsed: Record<string, Activity[]> | null = null;
  let aiErr: string | null = null;
  try {
    parsed = await extractItinerary({
      apiKey: lovableKey,
      pdfText,
      tripName: trip.name as string,
      startDate: trip.start_date as string,
      endDate: trip.end_date as string,
    });
  } catch (e) {
    aiErr = String((e as Error)?.message ?? e).slice(0, 500);
  }

  if (aiErr || !parsed) {
    await recordError(supa, tripId, aiErr ?? "no itinerary returned");
    return json({ ok: false, error: aiErr ?? "no itinerary returned" }, 502);
  }

  // Persist
  const { error: upErr } = await supa
    .from("fam_trips")
    .update({
      itinerary_by_day: parsed,
      itinerary_parsed_at: new Date().toISOString(),
      itinerary_parse_error: null,
    })
    .eq("id", tripId);
  if (upErr) {
    return json({ error: "update failed: " + upErr.message }, 500);
  }

  const dayCount = Object.keys(parsed).length;
  const activityCount = Object.values(parsed).reduce((n, a) => n + a.length, 0);
  return json({
    ok: true,
    trip_id: tripId,
    days_parsed: dayCount,
    activities_parsed: activityCount,
    model: MODEL,
    pdf_text_chars: pdfText.length,
  });
});

// ─── Types ──────────────────────────────────────────────────────────────────

interface Activity {
  time: string | null;
  title: string;
  detail?: string;
}

// ─── Lovable AI Gateway call ────────────────────────────────────────────────

async function extractItinerary(opts: {
  apiKey: string;
  pdfText: string;
  tripName: string;
  startDate: string;
  endDate: string;
}): Promise<Record<string, Activity[]>> {
  const systemPrompt = SYSTEM_PROMPT;
  const userPrompt = buildUserPrompt(opts);

  const resp = await fetch(LOVABLE_AI_URL, {
    method: "POST",
    headers: {
      "Authorization": `Bearer ${opts.apiKey}`,
      "Content-Type": "application/json",
    },
    body: JSON.stringify({
      model: MODEL,
      messages: [
        { role: "system", content: systemPrompt },
        { role: "user",   content: userPrompt },
      ],
      response_format: { type: "json_object" },
      temperature: 0.2,
      max_tokens: 8000,
    }),
  });

  if (!resp.ok) {
    const text = await resp.text();
    throw new Error(`Lovable AI ${resp.status}: ${text.slice(0, 400)}`);
  }

  const data = await resp.json() as {
    choices?: Array<{ message?: { content?: string } }>;
  };
  const content = data.choices?.[0]?.message?.content ?? "";
  if (!content) throw new Error("no content in Lovable AI response");

  const parsed = parseJsonObject(content);
  if (!parsed) throw new Error(`could not parse JSON: ${content.slice(0, 300)}`);

  return normaliseItinerary(parsed, opts.startDate, opts.endDate);
}

const SYSTEM_PROMPT = `You extract day-by-day itineraries from hotel FAM trip documents. The input is plain text extracted from a PDF. Output is a strict JSON object keyed by ISO date with arrays of activities.

Rules:
- Output ONLY a JSON object. No prose, no code fences. Start with "{" and end with "}".
- Activity shape: { "time": "HH:MM" | null, "title": "short label", "detail": "one-sentence summary" }.
- Include only days within the trip's stated date range.
- time = null for non-timed items (e.g. "Breakfast at leisure", "Free time").
- detail stays a single sentence. Pack pax counts, flight numbers, restaurant names, named attendees into it.
- Keep titles short (<= 8 words). Imperative / noun-phrase form: "Gatwick arrival", "Dinner at Taverna".
- Do NOT invent activities. If a day in the range has no structured schedule in the text, return an empty array for it.
- If you are uncertain about a time, set time=null and include the uncertainty in detail ("~morning", "after breakfast").

Return shape:
{
  "YYYY-MM-DD": [
    {"time": "HH:MM" | null, "title": string, "detail": string},
    ...
  ],
  ...
}`;

function buildUserPrompt(opts: {
  pdfText: string; tripName: string; startDate: string; endDate: string;
}): string {
  return [
    `Trip: ${opts.tripName}`,
    `Date range: ${opts.startDate} to ${opts.endDate}`,
    ``,
    `Extract the day-by-day itinerary. Only include dates between ${opts.startDate} and ${opts.endDate} inclusive.`,
    ``,
    `DOCUMENT TEXT:`,
    `---`,
    opts.pdfText,
    `---`,
    ``,
    `Return the JSON object now.`,
  ].join("\n");
}

// ─── JSON extraction + normalisation ───────────────────────────────────────

function parseJsonObject(text: string): any | null {
  let t = text.trim();
  t = t.replace(/^```(?:json)?\s*\n?/, "").replace(/\s*```\s*$/, "");
  for (let i = 0; i < t.length; i++) {
    if (t[i] !== "{") continue;
    let depth = 0, inStr = false, esc = false;
    for (let j = i; j < t.length; j++) {
      const c = t[j];
      if (esc) { esc = false; continue; }
      if (c === "\\") { esc = true; continue; }
      if (c === '"') { inStr = !inStr; continue; }
      if (inStr) continue;
      if (c === "{") depth++;
      else if (c === "}") {
        depth--;
        if (depth === 0) {
          try { return JSON.parse(t.slice(i, j + 1)); }
          catch { break; }
        }
      }
    }
  }
  return null;
}

function normaliseItinerary(
  raw: any,
  startDate: string,
  endDate: string,
): Record<string, Activity[]> {
  const out: Record<string, Activity[]> = {};
  if (!raw || typeof raw !== "object") return out;

  const start = new Date(startDate);
  const end = new Date(endDate);

  for (const [dateKey, rawActs] of Object.entries(raw)) {
    if (!/^\d{4}-\d{2}-\d{2}$/.test(dateKey)) continue;
    const d = new Date(dateKey);
    if (d < start || d > end) continue;

    const acts: Activity[] = [];
    if (Array.isArray(rawActs)) {
      for (const a of rawActs as any[]) {
        if (!a || typeof a !== "object") continue;
        const title = String(a.title ?? "").trim().slice(0, 120);
        if (!title) continue;
        const timeRaw = a.time;
        let time: string | null = null;
        if (typeof timeRaw === "string" && /^\d{1,2}:\d{2}$/.test(timeRaw.trim())) {
          const [h, m] = timeRaw.trim().split(":").map((x) => x.padStart(2, "0"));
          time = `${h.padStart(2, "0")}:${m}`;
        }
        const detail = a.detail ? String(a.detail).trim().slice(0, 500) : undefined;
        acts.push({ time, title, detail });
      }
    }
    acts.sort((x, y) => {
      if (x.time === null && y.time === null) return 0;
      if (x.time === null) return 1;
      if (y.time === null) return -1;
      return x.time.localeCompare(y.time);
    });
    out[dateKey] = acts;
  }

  return out;
}

// ─── Helpers ────────────────────────────────────────────────────────────────

async function recordError(supa: any, tripId: string, msg: string): Promise<void> {
  try {
    await supa.from("fam_trips").update({
      itinerary_parse_error: msg.slice(0, 1000),
      itinerary_parsed_at: new Date().toISOString(),
    }).eq("id", tripId);
  } catch (_) { /* best-effort */ }
}

function json(p: unknown, s = 200) {
  return new Response(JSON.stringify(p), { status: s, headers: { "content-type": "application/json" } });
}
