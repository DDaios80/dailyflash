// send-idea-reminders — Lovable Cloud edge function.
//
// Invoked hourly by the idea-reminder-trigger pg_cron job. For every open
// idea, checks SLA state and fires the appropriate email(s). Idempotency
// is enforced via the idea_reminder_fires table — each reminder type fires
// at most once per idea per day.
//
// Email types dispatched:
//   critical_alert — severity=critical with overdue ack → chair
//   owner_nudge    — response overdue with assigned owner → owner
//   chair_nudge    — response overdue with no owner → chair
//   escalation     — SLA * 1.5 elapsed → chair (with escalation banner)
//   owner_digest   — daily 08:00 Athens → each owner with their open items
//   committee_digest — Mondays 08:00 Athens → chair with all open items
//
// Contract:
//   POST /functions/v1/send-idea-reminders
//   Headers:  Authorization: Bearer <PIPELINE_SECRET>
//   Body:     { athens_slot: "HH:MM", athens_date: "YYYY-MM-DD", athens_dow: "Mon"|"Tue"|... }

import { createClient } from "jsr:@supabase/supabase-js@2";

Deno.serve(async (req) => {
  if (req.method !== "POST") return json({ error: "POST only" }, 405);

  const secret = Deno.env.get("PIPELINE_SECRET");
  const auth = req.headers.get("authorization") ?? "";
  if (!secret || auth !== `Bearer ${secret}`) return json({ error: "unauthorized" }, 401);

  const supabaseUrl = Deno.env.get("SUPABASE_URL");
  const serviceKey  = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY");
  const resendKey   = Deno.env.get("RESEND_API_KEY");
  const fromAddress = Deno.env.get("EMAIL_FROM") ?? "Daios Cove Flash <flash@daioshotels.com>";
  const dashboardUrl = Deno.env.get("DASHBOARD_URL") ?? "https://flashreport.daioscove.com";
  if (!supabaseUrl || !serviceKey) return json({ error: "Supabase env missing" }, 500);
  if (!resendKey) return json({ error: "RESEND_API_KEY not set" }, 500);

  const body = await req.json().catch(() => ({}));
  const athensSlot: string = body?.athens_slot ?? "";
  const athensDow:  string = body?.athens_dow  ?? "";

  const supa = createClient(supabaseUrl, serviceKey);

  // Load chair info
  const chair = await loadChair(supa);
  if (!chair) return json({ error: "committee chair not configured" }, 500);

  // Load stale ideas via RPC
  const { data: ideas, error: iErr } = await supa.rpc("find_stale_ideas");
  if (iErr) return json({ error: "find_stale_ideas failed: " + iErr.message }, 500);
  const stale = (ideas ?? []) as StaleIdea[];

  const summary: Record<string, number> = {
    critical_alert: 0, owner_nudge: 0, chair_nudge: 0, escalation: 0,
    committee_digest: 0, owner_digest: 0,
    skipped_idempotent: 0, send_errors: 0,
  };

  // ─── 1. Per-idea reminders (run every hour) ──────────────────────────────
  for (const idea of stale) {
    const reminderType = classifyReminder(idea);
    if (!reminderType) continue;

    // Idempotency check
    const already = await wasFiredToday(supa, idea.idea_id, reminderType);
    if (already) { summary.skipped_idempotent++; continue; }

    // Pick recipient
    let recipientEmail: string | null = null;
    let recipientUserId: string | null = null;

    if (reminderType === "owner_nudge") {
      if (!idea.assigned_to_user_id) continue; // no owner, falls through to chair_nudge separately
      const { data: u } = await supa.auth.admin.getUserById(idea.assigned_to_user_id);
      recipientEmail = u?.user?.email ?? null;
      recipientUserId = idea.assigned_to_user_id;
    } else {
      recipientEmail = chair.email;
      recipientUserId = chair.user_id;
    }
    if (!recipientEmail) continue;

    const { subject, html } = renderReminderEmail(reminderType, idea, dashboardUrl);
    await sendAndLog({
      supa, resendKey, fromAddress,
      ideaId: idea.idea_id,
      reminderType,
      recipientEmail, recipientUserId,
      subject, html,
      counts: summary,
    });
  }

  // ─── 2. Digests (gated to 08:00 Athens) ─────────────────────────────────
  const isDigestHour = athensSlot.startsWith("08:");
  const isMonday = athensDow === "Mon";

  if (isDigestHour && stale.length > 0) {
    // Per-owner digest
    const byOwner = new Map<string, StaleIdea[]>();
    for (const i of stale) {
      if (i.assigned_to_user_id) {
        const arr = byOwner.get(i.assigned_to_user_id) ?? [];
        arr.push(i);
        byOwner.set(i.assigned_to_user_id, arr);
      }
    }

    for (const [ownerId, ownerIdeas] of byOwner.entries()) {
      const already = await wasDigestFiredToday(supa, ownerId, "owner_digest");
      if (already) { summary.skipped_idempotent++; continue; }

      const { data: u } = await supa.auth.admin.getUserById(ownerId);
      const email = u?.user?.email;
      if (!email) continue;

      const { subject, html } = renderDigestEmail("owner", ownerIdeas, dashboardUrl);
      await sendAndLogDigest({
        supa, resendKey, fromAddress,
        reminderType: "owner_digest",
        recipientEmail: email, recipientUserId: ownerId,
        subject, html, counts: summary,
      });
    }

    // Committee digest (Mondays only)
    if (isMonday) {
      const already = await wasDigestFiredToday(supa, chair.user_id, "committee_digest");
      if (!already) {
        const { subject, html } = renderDigestEmail("committee", stale, dashboardUrl);
        await sendAndLogDigest({
          supa, resendKey, fromAddress,
          reminderType: "committee_digest",
          recipientEmail: chair.email, recipientUserId: chair.user_id,
          subject, html, counts: summary,
        });
      } else {
        summary.skipped_idempotent++;
      }
    }
  }

  return json({
    ok: true,
    athens_slot: athensSlot,
    athens_dow: athensDow,
    total_open_ideas: stale.length,
    ...summary,
  });
});

