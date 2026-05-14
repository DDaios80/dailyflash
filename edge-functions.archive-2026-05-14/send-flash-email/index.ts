// send-flash-email — Lovable Cloud edge function with approval gate.
//
// Fans out the daily flash report as role-tailored HTML emails via Resend.
// Invoked at 06:00 Athens by a Railway cron service (email-dispatcher).
//
// ─── Flow ──────────────────────────────────────────────────────────────────
//   06:00 Athens  Railway cron  →  POST /send-flash-email  (no body / mode=preview)
//                                  │
//                                  ├─ insert flash_email_approvals row (random token)
//                                  ├─ render Management-tier email (full content)
//                                  ├─ add big Approve / Reject links
//                                  └─ send ONLY to APPROVER_EMAIL (Thelxi)
//
//   (human click)  Approve link   →  GET  /approve-flash-email?token=…&action=approve
//                                  │
//                                  └─ approve-flash-email fn validates token, calls
//                                     this function again with mode=fanout + token
//
//   (this fn)      mode=fanout    →  validate token, then send role-filtered
//                                    emails to every recipient EXCEPT the approver
//                                    (they already got theirs as the preview).
//
// ─── Contract ──────────────────────────────────────────────────────────────
//   POST /functions/v1/send-flash-email
//   Headers:  Authorization: Bearer <PIPELINE_SECRET>
//   Body:     {
//     "date":            "YYYY-MM-DD"    optional, defaults to today Athens
//     "mode":            "preview" | "fanout" | "direct"   default "preview"
//     "approval_token":  string          required when mode=fanout
//     "dry_run":         bool            optional — skip sending
//     "only_recipients": ["a@b.com"]     optional — restrict for testing
//   }
//
// Responses: see json() calls inline.

import { createClient } from "jsr:@supabase/supabase-js@2";

// ─── Types ──────────────────────────────────────────────────────────────────

type UserRole =
  | "admin"
  | "management"
  | "guest_relations"
  | "front_office"
  | "housekeeping"
  | "fnb"
  | "maintenance"
  | "reservations"
  | "kepos"
  | "kids_club"
  | "sales"
  | "marketing"
  | "accounting"
  | "it"
  | "call_center"
  | "general";

type Mode = "preview" | "fanout" | "direct";

type Section =
  | "occupancy"
  | "special_attention_arrivals"
  | "special_attention_departures"
  | "complimentary_partner_arrivals"
  | "pep_arrivals"
  | "birthdays_in_house"
  | "allergies_in_house"
  | "alister_findings"
  | "pool_heating"
  | "daily_briefing";

type Recipient = { user_id: string; email: string; role: UserRole };
type DeliveryLog = {
  recipient_email: string;
  role: UserRole;
  status: "sent" | "failed" | "skipped";
  resend_message_id?: string;
  error?: string;
};

// ─── Role → sections map ────────────────────────────────────────────────────
// Tier A — full guest detail. Tier B — metrics only (no guest PII).

const FULL_A: Section[] = [
  "occupancy",
  "special_attention_arrivals",
  "special_attention_departures",
  "complimentary_partner_arrivals",
  "pep_arrivals",
  "birthdays_in_house",
  "allergies_in_house",
  "alister_findings",
  "pool_heating",
  "daily_briefing",
];

const SECTIONS_BY_ROLE: Record<UserRole, Section[]> = {
  // Tier A — everything
  admin: FULL_A,
  management: FULL_A,
  guest_relations: FULL_A,
  sales: FULL_A, // per user's explicit instruction — DOS-level need VIP visibility

  // Tier A — operational, no A-lister reasoning
  front_office: [
    "occupancy",
    "special_attention_arrivals",
    "special_attention_departures",
    "complimentary_partner_arrivals",
    "pep_arrivals",
    "birthdays_in_house",
    "allergies_in_house",
    "daily_briefing",
  ],
  housekeeping: [
    "occupancy",
    "special_attention_arrivals",
    "special_attention_departures",
    "birthdays_in_house",
    "pool_heating",
  ],
  fnb: [
    "occupancy",
    "special_attention_arrivals",
    "allergies_in_house", // prominent for F&B
    "birthdays_in_house",
    "pep_arrivals",
    "daily_briefing",
  ],
  maintenance: ["occupancy", "pool_heating", "daily_briefing"],
  reservations: [
    "occupancy",
    "special_attention_arrivals",
    "special_attention_departures",
    "birthdays_in_house",
    "daily_briefing",
  ],
  kepos: [
    "occupancy",
    "special_attention_arrivals",
    "birthdays_in_house",
    "daily_briefing",
  ],
  kids_club: [
    "occupancy",
    "special_attention_arrivals",
    "birthdays_in_house",
  ],

  // Tier B+ — metrics + A-lister (social / brand partnerships / phone ID)
  marketing: ["occupancy", "alister_findings", "daily_briefing"],
  call_center: ["occupancy", "alister_findings", "daily_briefing"],

  // Tier B — metrics only, no guest PII
  accounting: ["occupancy", "daily_briefing"],
  it: ["occupancy", "daily_briefing"],
  general: ["occupancy", "daily_briefing"],
};

