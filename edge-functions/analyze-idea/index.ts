// analyze-idea — Lovable Cloud edge function.
//
// Fires when submit_idea RPC runs. Calls Lovable AI Gateway (Gemini 2.5 Pro)
// to:
//   * Categorise the submission (category + subcategory)
//   * Assess severity + sentiment
//   * Produce a 1-sentence summary
//   * Suggest 3-5 industry good practices (citing source by name)
//   * Suggest 3-5 concrete starting-point actions
//   * Identify semantically-similar prior ideas (from a shortlist we pass in)
//
// Uses Lovable AI Gateway for cost efficiency (~60-80× cheaper than Opus).
// Gemini 2.5 Pro via the gateway handles structured output + source-cited
// good practices well; source_url is model-knowledge-based (no live search).
//
// Then:
//   * Updates the ideas row with the AI analysis
//   * Emails the Executive Committee (committee@daioscove.com by default)
//   * Emails the submitter a confirmation with the AI summary + ticket ID
//
// Contract:
//   POST /functions/v1/analyze-idea
//   Headers:  Authorization: Bearer <PIPELINE_SECRET>
//   Body:     { "idea_id": "uuid" }

import { createClient } from "jsr:@supabase/supabase-js@2";

const MODEL = Deno.env.get("IDEAS_MODEL") ?? "google/gemini-2.5-pro";
const LOVABLE_AI_URL = "https://ai.gateway.lovable.dev/v1/chat/completions";

