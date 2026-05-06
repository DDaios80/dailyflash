// approve-site-inspection — Lovable Cloud edge function.
//
// GET /functions/v1/approve-site-inspection?token=<token>&action=approve|reject
//
// Phase 43 mirror of approve-fam-trip for the site_inspections table.
// On approve, stamps sent_at = now() and emails the creator. On reject,
// notifies the creator with the rejection.
//
// If a site-inspection approve handler already exists in Lovable, replace
// it with this version (which fixes the sent_at NULL bug). If none
// exists, create it as a new edge function.

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

    await sendConfirmationEmail(supa, inspection).catch((e) => {
      console.error("approval confirmation email failed:", String((e as Error)?.message ?? e));
    });

    return htmlPage(
      200,
      "Approved",
      `The site inspection for <strong>${escapeHtml(inspection.travel_agency)}</strong> on ${escapeHtml(inspection.inspection_date)} is approved. The creator has been notified by email.`,
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
  // Try the FAM trip approvers RPC first (likely covers the same admin/management set).
  // Fall back to auth.admin.getUserById.
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

async function sendConfirmationEmail(
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
  const approverName = String(inspection.approver_name ?? "the approver");

  await fetch("https://api.resend.com/emails", {
    method: "POST",
    headers: {
      "Authorization": `Bearer ${resendKey}`,
      "Content-Type": "application/json",
    },
    body: JSON.stringify({
      from: fromAddress,
      to: [creatorEmail],
      subject: `[APPROVED] Site Inspection — ${agency}`,
      html: `<!doctype html><html><body style="margin:0;padding:24px;background:#F8F5EE;font-family:-apple-system,sans-serif;color:#1a1a17;">
        <div style="max-width:560px;margin:0 auto;background:#fff;padding:32px;border:1px solid #e5e0d3;">
          <div style="font-size:11px;text-transform:uppercase;letter-spacing:0.18em;color:#999;margin-bottom:6px;">Daios Cove</div>
          <h1 style="font-family:Georgia,serif;font-size:22px;margin:0 0 12px;color:#16a34a;">Site inspection approved</h1>
          <p style="font-size:14px;line-height:1.5;">The site inspection for <strong>${escapeHtml(agency)}</strong> on ${escapeHtml(date)} was approved by ${escapeHtml(approverName)}.</p>
          <p style="font-size:14px;line-height:1.5;">You can now share the inspection details with the operations team. Open the dashboard:</p>
          <p style="margin-top:20px;"><a href="${inspUrl}" style="display:inline-block;padding:10px 22px;background:#1a1a17;color:#fff;text-decoration:none;font-size:14px;">Open inspection</a></p>
        </div>
      </body></html>`,
    }),
  });
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
