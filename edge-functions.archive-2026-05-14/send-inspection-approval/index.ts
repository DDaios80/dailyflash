// send-inspection-approval — Lovable Cloud edge function.
//
// Triggered by the submit_inspection_for_approval RPC (via pg_net).
// Looks up the inspection + approver, renders a branded email with the full
// form fields + Approve / Reject links, sends it via Resend.
//
// Contract:
//   POST /functions/v1/send-inspection-approval
//   Headers:  Authorization: Bearer <PIPELINE_SECRET>
//   Body:     { "inspection_id": "uuid" }
//   200:      { ok: true, recipient, message_id }

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
  const approveFnUrl = Deno.env.get("APPROVE_INSPECTION_URL") ?? "";
  const dashboardUrl = Deno.env.get("DASHBOARD_URL") ?? "https://flashreport.daioscove.com";
  const supabaseUrl = Deno.env.get("SUPABASE_URL");
  const serviceKey = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY");

  if (!resendKey) return json({ error: "RESEND_API_KEY not set" }, 500);
  if (!supabaseUrl || !serviceKey) {
    return json({ error: "SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY not set" }, 500);
  }

  const body = await req.json().catch(() => ({}));
  const id: string | undefined = body?.inspection_id;
  if (!id) return json({ error: "inspection_id required" }, 400);

  const supa = createClient(supabaseUrl, serviceKey);

  const { data: insp, error: err } = await supa
    .from("site_inspections")
    .select("*")
    .eq("id", id)
    .maybeSingle();
  if (err) return json({ error: "lookup failed: " + err.message }, 500);
  if (!insp) return json({ error: "inspection not found" }, 404);
  if (insp.status !== "pending_approval") {
    return json({ error: `inspection status is ${insp.status}, expected pending_approval` }, 409);
  }
  if (!insp.approver_user_id || !insp.approval_token) {
    return json({ error: "missing approver_user_id or approval_token" }, 500);
  }

  // Look up approver email via RPC (security-definer, reads auth.users)
  const { data: approvers } = await supa.rpc("list_inspection_approvers");
  const approver = (approvers ?? []).find(
    (a: { user_id: string }) => a.user_id === insp.approver_user_id,
  );
  if (!approver) return json({ error: "approver not found in list_inspection_approvers" }, 500);

  const approveUrl = `${approveFnUrl}?token=${insp.approval_token}&action=approve`;
  const rejectUrl = `${approveFnUrl}?token=${insp.approval_token}&action=reject`;
  const inspectionUrl = `${dashboardUrl}/site-inspections/${insp.id}`;

  const html = renderApprovalEmail({ insp, approveUrl, rejectUrl, inspectionUrl });
  const subject = `[APPROVAL] Site Inspection — ${insp.travel_agency ?? insp.agency_contact_person ?? "no agency"}`;

  try {
    const msgId = await sendViaResend({
      to: approver.email,
      from: fromAddress,
      subject,
      html,
      apiKey: resendKey,
    });
    return json({ ok: true, recipient: approver.email, message_id: msgId });
  } catch (e) {
    return json({ ok: false, error: String((e as Error)?.message ?? e) }, 502);
  }
});

// ─── Helpers ────────────────────────────────────────────────────────────────

function json(payload: unknown, status = 200): Response {
  return new Response(JSON.stringify(payload), {
    status,
    headers: { "content-type": "application/json" },
  });
}

