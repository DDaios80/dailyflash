// ask-daios — Phase 26.1
//
// Text-only Q&A backend for the "Ask Daios" assistant. Caller passes a
// question + their JWT. We:
//   1. Verify the JWT, get user_id + role
//   2. Pull today's flash_reports.payload + role-gated zoho_notes
//   3. Call Claude Opus 4.7 with system prompt + cached context
//   4. Log the Q&A to daios_qa_log
//   5. Return the answer
//
// Voice (26.2) wraps this with browser SpeechRecognition + SpeechSynthesis.
// ElevenLabs upgrade (26.3) replaces the TTS layer.
//
// Contract:
//   POST /functions/v1/ask-daios
//   Headers: Authorization: Bearer <CALLER_JWT>
//   Body:    { question: string, language?: 'en'|'el', client?: 'web'|'voice' }
//   Returns: { ok: true, answer, qa_log_id, latency_ms } | { ok: false, error }

import { createClient } from "jsr:@supabase/supabase-js@2";
import Anthropic from "npm:@anthropic-ai/sdk@0.30.1";

const CORS_HEADERS = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Methods": "POST, OPTIONS",
  "Access-Control-Allow-Headers": "authorization, x-client-info, apikey, content-type",
  "Access-Control-Max-Age": "86400",
};

const MODEL = "claude-opus-4-7";

Deno.serve(async (req) => {
  if (req.method === "OPTIONS") {
    return new Response(null, { status: 204, headers: CORS_HEADERS });
  }
  if (req.method !== "POST") return json({ ok: false, error: "POST only" }, 405);

  const supabaseUrl = Deno.env.get("SUPABASE_URL");
  const serviceKey  = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY");
  const anthropicKey = Deno.env.get("ANTHROPIC_API_KEY");
  if (!supabaseUrl || !serviceKey || !anthropicKey) {
    return json({ ok: false, error: "server env missing" }, 500);
  }

  const callerJwt = (req.headers.get("authorization") ?? "").replace(/^Bearer\s+/i, "");
  if (!callerJwt) return json({ ok: false, error: "authorization header required" }, 401);

  // Auth via caller JWT — also picks up the active session for RLS
  const supaCaller = createClient(supabaseUrl, serviceKey, {
    global: { headers: { Authorization: `Bearer ${callerJwt}` } },
  });
  const { data: caller } = await supaCaller.auth.getUser();
  if (!caller?.user?.id) return json({ ok: false, error: "invalid caller JWT" }, 401);

  const userId = caller.user.id;
  const userName = (caller.user.user_metadata?.full_name as string)
                 || (caller.user.user_metadata?.name as string)
                 || caller.user.email
                 || "Unknown";

  const body = await req.json().catch(() => ({}));
  const question: string = (body?.question ?? "").toString().trim();
  const language: "en" | "el" = body?.language === "el" ? "el" : "en";
  const client: string = body?.client === "voice" ? "voice" : "web";

  if (!question || question.length > 5000) {
    return json({ ok: false, error: "question must be 1-5000 chars" }, 400);
  }

  // Service role for context fetch + log insert (bypasses RLS so we get all
  // role-relevant data; we apply our own role gating below).
  const supa = createClient(supabaseUrl, serviceKey);

  const { data: roleRow } = await supa
    .from("user_roles").select("role").eq("user_id", userId).maybeSingle();
  const userRole: string = roleRow?.role ?? "general";

  const t0 = Date.now();
  let answer = "";
  let promptTokens = 0;
  let completionTokens = 0;
  let cacheReadTokens = 0;
  let costUsd = 0;
  let errorMsg: string | null = null;
  let contextKeys: string[] = [];
  let contextTokenCount = 0;

  try {
    // ── Build context based on role ────────────────────────────────────
    const ctx = await buildContext(supa, userRole);
    contextKeys = Object.keys(ctx);
    const contextJson = JSON.stringify(ctx);
    // Rough token estimate: 4 chars per token
    contextTokenCount = Math.round(contextJson.length / 4);

    // ── Compose system prompt ──────────────────────────────────────────
    const sys = systemPrompt({
      userName,
      userRole,
      language,
      reportDate: ctx?.report_date ?? "",
      athensToday: athensToday(),
    });

    // ── Call Claude with prompt cache on system + context ──────────────
    const anthropic = new Anthropic({ apiKey: anthropicKey });

    const res = await anthropic.messages.create({
      model: MODEL,
      max_tokens: 600,
      system: [
        { type: "text", text: sys, cache_control: { type: "ephemeral" } },
        {
          type: "text",
          text: `\n\n=== Today's data ===\n${contextJson}\n=== End of data ===`,
          cache_control: { type: "ephemeral" },
        },
      ],
      messages: [{ role: "user", content: question }],
    });

    answer = res.content
      .filter((b: any) => b.type === "text")
      .map((b: any) => b.text)
      .join("")
      .trim();

    promptTokens     = res.usage?.input_tokens ?? 0;
    completionTokens = res.usage?.output_tokens ?? 0;
    cacheReadTokens  = (res.usage as any)?.cache_read_input_tokens ?? 0;
    costUsd = estimateCost({ promptTokens, completionTokens, cacheReadTokens });
  } catch (e) {
    errorMsg = String((e as Error)?.message ?? e).slice(0, 1000);
  }

  const latencyMs = Date.now() - t0;

  // ── Log Q&A ──────────────────────────────────────────────────────────
  const { data: logRow } = await supa
    .from("daios_qa_log")
    .insert({
      user_id: userId,
      user_role: userRole,
      user_name: userName,
      question,
      language,
      answer: errorMsg ? null : answer,
      context_keys: contextKeys,
      context_token_count: contextTokenCount,
      prompt_tokens: promptTokens,
      completion_tokens: completionTokens,
      cache_read_tokens: cacheReadTokens,
      cost_usd: costUsd,
      latency_ms: latencyMs,
      model: MODEL,
      client,
      error: errorMsg,
    })
    .select("id")
    .single();

  if (errorMsg) return json({ ok: false, error: errorMsg, latency_ms: latencyMs }, 500);

  return json({
    ok: true,
    answer,
    qa_log_id: logRow?.id,
    latency_ms: latencyMs,
  });
});


