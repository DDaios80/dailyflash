// ask-daios — Phase 26.1 (+ Phase 27 graph tools)
//
// Text-only Q&A backend for the "Ask Daios" assistant. Caller passes a
// question + their JWT. We:
//   1. Verify the JWT, get user_id + role
//   2. Pull today's flash_reports.payload + role-gated zoho_notes
//   3. Call Claude Opus 4.7 with system prompt + cached context
//      + tool definitions for cross-cutting graph queries (Phase 27)
//   4. Run the tool-use loop until Claude has all it needs
//   5. Log the Q&A to daios_qa_log
//   6. Return the answer
//
// Voice (26.2) wraps this with browser SpeechRecognition + SpeechSynthesis.
// ElevenLabs upgrade (26.3) replaces the TTS layer.
// Phase 27 — adds graph_* RPCs as tools so the assistant can answer
// "Has X stayed before?", "Which rooms have recurring complaints?",
// "Which TAs bring the most A-listers?", etc.
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

  // Phase 26.5 — conversation memory. The frontend keeps the last N
  // turns client-side and passes them as `history` on each new question.
  // Cap at last 10 entries (5 user + 5 assistant) to keep the input
  // token count bounded; older turns drop off naturally.
  const rawHistory = Array.isArray(body?.history) ? body.history : [];
  const history: Array<{ role: "user" | "assistant"; content: string }> = rawHistory
    .filter((t: any) => t && (t.role === "user" || t.role === "assistant") && typeof t.content === "string")
    .slice(-10)
    .map((t: any) => ({ role: t.role, content: t.content.toString().slice(0, 5000) }));

  // conversation_id ties multiple turns to one drawer session. Optional —
  // frontend may or may not pass it.
  const conversationId: string | null =
    typeof body?.conversation_id === "string" && body.conversation_id.length === 36
      ? body.conversation_id
      : null;

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
    const tools = graphTools(userRole);

    // Phase 27 — tool-use loop. Cap at 4 rounds total (most questions
    // resolve in 0-1 tool calls; 4 is a safety net so a runaway plan
    // can't burn the budget).
    const messages: any[] = [
      ...history,
      { role: "user", content: question },
    ];

    let toolRounds = 0;
    const maxRounds = 4;
    let res: any;
    while (true) {
      res = await anthropic.messages.create({
        model: MODEL,
        max_tokens: 600,
        tools,
        system: [
          { type: "text", text: sys, cache_control: { type: "ephemeral" } },
          {
            type: "text",
            text: `\n\n=== Today's data ===\n${contextJson}\n=== End of data ===`,
            cache_control: { type: "ephemeral" },
          },
        ],
        messages,
      });

      // Aggregate token usage across all rounds.
      promptTokens     += res.usage?.input_tokens ?? 0;
      completionTokens += res.usage?.output_tokens ?? 0;
      cacheReadTokens  += (res.usage as any)?.cache_read_input_tokens ?? 0;

      const toolUses = (res.content ?? []).filter((b: any) => b.type === "tool_use");
      if (toolUses.length === 0 || res.stop_reason !== "tool_use" || toolRounds >= maxRounds) {
        break;
      }

      // Execute each tool call against Postgres and feed results back.
      messages.push({ role: "assistant", content: res.content });
      const toolResults: any[] = [];
      for (const tu of toolUses) {
        const result = await runGraphTool(supa, tu.name, tu.input ?? {});
        toolResults.push({
          type: "tool_result",
          tool_use_id: tu.id,
          content: JSON.stringify(result).slice(0, 30000),
        });
      }
      messages.push({ role: "user", content: toolResults });
      toolRounds += 1;
    }

    answer = (res.content ?? [])
      .filter((b: any) => b.type === "text")
      .map((b: any) => b.text)
      .join("")
      .trim();

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
      conversation_id: conversationId,
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

  return `You are the Daios Cove assistant for ${o.userName} (role: ${o.userRole}). Today (Athens): ${o.athensToday}. Tonight's flash report covers: ${o.reportDate}.

═══════════════════════════════════════════════════════════════
TOOL ROUTING — READ FIRST, BEFORE ANYTHING ELSE
═══════════════════════════════════════════════════════════════

The data block at the bottom of this prompt covers ONLY TONIGHT'S report.
For ANY question about counts, comparisons, patterns, history, or "the most/best/recurring [X]" across rooms, agents, or guests — you MUST call a tool. The data block does not contain that information; the tools do.

If the user's question contains ANY of these words/phrases, the answer is NOT in tonight's data — call the matching tool:
- "most" / "fewest" / "best" / "worst" / "top" / "ranking" — comparison
- "recurring" / "repeat" / "again" / "history" / "ever" / "before" / "last [N] days" / "previous" — historical
- "patterns" / "trends" / "across" / "all rooms" / "all agents" — aggregate

Hard mappings (call the tool, do NOT answer from the data block):
- "which rooms have the most complaints" / "recurring complaints in rooms" / "rooms with problems" → recurring_complaint_rooms (default 30 days)
- "has room [X] had problems" / "complaints in room [X]" → room_complaints
- "has [name] stayed before" / "is [name] a returning guest" / "[name]'s history" → guest_history
- "which [travel agent / TA] brings the most A-listers" / "best agents for VIPs" → top_alister_tas
- "how is [TA name] performing" / "stats for [TA]" → ta_overview
- "returning A-listers" / "loyal VIPs" / "A-listers who came back" → alister_returners

Examples of CORRECT routing:
- Q: "which rooms have the most complaints" → call recurring_complaint_rooms({min_count: 2, days: 30}). Tonight's pending list is irrelevant — they want the 30-day pattern.
- Q: "any pending complaints right now" / "what complaints came in tonight" → DO NOT call a tool. Answer from the data block.
- Q: "which travel agent brings the most A-listers" → call top_alister_tas({min_stays: 2}). Tonight's arrivals don't tell you the answer.

NEVER decline a historical question with "I don't have that information for tonight" — that's wrong, the tools have it. Call the tool.

═══════════════════════════════════════════════════════════════

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
- Follow-ups: when the user has just asked a question and asks a related follow-up ("And tomorrow?", "Tell me more about that room", "What's the room number?"), use the prior turn for context. Don't make the user repeat themselves.
- Don't recommend, don't add commentary. Just the facts. (Exception: if the user explicitly asks "what should we do".)

Privacy and PII:
- The user's role determines what you've been given. Only mention what's in the data.
- If a guest is anonymous in any source, do not reveal identity even if you somehow have it.
- Never read out a confirmation number, email, phone, or full payment data.

Language:
- ${langInstr}

Topics you SHOULD answer (these are operational, even if they sound general):
- Weather (today, tomorrow, the 3-day forecast — all in the data)
- Occupancy, arrivals, departures, room counts, specific rooms
- A-listers and notable guests
- Allergies, dietary preferences, medical notes
- Pending or solved complaints
- Today's activities (boat trips, excursions)
- FAM trips, site inspections, group bookings
- Birthdays in house
- Daily briefing content
- Booking.com rating, channels, partner arrivals
- Ideas / opinions / feedback queue
- Anything that appears in the data block above
(Cross-cutting / historical questions are handled by the TOOL ROUTING block at the top of this prompt — not from the data block.)

Topics you should DECLINE (these are outside resort ops):
- General knowledge ("when did Crete become Greek?")
- News, sports scores, stock prices, celebrity gossip not tied to a guest
- Long-range weather forecasts beyond the 3 days in the data
- Personal advice not related to a guest interaction
- Programming or technical questions

If declining, be brief: "I'm focused on today at the Cove" — once, no preamble.`;
}


