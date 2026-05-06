// approve-site-inspection — Lovable Cloud edge function.
//
// GET /functions/v1/approve-site-inspection?token=<token>&action=approve|reject
//
// Phase 43 — on approve, stamp sent_at = now() so the lifecycle is closed.
// Phase 46 — on approve, fan out to the site inspection distribution list
// pulled from app_settings.site_inspection_recipients (managed in /admin
// → Distribution → Site Inspection Recipients).
//
// Reject branch notifies the creator with rejection.

import { createClient } from "npm:@supabase/supabase-js@2.104.0";

Deno.serve(async (req) => {
  const url = new URL(req.url);
  const token = url.searchParams.get("token") ?? "";
  const action = url.searchParams.get("action") ?? "";
  if (!token) return htmlPage(400, "Missing token", "This link is incomplete.");
  if (action !== "approve" && action !== "reject") {
    return htmlPage(400, "Invalid action", "The link is malformed.");
  }

  const supabaseUrl = Deno.env.get("SUPABASE_URL");
  const serviceKey = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY");
  if (!supabaseUrl || !serviceKey) return htmlPage(500, "Server misconfigured", "Supabase env vars missing.");

  const supa = createClient(supabaseUrl, serviceKey);
  const { data: inspection, error } = await supa
    .from("site_inspections").select("*").eq("approval_token", token).maybeSingle();
  if (error) return htmlPage(500, "Database error", error.message);
  if (!inspection) return htmlPage(404, "Unknown or expired link", "This approval link is no longer valid.");

  if (inspection.status === "approved") {
    return htmlPage(200, "Already approved", `This site inspection was already approved at ${inspection.approved_at}.`);
  }
  if (inspection.status === "rejected") {
    return htmlPage(200, "Already rejected", `This site inspection was already rejected${inspection.rejected_at ? ` at ${inspection.rejected_at}` : ""}.`);
  }
  if (inspection.status !== "pending_approval") {
    return htmlPage(409, "Not pending approval", `Current status: ${inspection.status}. Ask the creator to resubmit.`);
  }

  const now = new Date().toISOString();

  if (action === "approve") {
    const { error: updErr } = await supa.from("site_inspections")
      .update({
        status: "approved",
        approved_at: now,
        sent_at: now,
      })
      .eq("id", inspection.id);
    if (updErr) return htmlPage(500, "Database error", updErr.message);

    let recipientCount = 0;
    let recipientFailures = 0;
    try {
      const result = await fanOutDistributionEmail(supa, inspection);
      recipientCount = result.sent;
      recipientFailures = result.failed;
    } catch (e) {
      console.error("Site inspection distribution fan-out failed:", String((e as Error)?.message ?? e));
    }

    const summary = recipientCount > 0
      ? `Distribution email sent to ${recipientCount} recipient${recipientCount === 1 ? "" : "s"}${recipientFailures > 0 ? ` (${recipientFailures} failed)` : ""}.`
      : `Distribution list could not be reached. The inspection is approved; verify the recipient list in /admin and resend manually if needed.`;

    return htmlPage(
      200,
      "Approved",
      `The site inspection for <strong>${escapeHtml(inspection.travel_agency)}</strong> on ${escapeHtml(inspection.inspection_date)} is approved. ${summary}`,
    );
  }

  // action === "reject"
  const { error: updErr } = await supa.from("site_inspections")
    .update({ status: "rejected", rejected_at: now, rejection_reason: "Rejected via email — ask creator to revise." })
    .eq("id", inspection.id);
  if (updErr) return htmlPage(500, "Database error", updErr.message);

  await sendRejectionEmail(supa, inspection).catch((e) => {
    console.error("rejection email failed:", String((e as Error)?.message ?? e));
  });

  return htmlPage(
    200,
    "Rejected",
    `The site inspection for <strong>${escapeHtml(inspection.travel_agency)}</strong> has been rejected. The creator has been notified.`,
  );
});

async function resolveCreatorEmail(
  supa: ReturnType<typeof createClient>,
  creatorUserId: string,
): Promise<string | null> {
  try {
    const { data: approvers } = await supa.rpc("list_fam_trip_approvers");
    const match = (approvers ?? []).find(
      (a: { user_id: string }) => a.user_id === creatorUserId,
    );
    if (match?.email) return match.email;
  } catch (_e) { /* fall through */ }

  try {
    const { data: authUser } = await supa.auth.admin.getUserById(creatorUserId);
    return authUser?.user?.email ?? null;
  } catch (_e) {
    return null;
  }
}

