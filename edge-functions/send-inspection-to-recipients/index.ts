// send-inspection-to-recipients — Lovable Cloud edge function.
//
// Called when the creator (or admin) clicks "Send" on an approved site
// inspection. Renders the form as a branded email and distributes it to the
// recipient list configured in app_settings.site_inspection_recipients
// (comma or newline separated). Marks the inspection as 'sent' on success.
//
// Contract:
//   POST /functions/v1/send-inspection-to-recipients
//   Headers:  Authorization: Bearer <PIPELINE_SECRET>
//   Body:     { "inspection_id": "uuid" }
//   200:      { ok: true, sent: N, failed: M, recipients: [...] }

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

  const { data: insp, error: iErr } = await supa
    .from("site_inspections").select("*").eq("id", id).maybeSingle();
  if (iErr) return json({ error: "lookup failed: " + iErr.message }, 500);
  if (!insp) return json({ error: "inspection not found" }, 404);
  if (insp.status !== "approved") {
    return json({ error: `status is ${insp.status}, expected 'approved'` }, 409);
  }

  const { data: recipRow } = await supa
    .from("app_settings").select("value").eq("key", "site_inspection_recipients").maybeSingle();
  const rawList = (recipRow?.value as string) ?? "";
  const recipients = rawList
    .split(/[\s,;\n]+/).map((s) => s.trim()).filter((s) => /^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(s));
  if (recipients.length === 0) {
    return json({ error: "no recipients configured in app_settings.site_inspection_recipients" }, 500);
  }

  const html = renderInspectionEmail(insp as Record<string, unknown>);
  const subject = `SITE INSPECTION — ${(insp.agency_contact_person as string) ?? (insp.travel_agency as string) ?? "—"}`;

  const results: { to: string; status: "sent" | "failed"; message_id?: string; error?: string }[] = [];
  for (const to of recipients) {
    try {
      const msgId = await sendViaResend({ to, from: fromAddress, subject, html, apiKey: resendKey });
      results.push({ to, status: "sent", message_id: msgId });
    } catch (e) {
      results.push({ to, status: "failed", error: String((e as Error)?.message ?? e).slice(0, 300) });
    }
  }

  const sent = results.filter((r) => r.status === "sent").length;
  const failed = results.filter((r) => r.status === "failed").length;

  // Transition to 'sent' only if at least one delivery succeeded
  if (sent > 0) {
    await supa.from("site_inspections")
      .update({ status: "sent", sent_at: new Date().toISOString() })
      .eq("id", id);
  }

  return json({
    ok: sent > 0,
    inspection_id: id,
    total_recipients: recipients.length,
    sent, failed,
    deliveries: results,
  });
});

// ─── Helpers ────────────────────────────────────────────────────────────────

function json(payload: unknown, status = 200): Response {
  return new Response(JSON.stringify(payload), {
    status, headers: { "content-type": "application/json" },
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

function row(label: string, value: unknown): string {
  return `<tr>
    <td style="padding:6px 10px;border-bottom:1px solid #e8e6dc;color:#888;font-size:11px;width:220px;vertical-align:top;text-transform:uppercase;letter-spacing:1px;font-weight:600">${esc(label)}</td>
    <td style="padding:6px 10px;border-bottom:1px solid #e8e6dc;font-size:13px;color:#1a1a1a">${esc(value ?? "—")}</td>
  </tr>`;
}

function rowYN(label: string, v: unknown): string { return row(label, yn(v)); }

function renderInspectionEmail(i: Record<string, unknown>): string {
  return `<!doctype html><html lang="en"><head><meta charset="utf-8"></head>
<body style="margin:0;padding:0;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Helvetica,Arial,sans-serif;background:#f5f5f0;color:#1a1a1a">
<div style="max-width:640px;margin:0 auto;background:#ffffff">
  <div style="padding:24px 28px;border-bottom:3px solid #a38a6a">
    <div style="font-size:11px;letter-spacing:2px;color:#a38a6a;text-transform:uppercase;font-weight:600">Daios Cove</div>
    <div style="font-size:22px;font-weight:600;margin-top:4px">Site Inspection</div>
  </div>
  <div style="padding:20px 28px 28px">
    <table style="width:100%;border-collapse:collapse">
      ${row("Reason of visit", i.reason_of_visit)}
      ${row("Travel agency / tour operator", i.travel_agency)}
      ${row("Source market", i.source_market)}
      ${rowYN("Accompanied by DMC", i.accompanied_by_dmc)}
      ${row("Attendees", i.attendees)}
      ${row("Date of arrival", i.arrival_date)}
      ${row("Date of inspection", i.inspection_date)}
      ${row("Time of inspection", i.inspection_time)}
      ${rowYN("Stay at the hotel", i.stay_at_hotel)}
      ${row("Number of persons", i.number_of_persons)}
      ${row("Country / language", i.country_language)}
      ${rowYN("Promo material to be provided", i.promo_material_provided)}
      ${row("Agency / contact person", i.agency_contact_person)}
      ${row("Phone / mobile", i.phone_mobile)}
      ${row("E-mail address", i.email_address)}
      ${row("Inspection will be performed by", i.inspection_performed_by)}
      ${rowYN("Lunch — dinner", i.lunch_dinner)}
      ${rowYN("Spa offer", i.spa_offer)}
    </table>

    ${i.comments ? `<div style="margin-top:20px">
      <div style="font-size:11px;color:#888;text-transform:uppercase;letter-spacing:1px;font-weight:600;margin-bottom:6px">Comments</div>
      <div style="font-size:13px;line-height:1.6;white-space:pre-wrap;color:#1a1a1a">${esc(i.comments as string)}</div>
    </div>` : ""}

    <table style="width:100%;border-collapse:collapse;margin-top:20px;border-top:1px solid #e8e6dc">
      ${row("Created by", i.created_by_name)}
      ${row("Approved by", i.approver_name)}
      ${row("Issue date", i.issue_date)}
    </table>
  </div>
</div>
</body></html>`;
}
