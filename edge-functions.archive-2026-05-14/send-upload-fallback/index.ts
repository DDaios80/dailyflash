// send-upload-fallback — Lovable Cloud edge function.
//
// Fires when maybe_trigger_upload_fallback() detects that today's Opera file
// has not been uploaded by 19:30 Athens. Sends a detailed instructional
// email to Kyrillos Michailides (or whoever is configured) explaining how
// to extract the Daily Flash from Opera and upload it to OneDrive.
//
// Writes an `upload_fallback_fires` row so the trigger stays idempotent
// (one fire per day, success or fail).
//
// Contract:
//   POST /functions/v1/send-upload-fallback
//   Headers:  Authorization: Bearer <PIPELINE_SECRET>
//   Body:     { "date": "YYYY-MM-DD" }

import { createClient } from "jsr:@supabase/supabase-js@2";

Deno.serve(async (req) => {
  if (req.method !== "POST") return json({ error: "POST only" }, 405);

  const secret = Deno.env.get("PIPELINE_SECRET");
  const auth = req.headers.get("authorization") ?? "";
  if (!secret || auth !== `Bearer ${secret}`) return json({ error: "unauthorized" }, 401);

  const resendKey   = Deno.env.get("RESEND_API_KEY");
  const fromAddress = Deno.env.get("EMAIL_FROM") ?? "Daios Cove Flash <flash@daioshotels.com>";
  const supabaseUrl = Deno.env.get("SUPABASE_URL");
  const serviceKey  = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY");
  if (!resendKey) return json({ error: "RESEND_API_KEY not set" }, 500);
  if (!supabaseUrl || !serviceKey) return json({ error: "Supabase env missing" }, 500);

  const body = await req.json().catch(() => ({}));
  const dateStr: string | undefined = body?.date;
  if (!dateStr) return json({ error: "date required" }, 400);

  const supa = createClient(supabaseUrl, serviceKey);

  // Read recipient + instructions from app_settings
  const { data: cfgRows, error: cfgErr } = await supa
    .from("app_settings").select("key, value")
    .in("key", ["upload_fallback_recipient_email", "upload_fallback_instructions_text"]);
  if (cfgErr) return json({ error: "settings read failed: " + cfgErr.message }, 500);

  const cfg = Object.fromEntries((cfgRows ?? []).map((r) => [r.key, r.value]));
  const recipient = (cfg.upload_fallback_recipient_email ?? "").trim();
  const instructions = (cfg.upload_fallback_instructions_text ?? "").trim();
  if (!recipient) return json({ error: "upload_fallback_recipient_email not set" }, 500);
  if (!instructions) return json({ error: "upload_fallback_instructions_text not set" }, 500);

  const subject = `[URGENT] Daily Flash upload needed — ${dateStr}`;
  const html = renderEmail(dateStr, instructions);

  let messageId: string | null = null;
  let sendErr: string | null = null;
  try {
    messageId = await sendViaResend({
      apiKey: resendKey, from: fromAddress, to: recipient, subject, html,
    });
  } catch (e) {
    sendErr = String((e as Error)?.message ?? e).slice(0, 500);
    console.error("fallback email failed:", sendErr);
  }

  // Write idempotency + audit row
  const { error: logErr } = await supa.from("upload_fallback_fires").upsert({
    sent_date: dateStr,
    recipient,
    resend_message_id: messageId,
    status: sendErr ? "failed" : "sent",
    error: sendErr,
  }, { onConflict: "sent_date" });
  if (logErr) console.error("upload_fallback_fires upsert failed:", logErr.message);

  if (sendErr) return json({ ok: false, error: sendErr }, 502);
  return json({ ok: true, recipient, message_id: messageId });
});

// ─── Helpers ────────────────────────────────────────────────────────────────

function json(p: unknown, s = 200) {
  return new Response(JSON.stringify(p), { status: s, headers: { "content-type": "application/json" } });
}

function esc(s: unknown): string {
  return String(s ?? "")
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
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

function renderEmail(dateStr: string, instructions: string): string {
  // Render plain-text instructions with line breaks preserved. The admin
  // edits the instructions via `set_upload_fallback_instructions()` RPC;
  // we render that text inside a <pre>-style block to respect newlines
  // and indentation without requiring HTML knowledge.
  return `<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:0;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Helvetica,Arial,sans-serif;background:#f5f5f0;color:#1a1a1a">
<div style="max-width:640px;margin:0 auto;background:#ffffff">
  <div style="padding:24px 28px;border-bottom:3px solid #dc2626">
    <div style="font-size:11px;letter-spacing:2px;color:#a38a6a;text-transform:uppercase;font-weight:600">Daios Cove · Daily Flash</div>
    <div style="font-size:22px;font-weight:600;margin-top:4px;color:#dc2626">Action needed — ${esc(dateStr)}</div>
    <div style="font-size:12px;color:#666;margin-top:4px">Upload deadline missed · urgent</div>
  </div>
  <div style="padding:20px 28px 28px">
    <div style="background:#fef2f2;border:1px solid #fecaca;border-radius:6px;padding:16px;margin-bottom:16px">
      <div style="font-size:13px;color:#7f1d1d;line-height:1.5">
        The 20:00 Athens flash email fan-out cannot proceed without today's Opera export.
        Please follow the steps below as soon as possible.
      </div>
    </div>

    <div style="font-size:11px;color:#888;text-transform:uppercase;letter-spacing:1px;font-weight:600;margin-bottom:8px">Instructions</div>
    <div style="font-size:13px;line-height:1.65;white-space:pre-wrap;color:#1a1a1a;background:#fafaf6;padding:16px;border-radius:6px;border-left:3px solid #a38a6a">${esc(instructions)}</div>

    <div style="margin-top:24px;font-size:12px;color:#666;line-height:1.5">
      This is an automated message. If you believe you received it in error, reply
      to this email or contact Dimitris Daios (d.daios@daioshotels.com).
    </div>
  </div>
</div>
</body></html>`;
}