Deno.serve(async (req) => {
  if (req.method !== "POST") return json({ error: "POST only" }, 405);

  const secret = Deno.env.get("PIPELINE_SECRET");
  const auth = req.headers.get("authorization") ?? "";
  if (!secret || auth !== `Bearer ${secret}`) return json({ error: "unauthorized" }, 401);

  const lovableKey   = Deno.env.get("LOVABLE_API_KEY");
  const resendKey    = Deno.env.get("RESEND_API_KEY");
  const fromAddress  = Deno.env.get("EMAIL_FROM") ?? "Daios Cove Flash <flash@daioshotels.com>";
  const dashboardUrl = Deno.env.get("DASHBOARD_URL") ?? "https://flashreport.daioscove.com";
  const supabaseUrl  = Deno.env.get("SUPABASE_URL");
  const serviceKey   = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY");
  if (!lovableKey)   return json({ error: "LOVABLE_API_KEY not set" }, 500);
  if (!resendKey)    return json({ error: "RESEND_API_KEY not set" }, 500);
  if (!supabaseUrl || !serviceKey) return json({ error: "Supabase env missing" }, 500);

  const body = await req.json().catch(() => ({}));
  const id: string | undefined = body?.idea_id;
  if (!id) return json({ error: "idea_id required" }, 400);

  const supa = createClient(supabaseUrl, serviceKey);

  // ─── Fetch the idea ──────────────────────────────────────────────────────
  const { data: idea, error: iErr } = await supa
    .from("ideas").select("*").eq("id", id).maybeSingle();
  if (iErr)  return json({ error: "lookup failed: " + iErr.message }, 500);
  if (!idea) return json({ error: "idea not found" }, 404);

  // ─── Shortlist candidate "similar" ideas from the last 90 days ───────────
  // Same category_hint OR keyword overlap on a cheap tsvector-style match.
  // Keep it small (<= 10) so the prompt stays compact.
  const ninetyDaysAgo = new Date(Date.now() - 90 * 86400 * 1000).toISOString();
  const { data: candidates } = await supa
    .from("ideas")
    .select("id, subject, category, ai_summary, created_at")
    .neq("id", id)
    .gte("created_at", ninetyDaysAgo)
    .order("created_at", { ascending: false })
    .limit(25);

  const shortlist = pickShortlist(idea, candidates ?? []);

  // ─── Read the committee email (admin-editable via app_settings) ──────────
  const { data: cfg } = await supa
    .from("app_settings").select("value").eq("key", "ideas_committee_email").maybeSingle();
  const committeeEmail = ((cfg?.value as string) ?? "committee@daioscove.com").trim();

  // ─── Ask Lovable AI Gateway (Gemini 2.5 Pro) ─────────────────────────────
  let ai: AIResult | null = null;
  let aiErr: string | null = null;
  try {
    ai = await runLovableAI({
      apiKey: lovableKey,
      idea: idea as IdeaRow,
      shortlist,
    });
  } catch (e) {
    aiErr = String((e as Error)?.message ?? e).slice(0, 500);
    console.error("AI analysis failed:", aiErr);
  }

  // ─── Persist AI fields (even on failure — record that we tried) ──────────
  const update: Record<string, unknown> = {
    ai_analyzed_at: new Date().toISOString(),
    ai_model: MODEL,
  };
  if (ai) {
    update.category           = ai.category;
    update.subcategory        = ai.subcategory;
    update.severity           = ai.severity;
    update.sentiment          = ai.sentiment;
    update.ai_summary         = ai.summary;
    update.ai_good_practices  = ai.good_practices;
    update.ai_starting_points = ai.starting_points;
    update.ai_similar_idea_ids = ai.similar_idea_ids ?? [];
    update.ai_evidence_urls   = ai.evidence_urls ?? [];
    // Auto-advance status to 'triaged' once AI analysis lands
    update.status = "triaged";
  }
  const { error: upErr } = await supa.from("ideas").update(update).eq("id", id);
  if (upErr) console.error("ideas update failed:", upErr.message);

  // ─── Emails (best-effort) ────────────────────────────────────────────────
  const dashboardLink = `${dashboardUrl}/ideas/${id}`;
  let committeeMsgId: string | null = null;
  let submitterMsgId: string | null = null;

  if (committeeEmail) {
    try {
      committeeMsgId = await sendViaResend({
        apiKey: resendKey,
        from: fromAddress,
        to: committeeEmail,
        subject: `[IDEAS] ${ai?.severity?.toUpperCase() ?? "NEW"} — ${idea.subject}`,
        html: renderCommitteeEmail(idea as IdeaRow, ai, dashboardLink),
      });
    } catch (e) {
      console.error("committee email failed:", String((e as Error)?.message ?? e));
    }
  }

  if (!idea.is_anonymous && idea.submitter_email) {
    try {
      submitterMsgId = await sendViaResend({
        apiKey: resendKey,
        from: fromAddress,
        to: idea.submitter_email,
        subject: `Thanks — your submission has been received`,
        html: renderSubmitterEmail(idea as IdeaRow, ai, dashboardLink),
      });
    } catch (e) {
      console.error("submitter confirmation failed:", String((e as Error)?.message ?? e));
    }
  }

  return json({
    ok: !!ai,
    idea_id: id,
    ai_error: aiErr,
    committee_email: committeeEmail,
    committee_message_id: committeeMsgId,
    submitter_message_id: submitterMsgId,
    ai_summary: ai?.summary ?? null,
  });
});

// ─── Types ──────────────────────────────────────────────────────────────────

interface IdeaRow {
  id: string;
  subject: string;
  body: string;
  category_hint: string | null;
  is_anonymous: boolean;
  submitter_name: string | null;
  submitter_role: string | null;
  submitter_email: string | null;
  created_at: string;
}

interface AIResult {
  category: string;
  subcategory: string | null;
  severity: string;
  sentiment: string;
  summary: string;
  good_practices: Array<{ title: string; detail: string; source_url?: string }>;
  starting_points: Array<{ title: string; detail: string }>;
  similar_idea_ids: string[];
  evidence_urls: string[];
}

// ─── Shortlist heuristic (keyword overlap + same category_hint) ─────────────

function pickShortlist(
  idea: IdeaRow,
  candidates: { id: string; subject: string; category: string | null; ai_summary: string | null }[],
): { id: string; subject: string; category: string | null; ai_summary: string | null }[] {
  const tokens = tokenize(`${idea.subject} ${idea.body}`);
  const scored = candidates.map((c) => {
    const cTokens = tokenize(`${c.subject} ${c.ai_summary ?? ""}`);
    const overlap = tokens.reduce((n, t) => n + (cTokens.includes(t) ? 1 : 0), 0);
    const categoryBonus = (idea.category_hint && c.category === idea.category_hint) ? 3 : 0;
    return { c, score: overlap + categoryBonus };
  });
  return scored
    .filter((s) => s.score > 0)
    .sort((a, b) => b.score - a.score)
    .slice(0, 5)
    .map((s) => s.c);
}

