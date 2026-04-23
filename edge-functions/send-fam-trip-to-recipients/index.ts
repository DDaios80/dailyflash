// send-fam-trip-to-recipients — Lovable Cloud edge function.
//
// Called when the creator clicks "Send" on an approved FAM trip. Renders a
// branded summary email, attaches the uploaded PDF, and sends to each
// recipient in app_settings.fam_trip_recipients. Transitions to 'sent'.
//
// Contract:
//   POST /functions/v1/send-fam-trip-to-recipients
//   Headers:  Authorization: Bearer <PIPELINE_SECRET>
//   Body:     { "trip_id": "uuid" }

import { createClient } from "jsr:@supabase/supabase-js@2";

Deno.serve(async (req) => {
  if (req.method !== "POST") return json({ error: "POST only" }, 405);
  const secret = Deno.env.get("PIPELINE_SECRET");
  const auth = req.headers.get("authorization") ?? "";
  if (!secret || auth !== `Bearer ${secret}`) return json({ error: "unauthorized" }, 401);

  const resendKey = Deno.env.get("RESEND_API_KEY");
  const fromAddress = Deno.env.get("EMAIL_FROM") ?? "Daios Cove Flash <flash@daioshotels.com>";
  const supabaseUrl = Deno.env.get("SUPABASE_URL");
  const serviceKey = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY");
  if (!resendKey) return json({ error: "RESEND_API_KEY not set" }, 500);
  if (!supabaseUrl || !serviceKey) return json({ error: "Supabase env missing" }, 500);

  const body = await req.json().catch(() => ({}));
  const id: string | undefined = body?.trip_id;
  if (!id) return json({ error: "trip_id required" }, 400);

  const supa = createClient(supabaseUrl, serviceKey);
  const { data: trip, error: iErr } = await supa
    .from("fam_trips").select("*").eq("id", id).maybeSingle();
  if (iErr) return json({ error: "lookup failed: " + iErr.message }, 500);
  if (!trip) return json({ error: "fam trip not found" }, 404);
  if (trip.status !== "approved") {
    return json({ error: `status is ${trip.status}, expected 'approved'` }, 409);
  }

  const { data: recipRow } = await supa
    .from("app_settings").select("value").eq("key", "fam_trip_recipients").maybeSingle();
  const rawList = (recipRow?.value as string) ?? "";
  const recipients = rawList
    .split(/[\s,;\n]+/).map((s) => s.trim()).filter((s) => /^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(s));
  if (recipients.length === 0) {
    return json({ error: "no recipients configured in app_settings.fam_trip_recipients" }, 500);
  }

  // Fetch the PDF
  let pdfBase64: string | null = null;
  const pdfFilename = (trip.pdf_filename as string) ?? "fam-trip.pdf";
  if (trip.pdf_path) {
    const { data: blob, error: dlErr } = await supa.storage
      .from("fam-trip-pdfs").download(trip.pdf_path as string);
    if (dlErr) console.error("pdf download failed:", dlErr.message);
    else if (blob) pdfBase64 = bufferToBase64(new Uint8Array(await blob.arrayBuffer()));
  }

  const html = renderEmail(trip as Record<string, unknown>);
  const subject = `FAM TRIP — ${trip.name}`;

  const results: { to: string; status: "sent" | "failed"; message_id?: string; error?: string }[] = [];
  for (const to of recipients) {
    try {
      const msgId = await sendViaResend({
        to, from: fromAddress, subject, html, apiKey: resendKey,
        attachment: pdfBase64 ? { filename: pdfFilename, content_base64: pdfBase64 } : undefined,
      });
      results.push({ to, status: "sent", message_id: msgId });
    } catch (e) {
      results.push({ to, status: "failed", error: String((e as Error)?.message ?? e).slice(0, 300) });
    }
  }

  const sent = results.filter((r) => r.status === "sent").length;
  const failed = results.filter((r) => r.status === "failed").length;
  if (sent > 0) {
    await supa.from("fam_trips")
      .update({ status: "sent", sent_at: new Date().toISOString() }).eq("id", id);
  }

  return json({
    ok: sent > 0, trip_id: id,
    total_recipients: recipients.length, sent, failed,
    pdf_attached: !!pdfBase64,
    deliveries: results,
  });
});