// ─── Phase 27 — graph tools ──────────────────────────────────────────────
// Anthropic tool definitions backed by graph_* RPCs. Role-gated so e.g.
// general staff can't surface A-lister loyalty lists.

function graphTools(userRole: string): any[] {
  const seeAlister = ["admin", "management", "guest_relations"].includes(userRole);
  const seeService = canSeeServiceNotes(userRole);

  const tools: any[] = [];

  // Anyone in service can ask about guest history (no PII beyond what
  // the role can already see in zoho_notes and reservations).
  if (seeService) {
    tools.push({
      name: "guest_history",
      description:
        "Look up past stays for a guest by name. Use this when the user asks 'has X stayed before?', 'when did X last visit?', 'is X a returning guest?'. Returns up to 5 matching guests with stay counts, first/last stay dates, channels, and travel agents.",
      input_schema: {
        type: "object",
        properties: {
          name: { type: "string", description: "Guest name (full or partial)" },
          country: { type: "string", description: "Optional country filter to disambiguate common names" },
        },
        required: ["name"],
      },
    });

    tools.push({
      name: "room_complaints",
      description:
        "Get complaint history for a specific room. Use when the user asks 'has room X had problems?', 'how many complaints for room X?', 'what's wrong with room X?'. Returns count, distinct guest count, average resolution time, and the 10 most recent complaints.",
      input_schema: {
        type: "object",
        properties: {
          room: { type: "string", description: "Room number, e.g. '537'" },
          days: { type: "integer", description: "Lookback window in days, default 90", default: 90 },
        },
        required: ["room"],
      },
    });

    tools.push({
      name: "ta_overview",
      description:
        "Get travel agent quality metrics. Use when the user asks 'how is travel agent X performing?', 'how many bookings did TA X bring?', 'what's the A-lister rate from X?'. Returns stays, A-lister stays, allergy stays, VIP stays, and average length of stay.",
      input_schema: {
        type: "object",
        properties: {
          name: { type: "string", description: "Travel agent name (full or partial)" },
          days: { type: "integer", description: "Lookback window in days, default 90", default: 90 },
        },
        required: ["name"],
      },
    });

    tools.push({
      name: "recurring_complaint_rooms",
      description:
        "List rooms with recurring complaints across the recent window. Use when the user asks 'which rooms have most complaints?', 'are there rooms we should check?', 'recurring problems'. Returns rooms with at least min_count complaints in the window.",
      input_schema: {
        type: "object",
        properties: {
          min_count: { type: "integer", description: "Minimum complaints to include, default 3", default: 3 },
          days: { type: "integer", description: "Window in days, default 30 (use 90 for the longer view)", default: 30 },
        },
      },
    });
  }

  // A-lister tools restricted to admin/management/guest_relations.
  if (seeAlister) {
    tools.push({
      name: "top_alister_tas",
      description:
        "List the travel agents bringing the most A-list (notable) guests. Use when the user asks 'which TAs bring most A-listers?', 'who are our best agents for VIPs?'. Returns TAs with at least min_stays bookings ranked by A-lister volume.",
      input_schema: {
        type: "object",
        properties: {
          min_stays: { type: "integer", description: "Minimum total stays to include, default 3", default: 3 },
        },
      },
    });

    tools.push({
      name: "alister_returners",
      description:
        "List A-list (notable) guests who have stayed multiple times. Use when the user asks 'which A-listers are returning?', 'who are our loyal VIPs?'. Returns guests with stay_count >= min_stays.",
      input_schema: {
        type: "object",
        properties: {
          min_stays: { type: "integer", description: "Minimum stay count to include, default 2", default: 2 },
        },
      },
    });
  }

  return tools;
}