const ROLE_LABEL: Record<UserRole, string> = {
  admin: "Admin",
  management: "Management",
  guest_relations: "Guest Relations",
  front_office: "Front Office",
  housekeeping: "Housekeeping",
  fnb: "F&B",
  maintenance: "Maintenance",
  reservations: "Reservations",
  kepos: "Kepos",
  kids_club: "Kids Club",
  sales: "Sales",
  marketing: "Marketing",
  accounting: "Accounting",
  it: "IT",
  call_center: "Call Center",
  general: "General",
};

const DEFAULT_APPROVER = "thelxi.smyrnaki@daioshotels.com";

// Roles that see the full PDF (with A-lister). Anyone not in this set gets
// the redacted PDF. Matches can_see_alister() in phase5_email_roles.sql.
const ALISTER_ROLES: UserRole[] = [
  "admin", "management", "guest_relations", "sales", "marketing", "call_center",
];

// ─── Main ──────────────────────────────────────────────────────────────────

Deno.serve(async (req) => {
  if (req.method !== "POST") return json({ error: "POST only" }, 405);

  const pipelineSecret = Deno.env.get("PIPELINE_SECRET");
  const auth = req.headers.get("authorization") ?? "";
  if (!pipelineSecret || auth !== `Bearer ${pipelineSecret}`) {
    return json({ error: "unauthorized" }, 401);
  }

  const resendKey = Deno.env.get("RESEND_API_KEY");
  const fromAddress = Deno.env.get("EMAIL_FROM") ??
    "Daios Cove Flash <flash@daioshotels.com>";
  const dashboardUrl = Deno.env.get("DASHBOARD_URL") ??
    "https://daioscove-flash.lovable.app";
  const approverEmail = (Deno.env.get("APPROVER_EMAIL") ?? DEFAULT_APPROVER).toLowerCase();
  const approveFnUrl = Deno.env.get("APPROVE_FLASH_EMAIL_URL") ?? "";
  const pdfFnUrl = Deno.env.get("GENERATE_FLASH_PDF_URL") ?? "";
  const supabaseUrl = Deno.env.get("SUPABASE_URL");
  const serviceKey = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY");

  if (!resendKey) return json({ error: "RESEND_API_KEY not set" }, 500);
  if (!supabaseUrl || !serviceKey) {
    return json({ error: "SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY not set" }, 500);
  }

  const body = await req.json().catch(() => ({}));
  const mode: Mode = (body?.mode ?? "preview") as Mode;
  const dryRun: boolean = !!body?.dry_run;
  const onlyRecipients: string[] | null = Array.isArray(body?.only_recipients)
    ? body.only_recipients.map((s: string) => s.toLowerCase())
    : null;
  const reportDate: string = typeof body?.date === "string"
    ? body.date
    : athensToday();
  const submittedToken: string | undefined = body?.approval_token;

  const supa = createClient(supabaseUrl, serviceKey);

  // 1. Pull the flash payload
  const { data: flash, error: flashErr } = await supa
    .from("flash_reports")
    .select("payload, computed_at")
    .eq("report_date", reportDate)
    .maybeSingle();
  if (flashErr) return json({ error: "flash_reports query failed: " + flashErr.message }, 500);
  if (!flash) return json({ error: `no flash_reports row for ${reportDate}` }, 404);

  // 2. Resolve recipients via RPC
  const { data: recipRows, error: recipErr } = await supa.rpc("email_recipients");
  if (recipErr) return json({ error: "email_recipients() failed: " + recipErr.message }, 500);
  let recipients: Recipient[] = (recipRows ?? []) as Recipient[];
  if (onlyRecipients) {
    recipients = recipients.filter((r) => onlyRecipients.includes(r.email.toLowerCase()));
  }

  // Pre-fetch both PDF variants once per run. Fail-soft: if PDF generation
  // throws we still send the email without the attachment, with an error
  // logged. This avoids blocking the whole batch on a Browserless hiccup.
  const pdfCache: PdfCache = { full: null, redacted: null };
  const secret = pipelineSecret;
  if (pdfFnUrl) {
    const [full, redacted] = await Promise.all([
      fetchFlashPdfBase64({ pdfFnUrl, secret, date: reportDate, includeAlister: true }),
      fetchFlashPdfBase64({ pdfFnUrl, secret, date: reportDate, includeAlister: false }),
    ]);
    pdfCache.full = full;
    pdfCache.redacted = redacted;
  }

  // ─── Mode dispatch ────────────────────────────────────────────────────
  if (mode === "preview") {
    return await handlePreview({
      supa,
      recipients,
      approverEmail,
      approveFnUrl,
      reportDate,
      flashPayload: flash.payload,
      dashboardUrl,
      fromAddress,
      resendKey,
      dryRun,
      pdfCache,
    });
  }

  if (mode === "fanout") {
    return await handleFanout({
      supa,
      recipients,
      approverEmail,
      reportDate,
      flashPayload: flash.payload,
      dashboardUrl,
      fromAddress,
      resendKey,
      dryRun,
      submittedToken,
      pdfCache,
    });
  }

  if (mode === "direct") {
    return await handleDirect({
      supa,
      recipients,
      reportDate,
      flashPayload: flash.payload,
      dashboardUrl,
      fromAddress,
      resendKey,
      dryRun,
      pdfCache,
    });
  }

  return json({ error: `unknown mode: ${mode}` }, 400);
});