// ─── Types ──────────────────────────────────────────────────────────────────

interface StaleIdea {
  idea_id: string;
  subject: string;
  severity: string;
  status: string;
  created_at: string;
  committee_response: string | null;
  assigned_to_user_id: string | null;
  assigned_to_name: string | null;
  submitter_email: string | null;
  submitter_name: string | null;
  sla_state: "on_track" | "warning" | "overdue" | "escalated" | "closed";
  sla_bucket: "ack" | "respond" | null;
  hours_elapsed: number;
  deadline_hours: number;
}

interface Chair { user_id: string; email: string; name: string; }

// ─── Chair loading ──────────────────────────────────────────────────────────

async function loadChair(supa: any): Promise<Chair | null> {
  const { data: s } = await supa
    .from("app_settings").select("value")
    .eq("key", "committee_chair_user_id").maybeSingle();
  if (!s?.value) return null;
  const { data: u } = await supa.auth.admin.getUserById(s.value);
  if (!u?.user?.email) return null;
  return {
    user_id: s.value,
    email: u.user.email,
    name: (u.user.user_metadata?.full_name
      || u.user.user_metadata?.name
      || u.user.email) as string,
  };
}

// ─── Reminder classifier ───────────────────────────────────────────────────

type ReminderType =
  | "critical_alert" | "owner_nudge" | "chair_nudge" | "escalation";

function classifyReminder(i: StaleIdea): ReminderType | null {
  // escalation trumps everything else
  if (i.sla_state === "escalated") return "escalation";

  // critical severity with ack overdue = critical_alert
  if (i.severity === "critical" && i.sla_bucket === "ack" && i.sla_state === "overdue") {
    return "critical_alert";
  }

  // response bucket overdue → owner if assigned, else chair
  if (i.sla_bucket === "respond" && i.sla_state === "overdue") {
    return i.assigned_to_user_id ? "owner_nudge" : "chair_nudge";
  }

  // Also: submitted without ack past ack deadline, non-critical severities
  if (i.sla_bucket === "ack" && i.sla_state === "overdue") {
    return "chair_nudge";
  }

  return null;
}

// ─── Idempotency checks ────────────────────────────────────────────────────

async function wasFiredToday(supa: any, ideaId: string, reminderType: string): Promise<boolean> {
  const today = new Date().toISOString().slice(0, 10);
  const { data } = await supa
    .from("idea_reminder_fires").select("id")
    .eq("idea_id", ideaId).eq("reminder_type", reminderType).eq("fire_date", today)
    .maybeSingle();
  return !!data;
}