// Phase 46 — fan out approved site inspections to the distribution list.
async function fanOutDistributionEmail(
  supa: ReturnType<typeof createClient>,
  inspection: Record<string, unknown>,
): Promise<{ sent: number; failed: number }> {
  const resendKey = Deno.env.get("RESEND_API_KEY");
  const fromAddress = Deno.env.get("EMAIL_FROM") ?? "Daios Cove Flash <flash@daioshotels.com>";
  const dashboardUrl = Deno.env.get("DASHBOARD_URL") ?? "https://flashreport.daioscove.com";
  if (!resendKey) {
    console.error("RESEND_API_KEY missing — cannot fan out");
    return { sent: 0, failed: 0 };
  }

  const { data: setting } = await supa
    .from("app_settings")
    .select("value")
    .eq("key", "site_inspection_recipients")
    .maybeSingle();
  const raw = (setting?.value ?? "").toString();
  const recipients = parseRecipientList(raw);
  if (recipients.length === 0) {
    console.error("site_inspection_recipients setting empty");
    return { sent: 0, failed: 0 };
  }

  const inspUrl = `${dashboardUrl}/site-inspections/${inspection.id}`;
  const agency = String(inspection.travel_agency ?? "");
  const date = String(inspection.inspection_date ?? "");
  const time = inspection.inspection_time ? String(inspection.inspection_time) : "";
  const numberOfPersons = inspection.number_of_persons ?? null;
  const accompaniedByDmc = inspection.accompanied_by_dmc;
  const sourceMarket = String(inspection.source_market ?? "");
  const reason = String(inspection.reason_of_visit ?? "");

  const html = `<!doctype html><html><body style="margin:0;padding:24px;background:#F8F5EE;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;color:#1a1a17;">
    <div style="max-width:600px;margin:0 auto;background:#fff;padding:32px;border:1px solid #e5e0d3;">
      <div style="font-size:11px;text-transform:uppercase;letter-spacing:0.18em;color:#999;margin-bottom:8px;">Daios Cove · Site Inspection</div>
      <h1 style="font-family:Georgia,serif;font-size:24px;margin:0 0 16px;">${escapeHtml(agency)}</h1>
      <table style="width:100%;border-collapse:collapse;font-size:14px;line-height:1.6;">
        <tr><td style="padding:6px 0;color:#666;width:140px;">Inspection date</td><td>${escapeHtml(date)}${time ? " at " + escapeHtml(time) : ""}</td></tr>
        ${reason ? `<tr><td style="padding:6px 0;color:#666;">Reason</td><td>${escapeHtml(reason)}</td></tr>` : ""}
        ${numberOfPersons ? `<tr><td style="padding:6px 0;color:#666;">Number of persons</td><td>${escapeHtml(String(numberOfPersons))}</td></tr>` : ""}
        ${typeof accompaniedByDmc === "boolean" ? `<tr><td style="padding:6px 0;color:#666;">Accompanied by DMC</td><td>${accompaniedByDmc ? "Yes" : "No"}</td></tr>` : ""}
        ${sourceMarket ? `<tr><td style="padding:6px 0;color:#666;">Source market</td><td>${escapeHtml(sourceMarket)}</td></tr>` : ""}
      </table>
      <p style="font-size:14px;line-height:1.5;margin-top:24px;">Full inspection details, attendees, and attachment in the dashboard:</p>
      <p style="margin-top:16px;"><a href="${inspUrl}" style="display:inline-block;padding:10px 22px;background:#1a1a17;color:#fff;text-decoration:none;font-size:14px;">Open inspection</a></p>
      <p style="font-size:12px;color:#999;margin-top:32px;">You're receiving this because you're on the site inspection distribution list. To update, contact the admin team.</p>
    </div>
  </body></html>`;

  const subject = `[SITE INSPECTION] ${agency} — ${date}`;
  let sent = 0;
  let failed = 0;

  for (const chunk of chunked(recipients, 50)) {
    try {
      const r = await fetch("https://api.resend.com/emails", {
        method: "POST",
        headers: {
          "Authorization": `Bearer ${resendKey}`,
          "Content-Type": "application/json",
        },
        body: JSON.stringify({
          from: fromAddress,
          to: chunk,
          subject,
          html,
        }),
      });
      if (r.ok) {
        sent += chunk.length;
      } else {
        failed += chunk.length;
        console.error(`Resend chunk failed (${r.status}):`, await r.text().catch(() => ""));
      }
    } catch (e) {
      failed += chunk.length;
      console.error("Resend chunk exception:", String((e as Error)?.message ?? e));
    }
  }

  return { sent, failed };
}