type PdfCache = { full: string | null; redacted: string | null };

function pickPdfForRole(role: UserRole, cache: PdfCache): string | null {
  return ALISTER_ROLES.includes(role) ? cache.full : cache.redacted;
}

// ─── Mode handlers ─────────────────────────────────────────────────────────

async function handlePreview(opts: {
  supa: ReturnType<typeof createClient>;
  recipients: Recipient[];
  approverEmail: string;
  approveFnUrl: string;
  reportDate: string;
  flashPayload: Record<string, unknown>;
  dashboardUrl: string;
  fromAddress: string;
  resendKey: string;
  dryRun: boolean;
  pdfCache: PdfCache;
}): Promise<Response> {
  const approver = opts.recipients.find(
    (r) => r.email.toLowerCase() === opts.approverEmail,
  );
  if (!approver) {
    return json({
      error: `approver ${opts.approverEmail} not in user_roles — ` +
        `can't send preview. Create the user in Lovable Auth and ` +
        `assign a role (any Tier A role).`,
    }, 500);
  }

  // Create (or refresh) the approval row with a new token
  const token = crypto.randomUUID().replace(/-/g, "");
  const { error: upsertErr } = await opts.supa
    .from("flash_email_approvals")
    .upsert({
      report_date: opts.reportDate,
      approval_token: token,
      preview_sent_at: new Date().toISOString(),
      approved_at: null,
      approved_by: null,
      rejected_at: null,
      rejected_by: null,
      fanned_out_at: null,
    }, { onConflict: "report_date" });
  if (upsertErr) {
    return json({ error: "failed to upsert approval row: " + upsertErr.message }, 500);
  }

  const approveUrl = `${opts.approveFnUrl}?token=${token}&action=approve`;
  const rejectUrl = `${opts.approveFnUrl}?token=${token}&action=reject`;

  // Render with the approver's role + an approval banner on top.
  const sections = SECTIONS_BY_ROLE[approver.role] ?? FULL_A;
  const approvalBanner = renderApprovalBanner({
    reportDate: opts.reportDate,
    approveUrl,
    rejectUrl,
    recipientCount: opts.recipients.length,
  });
  const html = renderEmail({
    payload: opts.flashPayload,
    reportDate: opts.reportDate,
    sections,
    role: approver.role,
    dashboardUrl: opts.dashboardUrl,
    prepend: approvalBanner,
  });

  if (opts.dryRun) {
    return json({
      ok: true,
      mode: "preview",
      dry_run: true,
      report_date: opts.reportDate,
      preview_recipient: approver.email,
      token_length: token.length,
    });
  }

  const deliveries: DeliveryLog[] = [];
  const pdfB64 = pickPdfForRole(approver.role, opts.pdfCache);
  try {
    const msgId = await sendViaResend({
      to: approver.email,
      from: opts.fromAddress,
      subject: `[APPROVAL] Daily Flash preview — ${formatAthensDate(opts.reportDate)}`,
      html,
      apiKey: opts.resendKey,
      attachment: pdfB64 ? {
        filename: `daios-flash-${opts.reportDate}.pdf`,
        content_base64: pdfB64,
      } : undefined,
    });
    deliveries.push({
      recipient_email: approver.email,
      role: approver.role,
      status: "sent",
      resend_message_id: msgId,
    });
  } catch (e) {
    deliveries.push({
      recipient_email: approver.email,
      role: approver.role,
      status: "failed",
      error: String((e as Error)?.message ?? e),
    });
  }

  await opts.supa.from("email_deliveries").insert(
    deliveries.map((d) => ({
      report_date: opts.reportDate,
      recipient_email: d.recipient_email,
      role: d.role,
      status: d.status,
      resend_message_id: d.resend_message_id ?? null,
      error: d.error ?? null,
    })),
  );

  const sent = deliveries.filter((d) => d.status === "sent").length;
  const failed = deliveries.filter((d) => d.status === "failed").length;
  return json({
    ok: true,
    mode: "preview",
    report_date: opts.reportDate,
    approver_email: approver.email,
    total_recipients_pending_approval: opts.recipients.length,
    sent,
    failed,
    deliveries,
  });
}

