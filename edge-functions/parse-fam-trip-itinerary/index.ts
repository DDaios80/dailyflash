// parse-fam-trip-itinerary — Lovable Cloud edge function.
//
// Given a FAM trip id, downloads its PDF from Supabase Storage, passes it
// natively to Claude Haiku 4.5 (cheap + solid at PDF extraction), asks
// Claude to return a day-by-day itinerary as JSON, and UPDATEs the
// fam_trips.itinerary_by_day column with the result.
//
// Design notes:
//   * Anthropic direct API (not Lovable AI Gateway) because Claude's
//     native PDF handling is meaningfully better for itinerary extraction
//     than any OpenAI-compatible alternative through the gateway.
//   * Model: claude-haiku-4-5 ($1/$5 per 1M tokens, ~$0.02 per FAM trip
//     parse). Low volume (few trips per month) makes cost trivial.
//   * Idempotent: re-running overwrites itinerary_by_day. Admin "Re-parse"
//     button hits this same endpoint.
//
// Contract:
//   POST /functions/v1/parse-fam-trip-itinerary
//   Headers:  Authorization: Bearer <PIPELINE_SECRET>
//   Body:     { "trip_id": "uuid" }

import { createClient } from "jsr:@supabase/supabase-js@2";

const MODEL = Deno.env.get("ITINERARY_MODEL") ?? "claude-haiku-4-5";
const ANTHROPIC_VERSION = "2023-06-01";

Deno.serve(async (req) => {
  if (req.method !== "POST") return json({ error: "POST only" }, 405);

  const secret = Deno.env.get("PIPELINE_SECRET");
  const auth = req.headers.get("authorization") ?? "";
  if (!secret || auth !== `Bearer ${secret}`) return json({ error: "unauthorized" }, 401);

  const anthropicKey = Deno.env.get("ANTHROPIC_API_KEY");
  const supabaseUrl  = Deno.env.get("SUPABASE_URL");
  const serviceKey   = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY");
  if (!anthropicKey) return json({ error: "ANTHROPIC_API_KEY not set" }, 500);
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
  const pdfBase64 = bufferToBase64(new Uint8Array(await blob.arrayBuffer()));

  // Ask Claude
  let parsed: Record<string, Activity[]> | null = null;
  let aiErr: string | null = null;
  try {
    parsed = await extractItinerary({
      apiKey: anthropicKey,
      pdfBase64,
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
  });
});

// ─── Types ──────────────────────────────────────────────────────────────────

interface Activity {
  time: string | null;    // "HH:MM" or null for non-timed activities
  title: string;
  detail?: string;
}

// ─── Claude call (PDF native ingestion) ─────────────────────────────────────

async function extractItinerary(opts: {
  apiKey: string;
  pdfBase64: string;
  tripName: string;
  startDate: string;
  endDate: string;
}): Promise<Record<string, Activity[]>> {
  const userPrompt = [
    `Extract the day-by-day itinerary from this FAM trip PDF.`,
    ``,
    `Trip: ${opts.tripName}`,
    `Date range: ${opts.startDate} to ${opts.endDate}`,
    ``,
    `For every day covered by the PDF, extract each scheduled activity and return a JSON object keyed by ISO date (YYYY-MM-DD) whose values are arrays of activities.`,
    ``,
    `Activity shape: { "time": "HH:MM" | null, "title": "short label", "detail": "one-sentence summary including pax, location, flight, or contact if mentioned" }.`,
    ``,
    `Rules:`,
    `- Only include days within the trip's date range (${opts.startDate} .. ${opts.endDate}).`,
    `- time is null for non-timed items (e.g. "Breakfast at leisure").`,
    `- detail stays one sentence. Pack pax counts, flight numbers, restaurant names, and named attendees into it.`,
    `- Keep titles short (<= 8 words). Use imperative / noun-phrase form: "Gatwick arrival", "Dinner at Taverna", "Ball Room session".`,
    `- Do NOT invent activities. If the PDF has no structured schedule for a day, return an empty array for that day.`,
    `- Return ONLY the JSON object. Start with "{" and end with "}". No preamble, no code fences, no trailing prose.`,
    ``,
    `Example return shape:`,
    `{`,
    `  "${opts.startDate}": [`,
    `    {"time": "13:10", "title": "Gatwick arrival", "detail": "22 pax on EZY8215, shared minibus transfer to DC arriving ~15:00"},`,
    `    {"time": "19:30", "title": "Dinner at Taverna", "detail": "14 agents + host (Scott or Lyndsey)"}`,
    `  ]`,
    `}`,
  ].join("\n");

  const resp = await fetch("https://api.anthropic.com/v1/messages", {
    method: "POST",
    headers: {
      "x-api-key": opts.apiKey,
      "anthropic-version": ANTHROPIC_VERSION,
      "content-type": "application/json",
    },
    body: JSON.stringify({
      model: MODEL,
      max_tokens: 8000,
      messages: [
        {
          role: "user",
          content: [
            {
              type: "document",
              source: {
                type: "base64",
                media_type: "application/pdf",
                data: opts.pdfBase64,
              },
            },
            { type: "text", text: userPrompt },
          ],
        },
      ],
    }),
  });

  if (!resp.ok) {
    const text = await resp.text();
    throw new Error(`Anthropic ${resp.status}: ${text.slice(0, 400)}`);
  }

  const data = await resp.json() as { content: Array<{ type: string; text?: string }> };
  let lastText = "";
  for (const block of data.content || []) {
    if (block.type === "text" && typeof block.text === "string") {
      lastText = block.text;
    }
  }
  if (!lastText) throw new Error("no text in Claude response");

  const parsed = parseJsonObject(lastText);
  if (!parsed) throw new Error(`could not parse JSON: ${lastText.slice(0, 300)}`);

  return normaliseItinerary(parsed, opts.startDate, opts.endDate);
}

// ─── JSON extraction + validation ──────────────────────────────────────────

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
    // Sort by time ASC, nulls last
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

function bufferToBase64(buf: Uint8Array): string {
  const chunkSize = 32768;
  let binary = "";
  for (let i = 0; i < buf.length; i += chunkSize) {
    binary += String.fromCharCode.apply(null, Array.from(buf.subarray(i, i + chunkSize)) as number[]);
  }
  return btoa(binary);
}

function json(p: unknown, s = 200) {
  return new Response(JSON.stringify(p), { status: s, headers: { "content-type": "application/json" } });
}