async function wasDigestFiredToday(supa: any, recipientUserId: string, reminderType: string): Promise<boolean> {
  const today = new Date().toISOString().slice(0, 10);
  const { data } = await supa
    .from("idea_reminder_fires").select("id")
    .is("idea_id", null)
    .eq("recipient_user_id", recipientUserId)
    .eq("reminder_type", reminderType)
    .eq("fire_date", today)
    .maybeSingle();
  return !!data;
}

// ─── Send + log helpers ────────────────────────────────────────────────────

async function sendAndLog(opts: {
  supa: any; resendKey: string; fromAddress: string;
  ideaId: string; reminderType: string;
  recipientEmail: string; recipientUserId: string | null;
  subject: string; html: string;
  counts: Record<string, number>;
}): Promise<void> {
  let messageId: string | null = null;
  let err: string | null = null;
  try {
    messageId = await sendViaResend({
      apiKey: opts.resendKey, from: opts.fromAddress, to: opts.recipientEmail,
      subject: opts.subject, html: opts.html,
    });
    opts.counts[opts.reminderType] = (opts.counts[opts.reminderType] ?? 0) + 1;
  } catch (e) {
    err = String((e as Error)?.message ?? e).slice(0, 500);
    opts.counts.send_errors++;
  }
  try {
    await opts.supa.from("idea_reminder_fires").insert({
      idea_id: opts.ideaId,
      recipient_user_id: opts.recipientUserId,
      recipient_email: opts.recipientEmail,
      reminder_type: opts.reminderType,
      status: err ? "failed" : "sent",
      resend_message_id: messageId,
      error: err,
    });
  } catch (_) { /* best-effort */ }
}

async function sendAndLogDigest(opts: {
  supa: any; resendKey: string; fromAddress: string;
  reminderType: "committee_digest" | "owner_digest";
  recipientEmail: string; recipientUserId: string;
  subject: string; html: string;
  counts: Record<string, number>;
}): Promise<void> {
  let messageId: string | null = null;
  let err: string | null = null;
  try {
    messageId = await sendViaResend({
      apiKey: opts.resendKey, from: opts.fromAddress, to: opts.recipientEmail,
      subject: opts.subject, html: opts.html,
    });
    opts.counts[opts.reminderType] = (opts.counts[opts.reminderType] ?? 0) + 1;
  } catch (e) {
    err = String((e as Error)?.message ?? e).slice(0, 500);
    opts.counts.send_errors++;
  }
  try {
    await opts.supa.from("idea_reminder_fires").insert({
      idea_id: null,
      recipient_user_id: opts.recipientUserId,
      recipient_email: opts.recipientEmail,
      reminder_type: opts.reminderType,
      status: err ? "failed" : "sent",
      resend_message_id: messageId,
      error: err,
    });
  } catch (_) { /* best-effort */ }
}

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

// ─── Email templates ───────────────────────────────────────────────────────