async function handleFanout(opts: {
  supa: ReturnType<typeof createClient>;
  recipients: Recipient[];
  approverEmail: string;
  reportDate: string;
  flashPayload: Record<string, unknown>;
  dashboardUrl: string;
  fromAddress: string;
  resendKey: string;
  dryRun: boolean;
  submittedToken: string | undefined;
  pdfCache: PdfCache;
}): Promise<Response> {
  if (!opts.submittedToken) {
    return json({ error: "approval_token required for mode=fanout" }, 400);
  }

  // Validate token + approval status
  const { data: approval, error: apprErr } = await opts.supa
    .from("flash_email_approvals")
    .select("*")
    .eq("report_date", opts.reportDate)
    .maybeSingle();
  if (apprErr) return json({ error: "approval lookup failed: " + apprErr.message }, 500);
  if (!approval) return json({ error: "no approval row for " + opts.reportDate }, 400);
  if (approval.approval_token !== opts.submittedToken) {
    return json({ error: "invalid approval_token" }, 403);
  }
  if (approval.rejected_at) return json({ error: "approval was rejected" }, 403);
  if (!approval.approved_at) return json({ error: "approval pending" }, 403);
  if (approval.fanned_out_at) {
    return json({
      error: "already fanned out",
      fanned_out_at: approval.fanned_out_at,
    }, 409);
  }

  // Exclude the approver (they got the preview)
  const toSend = opts.recipients.filter(
    (r) => r.email.toLowerCase() !== opts.approverEmail,
  );
  const deliveries = await sendToAll({
    recipients: toSend,
    reportDate: opts.reportDate,
    flashPayload: opts.flashPayload,
    dashboardUrl: opts.dashboardUrl,
    fromAddress: opts.fromAddress,
    resendKey: opts.resendKey,
    dryRun: opts.dryRun,
    pdfCache: opts.pdfCache,
  });

  await opts.supa.from("email_deliveries").insert(
    deliveries.map((d) => ({
      report_date: opts.reportDate,
      recipient_email: d.recipient_email,
      role: d.role,
      status: d.status,
      resend_message_id: d.resend_message_id ?? null,
      error: d.error ?? null,
    })),
  );
  await opts.supa.from("flash_email_approvals")
    .update({ fanned_out_at: new Date().toISOString() })
    .eq("report_date", opts.reportDate);

  const sent = deliveries.filter((d) => d.status === "sent").length;
  const failed = deliveries.filter((d) => d.status === "failed").length;
  const skipped = deliveries.filter((d) => d.status === "skipped").length;
  return json({
    ok: true,
    mode: "fanout",
    report_date: opts.reportDate,
    approver_email: approval.approved_by ?? opts.approverEmail,
    total_recipients: toSend.length,
    sent,
    failed,
    skipped,
    dry_run: opts.dryRun,
    deliveries,
  });
}

async function handleDirect(opts: {
  supa: ReturnType<typeof createClient>;
  recipients: Recipient[];
  reportDate: string;
  flashPayload: Record<string, unknown>;
  dashboardUrl: string;
  fromAddress: string;
  resendKey: string;
  dryRun: boolean;
  pdfCache: PdfCache;
}): Promise<Response> {
  const deliveries = await sendToAll({
    recipients: opts.recipients,
    reportDate: opts.reportDate,
    flashPayload: opts.flashPayload,
    dashboardUrl: opts.dashboardUrl,
    fromAddress: opts.fromAddress,
    resendKey: opts.resendKey,
    dryRun: opts.dryRun,
    pdfCache: opts.pdfCache,
  });
  await opts.supa.from("email_deliveries").insert(
    deliveries.map((d) => ({
      report_date: opts.reportDate,
      recipient_email: d.recipient_email,
      role: d.role,
      status: d.status,
      resend_message_id: d.resend_message_id ?? null,
      error: d.error ?? null,
    })),
  );
  const sent = deliveries.filter((d) => d.status === "sent").length;
  const failed = deliveries.filter((d) => d.status === "failed").length;
  const skipped = deliveries.filter((d) => d.status === "skipped").length;
  return json({
    ok: true,
    mode: "direct",
    report_date: opts.reportDate,
    total_recipients: opts.recipients.length,
    sent,
    failed,
    skipped,
    dry_run: opts.dryRun,
    deliveries,
  });
}