function esc(s: unknown): string {
  return String(s ?? "")
    .replace(/&/g, "&amp;").replace(/</g, "&lt;")
    .replace(/>/g, "&gt;").replace(/"/g, "&quot;");
}

function yn(v: unknown): string {
  if (v === true) return "YES";
  if (v === false) return "NO";
  return "—";
}

async function sendViaResend(opts: {
  to: string; from: string; subject: string; html: string; apiKey: string;
}): Promise<string> {
  const resp = await fetch("https://api.resend.com/emails", {
    method: "POST",
    headers: {
      "Authorization": `Bearer ${opts.apiKey}`,
      "Content-Type": "application/json",
    },
    body: JSON.stringify({
      from: opts.from, to: [opts.to], subject: opts.subject, html: opts.html,
    }),
  });
  const text = await resp.text();
  if (!resp.ok) throw new Error(`Resend ${resp.status}: ${text}`);
  return JSON.parse(text).id ?? "";
}

function formRow(label: string, value: unknown): string {
  return `<tr>
    <td style="padding:6px 10px;border-bottom:1px solid #f0f0f0;color:#888;font-size:12px;width:220px;vertical-align:top;text-transform:uppercase;letter-spacing:0.5px">${esc(label)}</td>
    <td style="padding:6px 10px;border-bottom:1px solid #f0f0f0;font-size:13px">${esc(value ?? "—")}</td>
  </tr>`;
}

function formRowYN(label: string, value: unknown): string {
  return formRow(label, yn(value));
}

function renderApprovalEmail(opts: {
  insp: Record<string, unknown>;
  approveUrl: string;
  rejectUrl: string;
  inspectionUrl: string;
}): string {
  const i = opts.insp;
  return `<!doctype html>
<html lang="en"><head><meta charset="utf-8"></head>
<body style="margin:0;padding:0;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Helvetica,Arial,sans-serif;background:#f5f5f0;color:#1a1a1a">
<div style="max-width:640px;margin:0 auto;background:#ffffff">
  <div style="padding:24px 28px;border-bottom:3px solid #a38a6a">
    <div style="font-size:11px;letter-spacing:2px;color:#a38a6a;text-transform:uppercase;font-weight:600">Daios Cove</div>
    <div style="font-size:22px;font-weight:600;margin-top:4px">Site Inspection — approval required</div>
  </div>

  <div style="padding:20px 28px 4px">
    <div style="background:#fff8e6;border:1px solid #fde68a;border-radius:6px;padding:16px;margin-bottom:16px">
      <div style="font-size:14px;font-weight:600;color:#92400e;margin-bottom:6px">Review and approve</div>
      <div style="font-size:13px;color:#78350f;line-height:1.5">
        ${esc(i.created_by_name as string ?? "")} submitted a site inspection for
        <strong>${esc((i.travel_agency as string) ?? "—")}</strong> on
        <strong>${esc(i.inspection_date as string ?? "—")}</strong>${i.inspection_time ? ` at <strong>${esc(i.inspection_time as string)}</strong>` : ""}.
      </div>
      <div style="margin-top:14px">
        <a href="${opts.approveUrl}" style="display:inline-block;padding:10px 22px;background:#16a34a;color:#fff;text-decoration:none;border-radius:4px;font-size:14px;font-weight:600;margin-right:8px">Approve</a>
        <a href="${opts.rejectUrl}" style="display:inline-block;padding:10px 22px;background:#dc2626;color:#fff;text-decoration:none;border-radius:4px;font-size:14px;font-weight:600">Reject</a>
      </div>
    </div>

    <table style="width:100%;border-collapse:collapse;margin-top:8px">
      ${formRow("Reason of visit", i.reason_of_visit)}
      ${formRow("Travel agency / TO", i.travel_agency)}
      ${formRow("Source market", i.source_market)}
      ${formRowYN("Accompanied by DMC", i.accompanied_by_dmc)}
      ${formRow("Attendees", i.attendees)}
      ${formRow("Date of arrival", i.arrival_date)}
      ${formRow("Date of inspection", i.inspection_date)}
      ${formRow("Time of inspection", i.inspection_time)}
      ${formRowYN("Stay at the hotel", i.stay_at_hotel)}
      ${formRow("Number of persons", i.number_of_persons)}
      ${formRow("Country / language", i.country_language)}
      ${formRowYN("Promo material to be provided", i.promo_material_provided)}
      ${formRow("Agency / contact person", i.agency_contact_person)}
      ${formRow("Phone / mobile", i.phone_mobile)}
      ${formRow("E-mail address", i.email_address)}
      ${formRow("Inspection performed by", i.inspection_performed_by)}
      ${formRowYN("Lunch / dinner", i.lunch_dinner)}
      ${formRowYN("Spa offer", i.spa_offer)}
      ${formRow("Created by", i.created_by_name)}
      ${formRow("Issue date", i.issue_date)}
    </table>

    ${i.comments ? `<div style="margin-top:16px">
      <div style="font-size:12px;color:#888;text-transform:uppercase;letter-spacing:0.5px;margin-bottom:4px">Comments</div>
      <div style="font-size:13px;line-height:1.6;white-space:pre-wrap">${esc(i.comments as string)}</div>
    </div>` : ""}

    <div style="margin-top:24px;text-align:center">
      <a href="${opts.inspectionUrl}" style="display:inline-block;padding:8px 16px;background:transparent;color:#a38a6a;border:1px solid #a38a6a;text-decoration:none;border-radius:4px;font-size:13px">View in dashboard</a>
    </div>
  </div>
</div>
</body></html>`;
}