// ─── Context assembly ────────────────────────────────────────────────────
async function buildContext(supa: any, userRole: string): Promise<Record<string, unknown>> {
  // Tomorrow's report (= what tonight's flash represents)
  const reportDate = athensTomorrow();
  const ctx: Record<string, unknown> = { report_date: reportDate };

  // 1. Today's flash payload (everyone in service can ask about it)
  const { data: fr } = await supa
    .from("flash_reports")
    .select("payload, computed_at")
    .eq("report_date", reportDate)
    .maybeSingle();
  if (fr?.payload) {
    ctx.flash = strippedPayload(fr.payload, userRole);
  }

  // 2. Pending complaints — everyone in service except spa/finance/hr/it
  if (canSeeComplaints(userRole)) {
    const { data: pc } = await supa
      .from("zoho_notes")
      .select("room,guest_name,subject,body,status,note_created_at")
      .eq("source_type", "pending_complaints")
      .is("resolved_at", null)
      .order("note_created_at", { ascending: false })
      .limit(20);
    ctx.pending_complaints = pc ?? [];
  }

  // 3. Medical notes — admin / management / guest_relations only
  if (canSeeMedical(userRole)) {
    const { data: med } = await supa
      .from("zoho_notes")
      .select("room,guest_name,subject,body,note_created_at")
      .eq("source_type", "medical_notes")
      .order("note_created_at", { ascending: false })
      .limit(20);
    ctx.medical_notes = med ?? [];
  }

  // 4. All-notes (allergies, departmental notes) — service roles
  if (canSeeServiceNotes(userRole)) {
    const { data: nn } = await supa
      .from("zoho_notes")
      .select("room,guest_name,subject,body,note_created_at")
      .eq("source_type", "all_notes")
      .order("note_created_at", { ascending: false })
      .limit(80);
    ctx.service_notes = nn ?? [];
  }

  // 5. Today's excursions
  const { data: ex } = await supa
    .from("zoho_notes")
    .select("room,guest_name,subject,body,note_date")
    .eq("source_type", "excursions")
    .eq("note_date", reportDate)
    .limit(10);
  ctx.todays_activities = ex ?? [];

  // 6. Open ideas (committee + submitter; we bound it widely here, the
  //    answer behaviour is gated by what the user is allowed to see anyway).
  if (userRole === "admin" || userRole === "management") {
    const { data: ideas } = await supa
      .from("ideas")
      .select("id,subject,status,severity,category,for_monday_at,monday_meeting_date,created_at")
      .in("status", ["submitted", "acknowledged", "in_discussion", "for_monday"])
      .order("created_at", { ascending: false })
      .limit(20);
    ctx.open_ideas = ideas ?? [];
  }

  return ctx;
}


