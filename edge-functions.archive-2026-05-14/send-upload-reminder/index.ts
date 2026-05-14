// send-upload-reminder — Lovable Cloud edge function.
//
// Sends a daily reminder email asking the Rooms Division Manager (or
// whoever admin configured) to upload the Daily Flash + Birthdays xlsx
// to OneDrive before the pipeline fires.
//
// Triggered by pg_cron once per day at the admin-configured time
// (default 19:00 Athens, ~30 min before the pipeline runs at 19:30).
//
// Contract:
//   POST /functions/v1/send-upload-reminder
//   Headers:  Authorization: Bearer <PIPELINE_SECRET>
//   Body:     { "date": "YYYY-MM-DD" (optional — today Athens by default) }
//
//   200: { ok: true, recipient, message_id, sent_date }
//   401: unauthorized
//   409: already sent today
//   500: setup error / Resend failure

import { createClient } from "jsr:@supabase/supabase-js@2";

Deno.serve(async (req) => {
  if (req.method !== "POST") return json({ error: "POST only" }, 405);

  const secret = Deno.env.get("PIPELINE_SECRET");
  const auth = req.headers.get("authorization") ?? "";
  if (!secret || auth !== `Bearer ${secret}`) {
    return json({ error: "unauthorized" }, 401);
  }

  const resendKey = Deno.env.get("RESEND_API_KEY");
  const fromAddress = Deno.env.get("EMAIL_FROM") ??
    "Daios Cove Flash <flash@daioshotels.com>";
  const dashboardUrl = Deno.env.get("DASHBOARD_URL") ??
    "https://flashreport.daioscove.com";
  const supabaseUrl = Deno.env.get("SUPABASE_URL");
  const serviceKey = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY");

  if (!resendKey) return json({ error: "RESEND_API_KEY not set" }, 500);
  if (!supabaseUrl || !serviceKey) {
    return json({ error: "SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY not set" }, 500);
  }

  const body = await req.json().catch(() => ({}));
  const sendDate: string = typeof body?.date === "string" ? body.date : athensToday();

  const supa = createClient(supabaseUrl, serviceKey);

  // 1. Idempotency — skip if already sent today
  const { data: existing } = await supa
    .from("upload_reminders")
    .select("sent_at, status")
    .eq("sent_date", sendDate)
    .maybeSingle();
  if (existing) {
    return json({
      error: "already sent",
      sent_date: sendDate,
      sent_at: existing.sent_at,
    }, 409);
  }

  // 2. Look up recipient from app_settings
  const { data: recipRow } = await supa
    .from("app_settings")
    .select("value")
    .eq("key", "upload_reminder_recipient_email")
    .maybeSingle();
  const recipient = recipRow?.value?.trim() || "thelxi.smyrnaki@daioshotels.com";

  // 3. Render + send
  const html = renderReminderHtml({ date: sendDate, dashboardUrl });
  const subject = `Upload reminder — Daily Flash for ${formatAthensDate(sendDate)}`;

  let msgId = "";
  let status: "sent" | "failed" = "sent";
  let errorText: string | null = null;
  try {
    msgId = await sendViaResend({
      to: recipient,
      from: fromAddress,
      subject,
      html,
      apiKey: resendKey,
    });
  } catch (e) {
    status = "failed";
    errorText = String((e as Error)?.message ?? e).slice(0, 500);
  }

  // 4. Log (insert — primary key on sent_date prevents double-logging)
  await supa.from("upload_reminders").insert({
    sent_date: sendDate,
    recipient,
    resend_message_id: msgId || null,
    status,
    error: errorText,
  });

  if (status === "failed") {
    return json({ ok: false, recipient, sent_date: sendDate, error: errorText }, 502);
  }
  return json({
    ok: true,
    recipient,
    sent_date: sendDate,
    message_id: msgId,
  });
});

// ─── Helpers ───────────────────────────────────────────────────────────────

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

function formatDot(iso: string): string {
  // DD.MM.YYYY — matches the OneDrive filename pattern the operator needs
  const [y, m, d] = iso.split("-");
  return `${d}.${m}.${y}`;
}

async function sendViaResend(opts: {
  to: string;
  from: string;
  subject: string;
  html: string;
  apiKey: string;
}): Promise<string> {
  const resp = await fetch("https://api.resend.com/emails", {
    method: "POST",
    headers: {
      "Authorization": `Bearer ${opts.apiKey}`,
      "Content-Type": "application/json",
    },
    body: JSON.stringify({
      from: opts.from,
      to: [opts.to],
      subject: opts.subject,
      html: opts.html,
    }),
  });
  const text = await resp.text();
  if (!resp.ok) throw new Error(`Resend ${resp.status}: ${text}`);
  const parsed = JSON.parse(text);
  return parsed.id ?? "";
}

function esc(s: unknown): string {
  return String(s ?? "")
    .replace(/&/g, "&amp;").replace(/</g, "&lt;")
    .replace(/>/g, "&gt;").replace(/"/g, "&quot;");
}

function renderReminderHtml(opts: { date: string; dashboardUrl: string }): string {
  const pretty = formatAthensDate(opts.date);
  const dot = formatDot(opts.date);
  const dashLink = `${opts.dashboardUrl}/admin`;
  return `<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Upload reminder — ${esc(pretty)}</title>
</head>
<body style="margin:0;padding:0;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Helvetica,Arial,sans-serif;background:#f5f5f0;color:#1a1a1a">
<div style="max-width:560px;margin:0 auto;background:#ffffff">
  <div style="padding:24px 28px;border-bottom:3px solid #a38a6a">
    <div style="font-size:11px;letter-spacing:2px;color:#a38a6a;text-transform:uppercase;font-weight:600">Daios Cove</div>
    <div style="font-size:22px;font-weight:600;margin-top:4px">Upload reminder — ${esc(pretty)}</div>
  </div>
  <div style="padding:20px 28px 28px;font-size:14px;line-height:1.6">
    <p>Friendly reminder to upload today's exports to OneDrive <strong>before 19:30 Athens</strong>:</p>
    <ul style="padding-left:20px;margin:16px 0">
      <li><code style="font-family:Menlo,Monaco,monospace;font-size:12px;color:#444">DailyFlash/Daily Flash/Daily Flash ${esc(dot)}.xlsx</code></li>
      <li><code style="font-family:Menlo,Monaco,monospace;font-size:12px;color:#444">DailyFlash/Birthdays/eur_birthday_v.${esc(dot)}.xlsx</code></li>
    </ul>
    <p>If the pipeline can't find today's files at 19:30, it will use yesterday's data as a fallback.</p>
    <div style="margin-top:24px;text-align:center">
      <a href="${esc(dashLink)}" style="display:inline-block;padding:10px 20px;background:#a38a6a;color:#fff;text-decoration:none;border-radius:4px;font-size:14px;font-weight:500">Open admin dashboard</a>
    </div>
    <div style="margin-top:24px;color:#999;font-size:11px;text-align:center">
      This reminder is sent automatically once per day. Change the time or recipient from the admin settings page.
    </div>
  </div>
</div>
</body>
</html>`;
}