async function sendToAll(opts: {
  recipients: Recipient[];
  reportDate: string;
  flashPayload: Record<string, unknown>;
  dashboardUrl: string;
  fromAddress: string;
  resendKey: string;
  dryRun: boolean;
  pdfCache: PdfCache;
}): Promise<DeliveryLog[]> {
  const out: DeliveryLog[] = [];
  for (const r of opts.recipients) {
    const sections = SECTIONS_BY_ROLE[r.role];
    if (!sections) {
      out.push({
        recipient_email: r.email,
        role: r.role,
        status: "skipped",
        error: `unknown role: ${r.role}`,
      });
      continue;
    }
    const html = renderEmail({
      payload: opts.flashPayload,
      reportDate: opts.reportDate,
      sections,
      role: r.role,
      dashboardUrl: opts.dashboardUrl,
    });
    const subject = `Daily Flash — ${formatAthensDate(opts.reportDate)}`;

    if (opts.dryRun) {
      out.push({ recipient_email: r.email, role: r.role, status: "skipped" });
      continue;
    }

    const pdfB64 = pickPdfForRole(r.role, opts.pdfCache);
    try {
      const msgId = await sendViaResend({
        to: r.email,
        from: opts.fromAddress,
        subject,
        html,
        apiKey: opts.resendKey,
        attachment: pdfB64 ? {
          filename: `daios-flash-${opts.reportDate}.pdf`,
          content_base64: pdfB64,
        } : undefined,
      });
      out.push({
        recipient_email: r.email,
        role: r.role,
        status: "sent",
        resend_message_id: msgId,
      });
    } catch (e) {
      out.push({
        recipient_email: r.email,
        role: r.role,
        status: "failed",
        error: String((e as Error)?.message ?? e),
      });
    }
  }
  return out;
}

// ─── Helpers ────────────────────────────────────────────────────────────────

function json(payload: unknown, status = 200): Response {
  return new Response(JSON.stringify(payload), {
    status,
    headers: { "content-type": "application/json" },
  });
}

function athensToday(): string {
  const now = new Date();
  const parts = new Intl.DateTimeFormat("en-CA", {
    timeZone: "Europe/Athens",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  }).formatToParts(now);
  const y = parts.find((p) => p.type === "year")!.value;
  const m = parts.find((p) => p.type === "month")!.value;
  const d = parts.find((p) => p.type === "day")!.value;
  return `${y}-${m}-${d}`;
}

function formatAthensDate(iso: string): string {
  const [y, m, d] = iso.split("-");
  return `${d}/${m}/${y}`;
}

async function sendViaResend(opts: {
  to: string;
  from: string;
  subject: string;
  html: string;
  apiKey: string;
  attachment?: { filename: string; content_base64: string };
}): Promise<string> {
  const body: Record<string, unknown> = {
    from: opts.from,
    to: [opts.to],
    subject: opts.subject,
    html: opts.html,
  };
  if (opts.attachment) {
    body.attachments = [{
      filename: opts.attachment.filename,
      content: opts.attachment.content_base64,
    }];
  }
  const resp = await fetch("https://api.resend.com/emails", {
    method: "POST",
    headers: {
      "Authorization": `Bearer ${opts.apiKey}`,
      "Content-Type": "application/json",
    },
    body: JSON.stringify(body),
  });
  const text = await resp.text();
  if (!resp.ok) throw new Error(`Resend ${resp.status}: ${text}`);
  const parsed = JSON.parse(text);
  return parsed.id ?? "";
}