function esc(s: unknown): string {
  return String(s ?? "")
    .replace(/&/g, "&amp;").replace(/</g, "&lt;")
    .replace(/>/g, "&gt;").replace(/"/g, "&quot;");
}

function sevColor(sev: string): string {
  switch (sev) {
    case "critical": return "#dc2626";
    case "high":     return "#ea580c";
    case "medium":   return "#ca8a04";
    default:         return "#16a34a";
  }
}

function renderReminderEmail(
  type: ReminderType, idea: StaleIdea, dashboardUrl: string,
): { subject: string; html: string } {
  const link = `${dashboardUrl}/ideas/${idea.idea_id}`;
  const age = Math.round(idea.hours_elapsed);
  const sev = idea.severity;

  let kicker = "", heading = "", intro = "", subject = "";
  switch (type) {
    case "critical_alert":
      kicker = "Daios Cove · Ideas & Opinions";
      heading = "Critical submission — action needed";
      intro = `A <strong>critical</strong> severity idea has been open for ${age}h without acknowledgement. Please triage immediately.`;
      subject = `[CRITICAL] ${idea.subject}`;
      break;
    case "owner_nudge":
      kicker = "Daios Cove · Ideas & Opinions";
      heading = "Response overdue — you are the assigned owner";
      intro = `You were assigned this idea ${age}h ago and the response SLA (${idea.deadline_hours}h) has passed. Please update status and write a response to the submitter.`;
      subject = `[Overdue] Please respond — ${idea.subject}`;
      break;
    case "chair_nudge":
      kicker = "Daios Cove · Ideas & Opinions";
      heading = "Response overdue — needs an owner";
      intro = `This idea is past its SLA (${idea.deadline_hours}h, ${age}h elapsed) and has no assigned owner. Please triage and assign.`;
      subject = `[Overdue] Needs owner — ${idea.subject}`;
      break;
    case "escalation":
      kicker = "Daios Cove · Ideas & Opinions · ESCALATION";
      heading = "Escalation — SLA exceeded by 50%";
      intro = `This idea is now ${age}h old, past its ${idea.deadline_hours}h SLA AND its ${Math.round(idea.deadline_hours * 1.5)}h escalation threshold. Decide: act, delegate, or formally close with a reason.`;
      subject = `[ESCALATED] ${idea.subject}`;
      break;
  }

  const barColour = type === "escalation" || type === "critical_alert" ? "#dc2626" : "#ea580c";

  const html = `<!doctype html><html><body style="margin:0;padding:40px 20px;font-family:-apple-system,BlinkMacSystemFont,sans-serif;background:#f5f5f0;color:#1a1a1a">
<div style="max-width:580px;margin:0 auto;background:#fff;border-radius:8px;overflow:hidden;box-shadow:0 1px 3px rgba(0,0,0,0.08)">
  <div style="padding:20px 28px;border-bottom:3px solid ${barColour}">
    <div style="font-size:11px;letter-spacing:2px;color:#a38a6a;text-transform:uppercase;font-weight:600">${esc(kicker)}</div>
    <div style="font-size:18px;font-weight:600;margin-top:6px;color:${barColour}">${esc(heading)}</div>
  </div>
  <div style="padding:22px 28px">
    <p style="font-size:14px;line-height:1.55;color:#333;margin-top:0">${intro}</p>
    <div style="background:#fafaf6;border-left:3px solid ${sevColor(sev)};padding:14px 16px;border-radius:4px;margin:18px 0">
      <div style="font-size:11px;letter-spacing:1px;color:#888;text-transform:uppercase;font-weight:600;margin-bottom:4px">Idea</div>
      <div style="font-size:15px;font-weight:600;margin-bottom:6px">${esc(idea.subject)}</div>
      <div style="font-size:12px;color:#666">
        Severity: <strong style="color:${sevColor(sev)};text-transform:uppercase">${esc(sev)}</strong>
        &nbsp;·&nbsp; Status: ${esc(idea.status)}
        &nbsp;·&nbsp; Age: ${age}h
        ${idea.assigned_to_name ? `&nbsp;·&nbsp; Owner: ${esc(idea.assigned_to_name)}` : ""}
      </div>
    </div>
    <p style="text-align:center;margin-top:22px">
      <a href="${esc(link)}" style="display:inline-block;padding:10px 24px;background:#a38a6a;color:#fff;text-decoration:none;border-radius:4px;font-size:13px;font-weight:600">Open in dashboard</a>
    </p>
    <p style="font-size:11px;color:#888;margin-top:22px;line-height:1.5">
      This reminder will not fire again today for this idea. Next reminder tomorrow morning if still open.
    </p>
  </div>
</div></body></html>`;

  return { subject, html };
}

function renderDigestEmail(
  kind: "committee" | "owner", ideas: StaleIdea[], dashboardUrl: string,
): { subject: string; html: string } {
  const total = ideas.length;
  const overdue = ideas.filter((i) => i.sla_state === "overdue" || i.sla_state === "escalated").length;
  const critical = ideas.filter((i) => i.severity === "critical").length;

  const subject = kind === "committee"
    ? `Weekly Ideas digest — ${total} open · ${overdue} overdue`
    : `Your Ideas digest — ${total} open · ${overdue} overdue`;

  const heading = kind === "committee"
    ? "Weekly committee digest"
    : "Your open ideas";

  // Sort: escalated > overdue > warning > on_track, then oldest first
  const rank: Record<string, number> = { escalated: 0, overdue: 1, warning: 2, on_track: 3 };
  const sorted = [...ideas].sort((a, b) => {
    const r = (rank[a.sla_state] ?? 9) - (rank[b.sla_state] ?? 9);
    if (r !== 0) return r;
    return a.created_at.localeCompare(b.created_at);
  });

  const rows = sorted.slice(0, 50).map((i) => {
    const ageDays = Math.floor(i.hours_elapsed / 24);
    const ageLabel = ageDays >= 1 ? `${ageDays}d` : `${Math.round(i.hours_elapsed)}h`;
    const stateDot = i.sla_state === "escalated" ? "⚠️"
      : i.sla_state === "overdue" ? "🔴"
      : i.sla_state === "warning" ? "🟡"
      : "🟢";
    return `<tr>
      <td style="padding:8px 10px;border-bottom:1px solid #eee;font-size:13px">${stateDot} ${esc(i.subject).slice(0, 70)}</td>
      <td style="padding:8px 10px;border-bottom:1px solid #eee;font-size:11px;color:${sevColor(i.severity)};text-transform:uppercase;font-weight:600">${esc(i.severity)}</td>
      <td style="padding:8px 10px;border-bottom:1px solid #eee;font-size:12px;color:#666">${ageLabel}</td>
      <td style="padding:8px 10px;border-bottom:1px solid #eee;font-size:12px;color:#666">${esc(i.assigned_to_name ?? "—")}</td>
    </tr>`;
  }).join("");

  const html = `<!doctype html><html><body style="margin:0;padding:40px 20px;font-family:-apple-system,BlinkMacSystemFont,sans-serif;background:#f5f5f0;color:#1a1a1a">
<div style="max-width:720px;margin:0 auto;background:#fff;border-radius:8px;overflow:hidden">
  <div style="padding:20px 28px;border-bottom:3px solid #a38a6a">
    <div style="font-size:11px;letter-spacing:2px;color:#a38a6a;text-transform:uppercase;font-weight:600">Daios Cove · Ideas & Opinions</div>
    <div style="font-size:20px;font-weight:600;margin-top:4px">${esc(heading)}</div>
  </div>
  <div style="padding:22px 28px">
    <div style="display:flex;gap:18px;margin-bottom:20px">
      <div><div style="font-size:28px;font-weight:700">${total}</div><div style="font-size:11px;color:#888;text-transform:uppercase;letter-spacing:1px">Open</div></div>
      <div><div style="font-size:28px;font-weight:700;color:#dc2626">${overdue}</div><div style="font-size:11px;color:#888;text-transform:uppercase;letter-spacing:1px">Overdue</div></div>
      <div><div style="font-size:28px;font-weight:700;color:#dc2626">${critical}</div><div style="font-size:11px;color:#888;text-transform:uppercase;letter-spacing:1px">Critical</div></div>
    </div>
    <table style="width:100%;border-collapse:collapse;border-top:1px solid #eee">
      <thead>
        <tr style="background:#fafaf6">
          <th style="text-align:left;padding:8px 10px;font-size:10px;letter-spacing:1px;text-transform:uppercase;color:#888">Subject</th>
          <th style="text-align:left;padding:8px 10px;font-size:10px;letter-spacing:1px;text-transform:uppercase;color:#888">Severity</th>
          <th style="text-align:left;padding:8px 10px;font-size:10px;letter-spacing:1px;text-transform:uppercase;color:#888">Age</th>
          <th style="text-align:left;padding:8px 10px;font-size:10px;letter-spacing:1px;text-transform:uppercase;color:#888">Owner</th>
        </tr>
      </thead>
      <tbody>${rows}</tbody>
    </table>
    ${sorted.length > 50 ? `<p style="font-size:12px;color:#888;margin-top:12px">Showing 50 of ${sorted.length}. Open dashboard for the full list.</p>` : ""}
    <p style="text-align:center;margin-top:22px">
      <a href="${esc(dashboardUrl)}/ideas" style="display:inline-block;padding:10px 22px;background:#a38a6a;color:#fff;text-decoration:none;border-radius:4px;font-size:13px;font-weight:600">Open Ideas & Opinions</a>
    </p>
  </div>
</div></body></html>`;

  return { subject, html };
}

function json(p: unknown, s = 200) {
  return new Response(JSON.stringify(p), { status: s, headers: { "content-type": "application/json" } });
}