function tokenize(s: string): string[] {
  return s
    .toLowerCase()
    .replace(/[^a-z0-9\s]+/g, " ")
    .split(/\s+/)
    .filter((w) => w.length >= 4)
    .slice(0, 60);
}

// ─── Claude call with web_search ────────────────────────────────────────────

async function runLovableAI(opts: {
  apiKey: string;
  idea: IdeaRow;
  shortlist: { id: string; subject: string; category: string | null; ai_summary: string | null }[];
}): Promise<AIResult> {
  const userPrompt = buildUserPrompt(opts.idea, opts.shortlist);

  const resp = await fetch(LOVABLE_AI_URL, {
    method: "POST",
    headers: {
      "Authorization": `Bearer ${opts.apiKey}`,
      "Content-Type": "application/json",
    },
    body: JSON.stringify({
      model: MODEL,
      messages: [
        { role: "system", content: SYSTEM_PROMPT },
        { role: "user",   content: userPrompt },
      ],
      // Force structured JSON output. Gemini via gateway honours this.
      response_format: { type: "json_object" },
      temperature: 0.3,
      max_tokens: 3000,
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

  return normalizeResult(parsed);
}

// ─── Prompts ────────────────────────────────────────────────────────────────

const SYSTEM_PROMPT = `You are the analyst for the Ideas & Opinions inbox of Daios Cove, a 5-star resort on Crete. Employees submit ideas, opinions, and issues directly to the Executive Committee. Your job is to turn each raw submission into a crisp, actionable triage card.

For each submission, produce structured JSON with:

1. category — exactly one of:
   - guest_experience, operations, staff_hr, cost_revenue, safety_compliance, technology, sustainability, other

2. subcategory — free text, more specific area (department, outlet, system). Examples:
   "Housekeeping", "F&B — Ocean restaurant", "Front office — check-in", "Engineering — HVAC",
   "Spa (Kepos)", "HR — training", "IT — Opera PMS", "Sustainability — water".

3. severity:
   - low — minor improvement, no urgency
   - medium — meaningful issue, should be addressed this quarter
   - high — recurring problem or significant opportunity; address within weeks
   - critical — guest-facing risk, safety, compliance, revenue leak; address within days

4. sentiment:
   - positive — compliment or enthusiastic suggestion
   - constructive — neutral problem-solving tone
   - frustrated — employee is annoyed / feels unheard
   - urgent — strong language, immediate action implied

5. summary — ONE sentence (<= 30 words) capturing the core of the submission.

6. good_practices — 3 to 5 industry best practices relevant to this submission, drawn from your knowledge of the hospitality / operations literature.
   Each item: { "title": "short bold phrase", "detail": "one sentence explaining the practice", "source_url": null }.
   When you know a specific authoritative source by name, include it in the detail text (e.g. "Cornell Hospitality research shows…", "per the AHLA 2024 benchmark…"). Leave source_url as null — do not fabricate URLs.
   Reference where appropriate: Cornell Hospitality, Hotel News Now, Skift, HVS, EHL Hospitality Insights, AHLA, Deloitte Hospitality, STR, McKinsey Travel & Hospitality.

7. starting_points — 3 to 5 concrete first actions THIS specific hotel could take.
   Each item: { "title": "imperative verb phrase", "detail": "one sentence explaining what / who / where to start" }.
   Be specific: mention likely owners (Guest Relations, F&B Director, Engineering, HR, etc.) when obvious from context.

8. similar_idea_ids — from the shortlist of prior ideas provided in the user prompt, return the UUIDs of any that are genuinely on the same theme. Empty array if none match.

9. evidence_urls — leave as []. Live web search is not available in this mode.

Rules:
- Never fabricate URLs. Only cite source names in detail text; leave source_url as null.
- If the submission is too vague to analyse, set severity="low", sentiment="constructive", and populate good_practices with generic advice.
- Output MUST be a single valid JSON object with exactly these keys. No preamble, no code fences, no trailing prose.

JSON schema:
{
  "category": string,
  "subcategory": string | null,
  "severity": "low" | "medium" | "high" | "critical",
  "sentiment": "positive" | "constructive" | "frustrated" | "urgent",
  "summary": string,
  "good_practices": [{"title": string, "detail": string, "source_url": null}],
  "starting_points": [{"title": string, "detail": string}],
  "similar_idea_ids": string[],
  "evidence_urls": []
}`;

function buildUserPrompt(
  idea: IdeaRow,
  shortlist: { id: string; subject: string; category: string | null; ai_summary: string | null }[],
): string {
  const parts: string[] = [];
  parts.push("SUBMISSION");
  parts.push(`Subject: ${idea.subject}`);
  parts.push(`Body:\n${idea.body}`);
  if (idea.category_hint) parts.push(`Submitter's category guess: ${idea.category_hint}`);
  parts.push(`Submitter role: ${idea.submitter_role ?? "unknown"}`);
  parts.push(`Anonymous: ${idea.is_anonymous ? "yes" : "no"}`);
  parts.push("");
  if (shortlist.length > 0) {
    parts.push(`CANDIDATE SIMILAR IDEAS FROM LAST 90 DAYS (id | subject | category | summary):`);
    for (const c of shortlist) {
      parts.push(`- ${c.id} | ${c.subject} | ${c.category ?? "-"} | ${c.ai_summary ?? "-"}`);
    }
    parts.push("Only include IDs from this list in similar_idea_ids if they are truly on the same theme.");
  } else {
    parts.push("No candidate prior ideas available. similar_idea_ids = [].");
  }
  parts.push("");
  parts.push("Return the JSON finding.");
  return parts.join("\n");
}

// ─── JSON extraction & normalisation ────────────────────────────────────────

function parseJsonObject(text: string): any | null {
  let t = text.trim();
  t = t.replace(/^```(?:json)?\s*\n?/, "").replace(/\s*```\s*$/, "");
  for (let i = 0; i < t.length; i++) {
    if (t[i] !== "{") continue;
    // Find the matching closing brace by counting depth, respecting strings.
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

const CATEGORIES = new Set([
  "guest_experience", "operations", "staff_hr", "cost_revenue",
  "safety_compliance", "technology", "sustainability", "other",
]);
const SEVERITIES = new Set(["low", "medium", "high", "critical"]);
const SENTIMENTS = new Set(["positive", "constructive", "frustrated", "urgent"]);

function normalizeResult(raw: any): AIResult {
  const cat = String(raw.category ?? "").toLowerCase();
  const sev = String(raw.severity ?? "").toLowerCase();
  const sen = String(raw.sentiment ?? "").toLowerCase();
  const gp = Array.isArray(raw.good_practices) ? raw.good_practices : [];
  const sp = Array.isArray(raw.starting_points) ? raw.starting_points : [];
  const sim = Array.isArray(raw.similar_idea_ids) ? raw.similar_idea_ids : [];
  const ev = Array.isArray(raw.evidence_urls) ? raw.evidence_urls : [];
  return {
    category: CATEGORIES.has(cat) ? cat : "other",
    subcategory: raw.subcategory ? String(raw.subcategory).slice(0, 120) : null,
    severity: SEVERITIES.has(sev) ? sev : "low",
    sentiment: SENTIMENTS.has(sen) ? sen : "constructive",
    summary: String(raw.summary ?? "").slice(0, 500),
    good_practices: gp.slice(0, 5).map((x: any) => ({
      title: String(x?.title ?? "").slice(0, 120),
      detail: String(x?.detail ?? "").slice(0, 500),
      source_url: x?.source_url ? String(x.source_url).slice(0, 500) : undefined,
    })),
    starting_points: sp.slice(0, 5).map((x: any) => ({
      title: String(x?.title ?? "").slice(0, 120),
      detail: String(x?.detail ?? "").slice(0, 500),
    })),
    similar_idea_ids: sim.filter((x: any) => typeof x === "string" && /^[0-9a-f-]{36}$/i.test(x)).slice(0, 10),
    evidence_urls: ev.filter((x: any) => typeof x === "string" && x.startsWith("http")).slice(0, 5),
  };
}

// ─── Email helpers ──────────────────────────────────────────────────────────

async function sendViaResend(opts: {
  apiKey: string; from: string; to: string; subject: string; html: string;
}): Promise<string> {
  const resp = await fetch("https://api.resend.com/emails", {
    method: "POST",
    headers: { "Authorization": `Bearer ${opts.apiKey}`, "Content-Type": "application/json" },
    body: JSON.stringify({ from: opts.from, to: [opts.to], subject: opts.subject, html: opts.html }),
  });
  const text = await resp.text();
  if (!resp.ok) throw new Error(`Resend ${resp.status}: ${text}`);
  return JSON.parse(text).id ?? "";
}

function esc(s: unknown): string {
  return String(s ?? "")
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
}

function sevColor(sev: string | undefined): string {
  switch (sev) {
    case "critical": return "#dc2626";
    case "high":     return "#ea580c";
    case "medium":   return "#ca8a04";
    case "low":
    default:         return "#16a34a";
  }
}

function renderCommitteeEmail(idea: IdeaRow, ai: AIResult | null, link: string): string {
  const sev = ai?.severity ?? "low";
  const sen = ai?.sentiment ?? "constructive";
  const gp  = ai?.good_practices ?? [];
  const sp  = ai?.starting_points ?? [];
  const submitter = idea.is_anonymous
    ? "Anonymous"
    : `${esc(idea.submitter_name ?? "Unknown")}${idea.submitter_role ? ` · ${esc(idea.submitter_role)}` : ""}`;

  const gpHtml = gp.map((g) => {
    const url = g.source_url ? ` <a href="${esc(g.source_url)}" style="color:#a38a6a">[source]</a>` : "";
    return `<li style="margin-bottom:8px"><strong>${esc(g.title)}</strong> — ${esc(g.detail)}${url}</li>`;
  }).join("");

  const spHtml = sp.map((s) =>
    `<li style="margin-bottom:8px"><strong>${esc(s.title)}</strong> — ${esc(s.detail)}</li>`
  ).join("");

  const aiBlock = ai ? `
    <table style="width:100%;border-collapse:collapse;margin-top:20px">
      ${row("Category",   `${esc(ai.category)}${ai.subcategory ? ` · ${esc(ai.subcategory)}` : ""}`)}
      ${row("Severity",   `<span style="display:inline-block;padding:2px 8px;border-radius:3px;background:${sevColor(sev)};color:#fff;font-size:11px;letter-spacing:1px;text-transform:uppercase">${esc(sev)}</span>`, true)}
      ${row("Sentiment",  esc(sen))}
      ${row("AI summary", esc(ai.summary))}
    </table>

    ${gp.length ? `<div style="margin-top:20px">
      <div style="font-size:11px;color:#888;text-transform:uppercase;letter-spacing:1px;font-weight:600;margin-bottom:8px">Good practices</div>
      <ul style="margin:0;padding-left:20px;font-size:13px;line-height:1.5;color:#333">${gpHtml}</ul>
    </div>` : ""}

    ${sp.length ? `<div style="margin-top:20px">
      <div style="font-size:11px;color:#888;text-transform:uppercase;letter-spacing:1px;font-weight:600;margin-bottom:8px">Where to start</div>
      <ul style="margin:0;padding-left:20px;font-size:13px;line-height:1.5;color:#333">${spHtml}</ul>
    </div>` : ""}
  ` : `<p style="margin-top:20px;font-size:13px;color:#666;font-style:italic">AI analysis was not available — review the raw submission and triage manually.</p>`;

  return `<!doctype html><html lang="en"><head><meta charset="utf-8"></head>
<body style="margin:0;padding:0;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Helvetica,Arial,sans-serif;background:#f5f5f0;color:#1a1a1a">
<div style="max-width:680px;margin:0 auto;background:#ffffff">
  <div style="padding:24px 28px;border-bottom:3px solid ${sevColor(sev)}">
    <div style="font-size:11px;letter-spacing:2px;color:#a38a6a;text-transform:uppercase;font-weight:600">Daios Cove · Ideas & Opinions</div>
    <div style="font-size:20px;font-weight:600;margin-top:4px">${esc(idea.subject)}</div>
    <div style="font-size:12px;color:#666;margin-top:4px">From ${submitter}</div>
  </div>
  <div style="padding:20px 28px 28px">
    <div style="font-size:11px;color:#888;text-transform:uppercase;letter-spacing:1px;font-weight:600;margin-bottom:8px">Submission</div>
    <div style="font-size:13px;line-height:1.6;white-space:pre-wrap;color:#1a1a1a">${esc(idea.body)}</div>

    ${aiBlock}

    <div style="margin-top:28px;text-align:center">
      <a href="${esc(link)}" style="display:inline-block;padding:10px 22px;background:#a38a6a;color:#fff;text-decoration:none;border-radius:4px;font-size:13px;font-weight:600">Open in dashboard</a>
    </div>
  </div>
</div></body></html>`;
}

function renderSubmitterEmail(idea: IdeaRow, ai: AIResult | null, link: string): string {
  const short = idea.id.slice(0, 8);
  const summaryLine = ai?.summary
    ? `<p style="font-size:13px;line-height:1.6;color:#333"><em>${esc(ai.summary)}</em></p>`
    : "";
  return `<!doctype html><html lang="en"><head><meta charset="utf-8"></head>
<body style="margin:0;padding:40px 20px;font-family:-apple-system,BlinkMacSystemFont,sans-serif;background:#f5f5f0;color:#1a1a1a">
<div style="max-width:540px;margin:0 auto;background:#ffffff;border-radius:8px;padding:28px">
  <div style="font-size:11px;letter-spacing:2px;color:#a38a6a;text-transform:uppercase;font-weight:600">Daios Cove</div>
  <h2 style="margin:4px 0 16px;font-size:20px;font-weight:600">Thanks — we received your submission</h2>
  <p style="font-size:13px;line-height:1.6;color:#333">
    Your idea has been logged and routed to the Executive Committee. Reference:
    <strong>#${short}</strong>.
  </p>
  <div style="margin:16px 0;padding:14px;background:#fafaf6;border-radius:6px">
    <div style="font-size:11px;color:#888;text-transform:uppercase;letter-spacing:1px;font-weight:600">Subject</div>
    <div style="font-size:14px;font-weight:600;margin-top:2px">${esc(idea.subject)}</div>
  </div>
  ${summaryLine}
  <p style="font-size:13px;line-height:1.6;color:#333;margin-top:16px">
    You can track the status and read the committee's response any time:
  </p>
  <p style="margin:16px 0;text-align:center">
    <a href="${esc(link)}" style="display:inline-block;padding:10px 22px;background:#a38a6a;color:#fff;text-decoration:none;border-radius:4px;font-size:13px">View submission</a>
  </p>
  <p style="font-size:11px;color:#888;margin-top:24px">You're receiving this because you submitted an idea via the Daios Cove dashboard.</p>
</div>
</body></html>`;
}

function row(label: string, value: string, isHtml = false): string {
  return `<tr>
    <td style="padding:6px 10px;border-bottom:1px solid #e8e6dc;color:#888;font-size:11px;width:140px;vertical-align:top;text-transform:uppercase;letter-spacing:1px;font-weight:600">${esc(label)}</td>
    <td style="padding:6px 10px;border-bottom:1px solid #e8e6dc;font-size:13px;color:#1a1a1a">${isHtml ? value : esc(value)}</td>
  </tr>`;
}

function json(p: unknown, s = 200) {
  return new Response(JSON.stringify(p), { status: s, headers: { "content-type": "application/json" } });
}