async function sendRejectionEmail(
  supa: ReturnType<typeof createClient>,
  inspection: Record<string, unknown>,
): Promise<void> {
  const resendKey = Deno.env.get("RESEND_API_KEY");
  const fromAddress = Deno.env.get("EMAIL_FROM") ?? "Daios Cove Flash <flash@daioshotels.com>";
  const dashboardUrl = Deno.env.get("DASHBOARD_URL") ?? "https://flashreport.daioscove.com";
  if (!resendKey || !inspection.created_by_user_id) return;

  const creatorEmail = await resolveCreatorEmail(supa, String(inspection.created_by_user_id));
  if (!creatorEmail) return;

  const inspUrl = `${dashboardUrl}/site-inspections/${inspection.id}`;
  const agency = String(inspection.travel_agency ?? "");
  const date = String(inspection.inspection_date ?? "");

  await fetch("https://api.resend.com/emails", {
    method: "POST",
    headers: {
      "Authorization": `Bearer ${resendKey}`,
      "Content-Type": "application/json",
    },
    body: JSON.stringify({
      from: fromAddress,
      to: [creatorEmail],
      subject: `[REJECTED] Site Inspection — ${agency}`,
      html: `<!doctype html><html><body style="margin:0;padding:24px;background:#F8F5EE;font-family:-apple-system,sans-serif;color:#1a1a17;">
        <div style="max-width:560px;margin:0 auto;background:#fff;padding:32px;border:1px solid #e5e0d3;">
          <div style="font-size:11px;text-transform:uppercase;letter-spacing:0.18em;color:#999;margin-bottom:6px;">Daios Cove</div>
          <h1 style="font-family:Georgia,serif;font-size:22px;margin:0 0 12px;">Your site inspection was rejected</h1>
          <p style="font-size:14px;line-height:1.5;">The approver rejected <strong>${escapeHtml(agency)}</strong> (inspection date ${escapeHtml(date)}).</p>
          <p style="font-size:14px;line-height:1.5;">Revise and resubmit from the dashboard:</p>
          <p style="margin-top:20px;"><a href="${inspUrl}" style="display:inline-block;padding:10px 22px;background:#1a1a17;color:#fff;text-decoration:none;font-size:14px;">Open inspection</a></p>
        </div>
      </body></html>`,
    }),
  });
}

function parseRecipientList(raw: string): string[] {
  const matches = raw.match(/<([^<>\s]+@[^<>\s]+)>|(\b[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}\b)/g) ?? [];
  const out = new Set<string>();
  for (const m of matches) {
    const email = m.replace(/^</, "").replace(/>$/, "").toLowerCase().trim();
    if (email.includes("@") && email.includes(".")) out.add(email);
  }
  return Array.from(out);
}

function chunked<T>(arr: T[], n: number): T[][] {
  const out: T[][] = [];
  for (let i = 0; i < arr.length; i += n) out.push(arr.slice(i, i + n));
  return out;
}

function htmlPage(status: number, heading: string, body: string): Response {
  const color = status >= 500 ? "#dc2626" : status >= 400 ? "#f59e0b" : "#16a34a";
  const html = `<!doctype html><html><head><meta charset="utf-8"><title>${heading}</title></head>
<body style="margin:0;padding:48px 24px;background:#F8F5EE;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;color:#1a1a17;">
  <div style="max-width:520px;margin:0 auto;background:#fff;padding:40px;border:1px solid #e5e0d3;text-align:center;">
    <div style="font-size:11px;text-transform:uppercase;letter-spacing:0.18em;color:#999;margin-bottom:18px;">Daios Cove · Site Inspections</div>
    <h1 style="font-family:Georgia,serif;font-size:28px;margin:0 0 16px;color:${color};">${heading}</h1>
    <p style="font-size:15px;line-height:1.6;color:#1a1a17;">${body}</p>
    <p style="margin-top:32px;font-size:12px;color:#999;">You can close this window.</p>
  </div>
</body></html>`;
  return new Response(html, { status, headers: { "content-type": "text/html; charset=utf-8" } });
}

function escapeHtml(s: unknown): string {
  return String(s ?? "")
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
}