// Fetches one PDF variant from the generate-flash-pdf edge function and
// returns the base64 string. Called up to twice per send-flash-email run —
// once with include_alister=true, once with false — and the results are
// cached in a Map keyed by variant for the duration of the run.
async function fetchFlashPdfBase64(opts: {
  pdfFnUrl: string;
  secret: string;
  date: string;
  includeAlister: boolean;
}): Promise<string | null> {
  try {
    const resp = await fetch(opts.pdfFnUrl, {
      method: "POST",
      headers: {
        "Authorization": `Bearer ${opts.secret}`,
        "Content-Type": "application/json",
      },
      body: JSON.stringify({
        date: opts.date,
        include_alister: opts.includeAlister,
      }),
    });
    if (!resp.ok) {
      console.error(`generate-flash-pdf ${resp.status}: ${(await resp.text()).slice(0, 300)}`);
      return null;
    }
    const json = await resp.json();
    return json.pdf_base64 ?? null;
  } catch (e) {
    console.error("generate-flash-pdf fetch failed:", (e as Error).message);
    return null;
  }
}

// ─── HTML rendering ────────────────────────────────────────────────────────

function renderApprovalBanner(opts: {
  reportDate: string;
  approveUrl: string;
  rejectUrl: string;
  recipientCount: number;
}): string {
  return `<div style="background:#fff8e6;border:1px solid #fde68a;border-radius:6px;padding:18px;margin-bottom:20px">
  <div style="font-size:14px;font-weight:600;color:#92400e;margin-bottom:6px">Approval required</div>
  <div style="font-size:13px;color:#78350f;line-height:1.5;margin-bottom:14px">
    Review the flash report below for <strong>${formatAthensDate(opts.reportDate)}</strong>.
    When you click <em>Approve</em>, the role-filtered emails will be sent to
    <strong>${opts.recipientCount - 1} other recipients</strong>.
  </div>
  <div>
    <a href="${opts.approveUrl}" style="display:inline-block;padding:10px 22px;background:#16a34a;color:#fff;text-decoration:none;border-radius:4px;font-size:14px;font-weight:600;margin-right:8px">Approve &amp; send</a>
    <a href="${opts.rejectUrl}" style="display:inline-block;padding:10px 22px;background:#dc2626;color:#fff;text-decoration:none;border-radius:4px;font-size:14px;font-weight:600">Reject</a>
  </div>
</div>`;
}

function renderEmail(opts: {
  payload: Record<string, unknown>;
  reportDate: string;
  sections: Section[];
  role: UserRole;
  dashboardUrl: string;
  prepend?: string;
}): string {
  const { payload, reportDate, sections, role, dashboardUrl, prepend } = opts;
  const body = sections
    .map((s) => renderSection(s, payload))
    .filter((s) => s.length > 0)
    .join("\n");
  const dashLink = `${dashboardUrl}/dashboard?date=${reportDate}`;
  return `<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Daily Flash — ${formatAthensDate(reportDate)}</title>
</head>
<body style="margin:0;padding:0;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Helvetica,Arial,sans-serif;background:#f5f5f0;color:#1a1a1a">
<div style="max-width:640px;margin:0 auto;background:#ffffff">
  <div style="padding:24px 28px;border-bottom:3px solid #a38a6a;background:#ffffff">
    <div style="font-size:11px;letter-spacing:2px;color:#a38a6a;text-transform:uppercase;font-weight:600">Daios Cove</div>
    <div style="font-size:22px;font-weight:600;margin-top:4px">Daily Flash — ${formatAthensDate(reportDate)}</div>
    <div style="font-size:12px;color:#666;margin-top:4px">Role: ${ROLE_LABEL[role]}</div>
  </div>
  <div style="padding:20px 28px 28px">
    ${prepend ?? ""}
    ${body || '<p style="color:#888;font-style:italic">No content configured for this role.</p>'}
    <div style="margin-top:32px;padding-top:20px;border-top:1px solid #eee;text-align:center">
      <a href="${dashLink}" style="display:inline-block;padding:10px 20px;background:#a38a6a;color:#fff;text-decoration:none;border-radius:4px;font-size:14px;font-weight:500">Open full dashboard</a>
    </div>
    <div style="margin-top:24px;color:#999;font-size:11px;text-align:center">
      This is an automated report. Data is accurate as of ${formatAthensDate(reportDate)} 06:00 Athens time.<br>
      Questions? Reach out to the Daily Flash admin.
    </div>
  </div>
</div>
</body>
</html>`;
}

