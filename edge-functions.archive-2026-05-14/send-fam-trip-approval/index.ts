// send-fam-trip-approval — Lovable Cloud edge function.
//
// Fires when submit_fam_trip_for_approval RPC runs. Emails the approver
// with metadata + Approve/Reject buttons + the uploaded PDF attached.
//
// Contract:
//   POST /functions/v1/send-fam-trip-approval
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
  const approveFnUrl = Deno.env.get("APPROVE_FAM_TRIP_URL") ?? "";
  const dashboardUrl = Deno.env.get("DASHBOARD_URL") ?? "https://flashreport.daioscove.com";
  const supabaseUrl = Deno.env.get("SUPABASE_URL");
  const serviceKey = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY");
  if (!resendKey) return json({ error: "RESEND_API_KEY not set" }, 500);
  if (!supabaseUrl || !serviceKey) return json({ error: "Supabase env missing" }, 500);

  const body = await req.json().catch(() => ({}));
  const id: string | undefined = body?.trip_id;
  if (!id) return json({ error: "trip_id required" }, 400);

  const supa = createClient(supabaseUrl, serviceKey);
  const { data: trip, error: err } = await supa
    .from("fam_trips").select("*").eq("id", id).maybeSingle();
  if (err) return json({ error: "lookup failed: " + err.message }, 500);
  if (!trip) return json({ error: "fam trip not found" }, 404);
  if (trip.status !== "pending_approval") {
    return json({ error: `status is ${trip.status}, expected pending_approval` }, 409);
  }

  const { data: approvers } = await supa.rpc("list_fam_trip_approvers");
  const approver = (approvers ?? []).find(
    (a: { user_id: string }) => a.user_id === trip.approver_user_id,
  );
  if (!approver) return json({ error: "approver not in list_fam_trip_approvers" }, 500);

  // Download the PDF from storage so we can attach it
  let pdfBase64: string | null = null;
  let pdfFilename = trip.pdf_filename ?? "fam-trip.pdf";
  if (trip.pdf_path) {
    const { data: blob, error: dlErr } = await supa.storage
      .from("fam-trip-pdfs").download(trip.pdf_path);
    if (dlErr) {
      console.error("pdf download failed:", dlErr.message);
    } else if (blob) {
      pdfBase64 = bufferToBase64(new Uint8Array(await blob.arrayBuffer()));
    }
  }

  const approveUrl = `${approveFnUrl}?token=${trip.approval_token}&action=approve`;
  const rejectUrl = `${approveFnUrl}?token=${trip.approval_token}&action=reject`;
  const tripUrl = `${dashboardUrl}/fam-trips/${trip.id}`;

  const html = renderApprovalEmail({ trip, approveUrl, rejectUrl, tripUrl });
  const subject = `[APPROVAL] FAM Trip — ${trip.name}`;

  try {
    const msgId = await sendViaResend({
      to: approver.email, from: fromAddress, subject, html,
      apiKey: resendKey,
      attachment: pdfBase64 ? { filename: pdfFilename, content_base64: pdfBase64 } : undefined,
    });
    return json({ ok: true, recipient: approver.email, message_id: msgId, pdf_attached: !!pdfBase64 });
  } catch (e) {
    return json({ ok: false, error: String((e as Error)?.message ?? e) }, 502);
  }
});

// ─── Helpers ────────────────────────────────────────────────────────────────

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
    <td style="padding:6px 10px;border-bottom:1px solid #f0f0f0;color:#888;font-size:12px;width:200px;vertical-align:top;text-transform:uppercase;letter-spacing:0.5px">${esc(label)}</td>
    <td style="padding:6px 10px;border-bottom:1px solid #f0f0f0;font-size:13px">${esc(value ?? "—")}</td>
  </tr>`;
}

function renderApprovalEmail(opts: {
  trip: Record<string, unknown>; approveUrl: string; rejectUrl: string; tripUrl: string;
}): string {
  const t = opts.trip;
  const dateRange = t.start_date === t.end_date
    ? esc(t.start_date)
    : `${esc(t.start_date)} → ${esc(t.end_date)}`;
  return `<!doctype html><html lang="en"><head><meta charset="utf-8"></head>
<body style="margin:0;padding:0;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Helvetica,Arial,sans-serif;background:#f5f5f0;color:#1a1a1a">
<div style="max-width:640px;margin:0 auto;background:#ffffff">
  <div style="padding:24px 28px;border-bottom:3px solid #a38a6a">
    <div style="font-size:11px;letter-spacing:2px;color:#a38a6a;text-transform:uppercase;font-weight:600">Daios Cove</div>
    <div style="font-size:22px;font-weight:600;margin-top:4px">FAM Trip — approval required</div>
  </div>

  <div style="padding:20px 28px 4px">
    <div style="background:#fff8e6;border:1px solid #fde68a;border-radius:6px;padding:16px;margin-bottom:16px">
      <div style="font-size:14px;font-weight:600;color:#92400e;margin-bottom:6px">Review and approve</div>
      <div style="font-size:13px;color:#78350f;line-height:1.5">
        ${esc(t.created_by_name as string ?? "")} submitted a FAM trip:
        <strong>${esc(t.name as string)}</strong> (${dateRange}${t.total_pax ? `, ${esc(t.total_pax)} pax` : ""}).
        The full itinerary is attached as PDF.
      </div>
      <div style="margin-top:14px">
        <a href="${opts.approveUrl}" style="display:inline-block;padding:10px 22px;background:#16a34a;color:#fff;text-decoration:none;border-radius:4px;font-size:14px;font-weight:600;margin-right:8px">Approve</a>
        <a href="${opts.rejectUrl}" style="display:inline-block;padding:10px 22px;background:#dc2626;color:#fff;text-decoration:none;border-radius:4px;font-size:14px;font-weight:600">Reject</a>
      </div>
    </div>

    <table style="width:100%;border-collapse:collapse">
      ${row("Trip name", t.name)}
      ${row("Dates", dateRange)}
      ${row("Total pax", t.total_pax)}
      ${row("Source market", t.source_market)}
      ${row("Room plan", t.room_plan_summary)}
      ${row("In charge", t.in_charge)}
      ${row("Dietary summary", t.dietary_summary)}
      ${row("Created by", t.created_by_name)}
    </table>

    <div style="margin-top:24px;text-align:center">
      <a href="${opts.tripUrl}" style="display:inline-block;padding:8px 16px;background:transparent;color:#a38a6a;border:1px solid #a38a6a;text-decoration:none;border-radius:4px;font-size:13px">View in dashboard</a>
    </div>
  </div>
</div></body></html>`;
}