async function runGraphTool(supa: any, name: string, input: Record<string, any>): Promise<any> {
  try {
    switch (name) {
      case "guest_history": {
        const { data, error } = await supa.rpc("graph_guest_history", {
          p_name: String(input.name ?? ""),
          p_country: input.country ? String(input.country) : null,
        });
        if (error) return { error: error.message };
        return data ?? [];
      }
      case "room_complaints": {
        const { data, error } = await supa.rpc("graph_room_complaints", {
          p_room: String(input.room ?? ""),
          p_days: Number(input.days ?? 90),
        });
        if (error) return { error: error.message };
        return data ?? {};
      }
      case "ta_overview": {
        const { data, error } = await supa.rpc("graph_ta_overview", {
          p_name: String(input.name ?? ""),
          p_days: Number(input.days ?? 90),
        });
        if (error) return { error: error.message };
        return data ?? [];
      }
      case "recurring_complaint_rooms": {
        const { data, error } = await supa.rpc("graph_recurring_complaint_rooms", {
          p_min_count: Number(input.min_count ?? 3),
          p_days: Number(input.days ?? 30),
        });
        if (error) return { error: error.message };
        return data ?? [];
      }
      case "top_alister_tas": {
        const { data, error } = await supa.rpc("graph_top_alister_tas", {
          p_min_stays: Number(input.min_stays ?? 3),
        });
        if (error) return { error: error.message };
        return data ?? [];
      }
      case "alister_returners": {
        const { data, error } = await supa.rpc("graph_alister_returners", {
          p_min_stays: Number(input.min_stays ?? 2),
        });
        if (error) return { error: error.message };
        return data ?? [];
      }
      default:
        return { error: `unknown tool: ${name}` };
    }
  } catch (e) {
    return { error: String((e as Error)?.message ?? e).slice(0, 500) };
  }
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