function renderSection(section: Section, payload: Record<string, any>): string {
  switch (section) {
    case "occupancy": return renderOccupancy(payload.occupancy ?? []);
    case "special_attention_arrivals":
      return renderGuestList("Special attention — arrivals", payload.special_attention_arrivals ?? []);
    case "special_attention_departures":
      return renderGuestList("Special attention — departures", payload.special_attention_departures ?? []);
    case "complimentary_partner_arrivals":
      return renderGuestList("Complimentary / Partner arrivals", payload.complimentary_partner_arrivals ?? []);
    case "pep_arrivals":
      return renderGuestList("PEP arrivals", payload.pep_arrivals ?? []);
    case "birthdays_in_house":
      return renderBirthdays(payload.birthdays_in_house ?? []);
    case "allergies_in_house": return renderAllergies(payload.allergies_in_house ?? []);
    case "alister_findings": return renderAlister(payload.alister_findings ?? []);
    case "pool_heating": return renderPoolHeating(payload.pool_heating ?? []);
    case "daily_briefing": return renderBriefing(payload.daily_briefing ?? null);
    default: return "";
  }
}

function h2(text: string): string {
  return `<h2 style="font-size:13px;text-transform:uppercase;letter-spacing:1.5px;color:#a38a6a;font-weight:600;margin:24px 0 8px;border-bottom:1px solid #eee;padding-bottom:4px">${escapeHtml(text)}</h2>`;
}