function json(p: unknown, s = 200) {
  return new Response(JSON.stringify(p), { status: s, headers: { "content-type": "application/json" } });
}
function esc(s: unknown): string {
  return String(s ?? "")
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
}
function bufferToBase64(buf: Uint8Array): string {
  const chunkSize = 32768;
  let binary = "";
  for (let i = 0; i < buf.length; i += chunkSize) {
    binary += String.fromCharCode.apply(null, Array.from(buf.subarray(i, i + chunkSize)) as number[]);
  }
  return btoa(binary);
}
async function sendViaResend(opts: {
  to: string; from: string; subject: string; html: string; apiKey: string;
  attachment?: { filename: string; content_base64: string };
}): Promise<string> {
  const body: Record<string, unknown> = {
    from: opts.from, to: [opts.to], subject: opts.subject, html: opts.html,
  };
  if (opts.attachment) {
    body.attachments = [{ filename: opts.attachment.filename, content: opts.attachment.content_base64 }];
  }
  const resp = await fetch("https://api.resend.com/emails", {
    method: "POST",
    headers: { "Authorization": `Bearer ${opts.apiKey}`, "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const text = await resp.text();
  if (!resp.ok) throw new Error(`Resend ${resp.status}: ${text}`);
  return JSON.parse(text).id ?? "";
}

function row(label: string, value: unknown): string {
  return `<tr>
    <td style="padding:6px 10px;border-bottom:1px solid #e8e6dc;color:#888;font-size:11px;width:200px;vertical-align:top;text-transform:uppercase;letter-spacing:1px;font-weight:600">${esc(label)}</td>
    <td style="padding:6px 10px;border-bottom:1px solid #e8e6dc;font-size:13px;color:#1a1a1a">${esc(value ?? "—")}</td>
  </tr>`;
}

function renderEmail(t: Record<string, unknown>): string {
  const range = t.start_date === t.end_date
    ? esc(t.start_date)
    : `${esc(t.start_date)} → ${esc(t.end_date)}`;
  return `<!doctype html><html lang="en"><head><meta charset="utf-8"></head>
<body style="margin:0;padding:0;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Helvetica,Arial,sans-serif;background:#f5f5f0;color:#1a1a1a">
<div style="max-width:640px;margin:0 auto;background:#ffffff">
  <div style="padding:24px 28px;border-bottom:3px solid #a38a6a">
    <div style="font-size:11px;letter-spacing:2px;color:#a38a6a;text-transform:uppercase;font-weight:600">Daios Cove</div>
    <div style="font-size:22px;font-weight:600;margin-top:4px">${esc(t.name)}</div>
    <div style="font-size:13px;color:#666;margin-top:2px">${range}${t.total_pax ? ` · ${esc(t.total_pax)} pax` : ""}</div>
  </div>
  <div style="padding:20px 28px 28px">
    <p style="margin:0 0 16px;font-size:13px;line-height:1.6;color:#444">
      The full itinerary is attached as PDF. Key summary below.
    </p>
    <table style="width:100%;border-collapse:collapse">
      ${row("Dates", range)}
      ${row("Total pax", t.total_pax)}
      ${row("Source market", t.source_market)}
      ${row("Room plan", t.room_plan_summary)}
      ${row("In charge", t.in_charge)}
      ${row("Dietary summary", t.dietary_summary)}
    </table>
    <table style="width:100%;border-collapse:collapse;margin-top:20px;border-top:1px solid #e8e6dc">
      ${row("Created by", t.created_by_name)}
      ${row("Approved by", t.approver_name)}
      ${row("Issue date", t.issue_date)}
    </table>
  </div>
</div></body></html>`;
}