// Strip PII / role-irrelevant fields from the flash payload.
function strippedPayload(payload: any, userRole: string): any {
  const out: any = {};
  // Always-safe fields
  for (const k of [
    "report_date", "occupancy", "weather",
    "totals", "alister_findings_count",
  ]) {
    if (payload?.[k] !== undefined) out[k] = payload[k];
  }
  // Role-gated arrays
  const inService = canSeeServiceNotes(userRole);
  const seeAlister = ["admin","management","guest_relations"].includes(userRole);
  const seeBookingCom = ["admin","management","guest_relations","front_office","f_and_b","kitchen","housekeeping"].includes(userRole);

  if (inService && payload.special_attention_arrivals)   out.special_attention_arrivals = payload.special_attention_arrivals;
  if (inService && payload.special_attention_departures) out.special_attention_departures = payload.special_attention_departures;
  if (inService && payload.complimentary_partner_arrivals) out.complimentary_partner_arrivals = payload.complimentary_partner_arrivals;
  if (inService && payload.pep_arrivals) out.pep_arrivals = payload.pep_arrivals;
  if (seeBookingCom && payload.booking_com_arrivals) out.booking_com_arrivals = payload.booking_com_arrivals;
  if (inService && payload.birthdays_in_house) out.birthdays_in_house = payload.birthdays_in_house;
  if (inService && payload.allergies_in_house) out.allergies_in_house = payload.allergies_in_house;
  if (seeAlister && payload.alister_findings) out.alister_findings = payload.alister_findings;
  if (inService && payload.daily_briefing) out.daily_briefing = payload.daily_briefing;
  if (inService && payload.zoho_pending_complaints) out.zoho_pending_complaints = payload.zoho_pending_complaints;
  if (inService && payload.zoho_todays_activities) out.zoho_todays_activities = payload.zoho_todays_activities;
  if (inService && payload.zoho_hsk_summary) out.zoho_hsk_summary = payload.zoho_hsk_summary;

  return out;
}


function canSeeMedical(role: string): boolean {
  return ["admin", "management", "guest_relations"].includes(role);
}

function canSeeComplaints(role: string): boolean {
  return ["admin","management","guest_relations","front_office","f_and_b","kitchen","housekeeping","reservations","sales","marketing","call_center"].includes(role);
}

function canSeeServiceNotes(role: string): boolean {
  return canSeeComplaints(role); // same set for v1
}


// ─── System prompt ───────────────────────────────────────────────────────
function systemPrompt(o: {
  userName: string; userRole: string; language: "en"|"el";
  reportDate: string; athensToday: string;
}): string {
  const langInstr = o.language === "el"
    ? "Always answer in Greek (Ελληνικά)."
    : "Always answer in English unless the question is asked in Greek.";

  return `You are the Daios Cove assistant. You give short, factual answers about today's operations at Daios Cove, a 5-star resort on Crete.

User: ${o.userName} (role: ${o.userRole})
Today (Athens): ${o.athensToday}
Tonight's flash report covers: ${o.reportDate}

Voice you must use:
- Concierge tone — calm, professional, factual.
- No filler. No "Great question!" or "I'd be happy to help".
- Aim for 30 words or fewer for any single answer. Cap at 60 words.
- If 5 or fewer items, list them. If more than 5, give the count plus the top 2-3 highlights, then end with "Want the rest?" so the user can opt in to a longer follow-up.
- Numbers spoken aloud: write digits as words for small counts ("five guests"); leave bigger numbers as digits ("237 guests").
- Use exact spelling from the data for names — including non-Latin and accented characters.
- For room numbers: read each digit individually if it's clearer ("five-three-seven" for 537).
- Never speculate. If the data doesn't contain the answer, say "I don't have that information for tonight" or equivalent.
- Don't repeat the question back to the user.
- Don't mention "data" or "context" — speak as if you naturally know.
- Don't recommend, don't add commentary. Just the facts. (Exception: if the user explicitly asks "what should we do".)

Privacy and PII:
- The user's role determines what you've been given. Only mention what's in the data.
- If a guest is anonymous in any source, do not reveal identity even if you somehow have it.
- Never read out a confirmation number, email, phone, or full payment data.

Language:
- ${langInstr}

If you're asked something outside the resort's daily operations (general knowledge, weather forecasts beyond what's in the data, sports scores, etc.), politely decline: "I'm focused on today at the Cove."`;
}


// ─── Pricing ──────────────────────────────────────────────────────────────
function estimateCost(o: {
  promptTokens: number; completionTokens: number; cacheReadTokens: number;
}): number {
  // Opus 4.7 prices: input $5/1M, output $25/1M, cache read 0.1× input ($0.50/1M)
  const inputPaid = Math.max(0, o.promptTokens - o.cacheReadTokens);
  const inputUsd  = (inputPaid * 5) / 1_000_000;
  const cacheUsd  = (o.cacheReadTokens * 0.5) / 1_000_000;
  const outputUsd = (o.completionTokens * 25) / 1_000_000;
  return Number((inputUsd + cacheUsd + outputUsd).toFixed(5));
}


// ─── Date helpers ─────────────────────────────────────────────────────────
function athensToday(): string {
  return new Intl.DateTimeFormat("en-CA", {
    timeZone: "Europe/Athens", year: "numeric", month: "2-digit", day: "2-digit",
  }).format(new Date()); // YYYY-MM-DD
}

function athensTomorrow(): string {
  const today = athensToday();
  const d = new Date(today + "T00:00:00Z");
  d.setUTCDate(d.getUTCDate() + 1);
  return d.toISOString().slice(0, 10);
}


function json(p: unknown, status = 200): Response {
  return new Response(JSON.stringify(p), {
    status,
    headers: { "content-type": "application/json", ...CORS_HEADERS },
  });
}