function escapeHtml(s: unknown): string {
  return String(s ?? "")
    .replace(/&/g, "&amp;").replace(/</g, "&lt;")
    .replace(/>/g, "&gt;").replace(/"/g, "&quot;");
}

function renderOccupancy(rows: any[]): string {
  if (!rows?.length) return "";
  const cards = rows.map((r) => {
    const label = r.label ?? r.date ?? "";
    const pct = typeof r.occupancy_pct === "number" ? `${r.occupancy_pct.toFixed(1)}%` : r.occupancy_pct ?? "—";
    const occupied = r.rooms_occupied ?? r.occupied ?? "";
    const total = r.total_rooms ?? "";
    return `<td style="padding:12px 8px;border:1px solid #eee;text-align:center;width:33%">
      <div style="font-size:11px;color:#888;text-transform:uppercase;letter-spacing:1px">${escapeHtml(label)}</div>
      <div style="font-size:24px;font-weight:600;color:#1a1a1a;margin-top:4px">${escapeHtml(pct)}</div>
      <div style="font-size:11px;color:#666;margin-top:2px">${escapeHtml(occupied)}${total ? ` / ${escapeHtml(total)}` : ""}</div>
    </td>`;
  }).join("");
  return `${h2("Occupancy")}<table style="width:100%;border-collapse:collapse"><tr>${cards}</tr></table>`;
}

function renderGuestList(title: string, rows: any[]): string {
  if (!rows?.length) return "";
  const items = rows.map((r) => {
    const room = r.room ?? r.room_number ?? "—";
    const name = r.guest_name ?? r.name ?? "";
    const extra = [r.reason, r.notes, r.nationality, r.vip_status]
      .filter((x) => x != null && x !== "")
      .map(escapeHtml).join(" · ");
    return `<tr>
      <td style="padding:6px 8px;border-bottom:1px solid #f0f0f0;font-variant-numeric:tabular-nums;color:#a38a6a;font-weight:600;white-space:nowrap">${escapeHtml(room)}</td>
      <td style="padding:6px 8px;border-bottom:1px solid #f0f0f0">${escapeHtml(name)}${extra ? ` <span style="color:#888;font-size:12px">· ${extra}</span>` : ""}</td>
    </tr>`;
  }).join("");
  return `${h2(title)}<table style="width:100%;border-collapse:collapse;font-size:13px">${items}</table>`;
}

function renderAllergies(rows: any[]): string {
  if (!rows?.length) return "";
  const items = rows.map((r) => {
    const room = r.room ?? r.room_number ?? "—";
    const name = r.guest_name ?? r.name ?? "";
    const allergy = r.allergies ?? r.allergy ?? r.notes ?? "";
    return `<tr>
      <td style="padding:8px 10px;border-bottom:1px solid #fde68a;background:#fff8e6;font-weight:600;color:#a38a6a;white-space:nowrap">${escapeHtml(room)}</td>
      <td style="padding:8px 10px;border-bottom:1px solid #fde68a;background:#fff8e6">
        <div style="font-weight:500">${escapeHtml(name)}</div>
        <div style="color:#9a3412;font-size:13px;margin-top:2px">⚠ ${escapeHtml(allergy)}</div>
      </td>
    </tr>`;
  }).join("");
  return `${h2("Allergies — in-house")}<table style="width:100%;border-collapse:collapse;font-size:13px">${items}</table>`;
}

function renderBirthdays(rows: any[]): string {
  // Phase 28.8 — line-by-line layout. Replaces the generic renderGuestList
  // which only showed room + name (the birthday-relevant `age` and
  // `birth_date` fields weren't read). Two-line block per guest, mirroring
  // the renderAllergies pattern so birthdays / allergies feel consistent.
  if (!rows?.length) return "";
  const items = rows.map((r) => {
    const room    = r.room ?? r.room_number ?? "—";
    const name    = r.guest_name ?? r.name ?? "";
    const ageNum  = typeof r.age === "number" ? r.age : (r.age ? Number(r.age) : null);
    const ageText = ageNum && Number.isFinite(ageNum) ? `Turns ${ageNum} today` : "Birthday today";
    const departure = r.departure ?? r.dep_date ?? "";
    const stayLine  = departure ? `Departing ${escapeHtml(formatStayDate(departure))}` : "";
    return `<tr>
      <td style="padding:8px 10px;border-bottom:1px solid #f5e8c8;background:#fff8e6;font-weight:600;color:#a38a6a;white-space:nowrap;vertical-align:top">${escapeHtml(room)}</td>
      <td style="padding:8px 10px;border-bottom:1px solid #f5e8c8;background:#fff8e6">
        <div style="font-weight:500">${escapeHtml(name)}</div>
        <div style="color:#9a3412;font-size:13px;margin-top:2px">${escapeHtml(ageText)}${stayLine ? ` · ${stayLine}` : ""}</div>
      </td>
    </tr>`;
  }).join("");
  return `${h2("Birthdays — in-house")}<table style="width:100%;border-collapse:collapse;font-size:13px">${items}</table>`;
}

function formatStayDate(s: string): string {
  // Accept ISO "2026-05-12" or full timestamp; render as "12 May".
  try {
    const d = new Date(s);
    if (isNaN(d.getTime())) return s;
    return d.toLocaleDateString("en-GB", { day: "2-digit", month: "short" });
  } catch {
    return s;
  }
}

function renderAlister(rows: any[]): string {
  if (!rows?.length) return "";
  const items = rows.map((r) => {
    const room = r.room ?? r.room_number ?? "";
    const subject = r.subject ?? r.name ?? "";
    const summary = r.summary ?? r.finding ?? r.reasoning ?? "";
    const category = r.category ?? r.classification ?? "";
    return `<div style="margin-bottom:14px;padding:12px;background:#fafaf6;border-left:3px solid #a38a6a;border-radius:3px">
      <div style="font-weight:600;color:#1a1a1a">${escapeHtml(subject)}${room ? ` <span style="color:#a38a6a">· Room ${escapeHtml(room)}</span>` : ""}</div>
      ${category ? `<div style="font-size:11px;color:#888;text-transform:uppercase;letter-spacing:1px;margin-top:2px">${escapeHtml(category)}</div>` : ""}
      ${summary ? `<div style="color:#444;margin-top:6px;font-size:13px;line-height:1.5">${escapeHtml(summary)}</div>` : ""}
    </div>`;
  }).join("");
  return `${h2("A-lister research")}<div>${items}</div>`;
}

function renderPoolHeating(rows: any[]): string {
  if (!rows?.length) return "";
  const items = rows.map((r) => {
    const room = r.room ?? "—";
    const status = r.status ?? r.heating ?? "";
    return `<tr>
      <td style="padding:6px 8px;border-bottom:1px solid #f0f0f0;font-weight:600;color:#a38a6a">${escapeHtml(room)}</td>
      <td style="padding:6px 8px;border-bottom:1px solid #f0f0f0">${escapeHtml(status)}</td>
    </tr>`;
  }).join("");
  return `${h2("Pool heating")}<table style="width:100%;border-collapse:collapse;font-size:13px">${items}</table>`;
}

function renderBriefing(briefing: any): string {
  if (!briefing) return "";
  const rows: string[] = [];
  const kv = (label: string, value: unknown) => {
    if (value == null || value === "") return;
    rows.push(
      `<tr><td style="padding:6px 8px;color:#888;font-size:12px;width:140px;vertical-align:top">${escapeHtml(label)}</td><td style="padding:6px 8px">${escapeHtml(value)}</td></tr>`,
    );
  };
  kv("MOD", `${briefing.mod_name ?? ""}${briefing.mod_phone ? ` · ${briefing.mod_phone}` : ""}`);
  kv("Hotel events", briefing.hotel_events);
  kv("Site inspections", briefing.site_inspections);
  kv("Group events", briefing.group_events);
  kv("Show rooms", briefing.show_rooms);
  kv("Notes", briefing.notes);
  if (!rows.length) return "";
  return `${h2("Daily briefing")}<table style="width:100%;border-collapse:collapse;font-size:13px">${rows.join("")}</table>`;
}
