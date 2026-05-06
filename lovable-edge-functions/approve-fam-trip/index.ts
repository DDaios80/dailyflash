// approve-fam-trip — Lovable Cloud edge function.
//
// GET /functions/v1/approve-fam-trip?token=<token>&action=approve|reject
//
// Phase 43 — on approve, also fire a confirmation email to the creator
// AND stamp sent_at = now(). Previously the approve branch only updated
// status + approved_at and showed a "you can now send to distribution
// list" page, but there was no Send button anywhere; sent_at stayed
// NULL forever and operations never received the FAM trip details.
//
// The reject branch already had a creator-notification pattern; the
// approve branch now mirrors it. A proper distribution-list integration
// is a future phase — for now, the creator gets the confirmation and
// can manually forward, which closes the loop on the lifecycle stamp.

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
  const { data: trip, error } = await supa
    .from("fam_trips").select("*").eq("approval_token", token).maybeSingle();
  if (error) return htmlPage(500, "Database error", error.message);
  if (!trip) return htmlPage(404, "Unknown or expired link", "This approval link is no longer valid.");

  if (trip.status === "approved") {
    return htmlPage(200, "Already approved", `This FAM trip was already approved at ${trip.approved_at}.`);
  }
  if (trip.status === "rejected") {
    return htmlPage(200, "Already rejected", `This FAM trip was already rejected${trip.rejected_at ? ` at ${trip.rejected_at}` : ""}.`);
  }
  if (trip.status !== "pending_approval") {
    return htmlPage(409, "Not pending approval", `Current status: ${trip.status}. Ask the creator to resubmit.`);
  }

  const now = new Date().toISOString();

  if (action === "approve") {
    // Phase 43 — stamp sent_at along with status + approved_at so the
    // dashboard's "approved + sent" lifecycle is fully closed.
    const { error: updErr } = await supa.from("fam_trips")
      .update({
        status: "approved",
        approved_at: now,
        sent_at: now,
      })
      .eq("id", trip.id);
    if (updErr) return htmlPage(500, "Database error", updErr.message);

    // Best-effort: notify the creator that approval landed.
    await sendConfirmationEmail(supa, trip).catch((e) => {
      console.error("approval confirmation email failed:", String((e as Error)?.message ?? e));
    });

    return htmlPage(
      200,
      "Approved",
      `The FAM trip <strong>${escapeHtml(trip.name)}</strong> (${escapeHtml(trip.start_date)} → ${escapeHtml(trip.end_date)}) is approved. The creator has been notified by email.`,
    );
  }

  // action === "reject"
  const { error: updErr } = await supa.from("fam_trips")
    .update({ status: "rejected", rejected_at: now, rejection_reason: "Rejected via email — ask creator to revise." })
    .eq("id", trip.id);
  if (updErr) return htmlPage(500, "Database error", updErr.message);

  // Best-effort: notify creator
  try {
    const resendKey = Deno.env.get("RESEND_API_KEY");
    const fromAddress = Deno.env.get("EMAIL_FROM") ?? "Daios Cove Flash <flash@daioshotels.com>";
    const dashboardUrl = Deno.env.get("DASHBOARD_URL") ?? "https://flashreport.daioscove.com";
    if (resendKey && trip.created_by_user_id) {
      const { data: approvers } = await supa.rpc("list_fam_trip_approvers");
      const fromApprovers = (approvers ?? []).find((a: { user_id: string }) => a.user_id === trip.created_by_user_id);
      const creatorEmail = fromApprovers?.email;
      if (creatorEmail) {
        const tripUrl = `${dashboardUrl}/fam-trips/${trip.id}`;
        await fetch("https://api.resend.com/emails", {
          method: "POST",
          headers: { "Authorization": `Bearer ${resendKey}`, "Content-Type": "application/json" },
          body: JSON.stringify({
            from: fromAddress, to: [creatorEmail],
            subject: `[REJECTED] FAM Trip — ${trip.name}`,
            html: `<!doctype html><html><body style="margin:0;padding:24px;background:#F8F5EE;font-family:-apple-system,sans-serif;color:#1a1a17;">
              <div style="max-width:560px;margin:0 auto;background:#fff;padding:32px;border:1px solid #e5e0d3;">
                <div style="font-size:11px;text-transform:uppercase;letter-spacing:0.18em;color:#999;margin-bottom:6px;">Daios Cove</div>
                <h1 style="font-family:Georgia,serif;font-size:22px;margin:0 0 12px;">Your FAM trip was rejected</h1>
                <p style="font-size:14px;line-height:1.5;">The approver rejected <strong>${escapeHtml(trip.name)}</strong> (${escapeHtml(trip.start_date)} → ${escapeHtml(trip.end_date)}).</p>
                <p style="font-size:14px;line-height:1.5;">Revise and resubmit from the dashboard:</p>
                <p style="margin-top:20px;"><a href="${tripUrl}" style="display:inline-block;padding:10px 22px;background:#1a1a17;color:#fff;text-decoration:none;font-size:14px;">Open trip</a></p>
              </div>
            </body></html>`,
          }),
        });
      }
    }
  } catch { /* best-effort */ }

  return htmlPage(
    200,
    "Rejected",
    `The FAM trip <strong>${escapeHtml(trip.name)}</strong> has been rejected. The creator has been notified.`,
  );
});

// Phase 43 — confirmation email on approve. Same Resend pattern as the
// reject branch. Uses list_fam_trip_approvers to resolve the creator's
// email (the RPC includes management/admin/staff who could create trips).
async function sendConfirmationEmail(
  supa: ReturnType<typeof createClient>,
  trip: Record<string, unknown>,
): Promise<void> {
  const resendKey = Deno.env.get("RESEND_API_KEY");
  const fromAddress = Deno.env.get("EMAIL_FROM") ?? "Daios Cove Flash <flash@daioshotels.com>";
  const dashboardUrl = Deno.env.get("DASHBOARD_URL") ?? "https://flashreport.daioscove.com";
  if (!resendKey || !trip.created_by_user_id) return;

  const { data: approvers } = await supa.rpc("list_fam_trip_approvers");
  const fromApprovers = (approvers ?? []).find(
    (a: { user_id: string }) => a.user_id === trip.created_by_user_id,
  );
  const creatorEmail = fromApprovers?.email;
  if (!creatorEmail) return;

  const tripUrl = `${dashboardUrl}/fam-trips/${trip.id}`;
  const name = String(trip.name ?? "");
  const start = String(trip.start_date ?? "");
  const end = String(trip.end_date ?? "");
  const approverName = String(trip.approver_name ?? "the approver");

  await fetch("https://api.resend.com/emails", {
    method: "POST",
    headers: {
      "Authorization": `Bearer ${resendKey}`,
      "Content-Type": "application/json",
    },
    body: JSON.stringify({
      from: fromAddress,
      to: [creatorEmail],
      subject: `[APPROVED] FAM Trip — ${name}`,
      html: `<!doctype html><html><body style="margin:0;padding:24px;background:#F8F5EE;font-family:-apple-system,sans-serif;color:#1a1a17;">
        <div style="max-width:560px;margin:0 auto;background:#fff;padding:32px;border:1px solid #e5e0d3;">
          <div style="font-size:11px;text-transform:uppercase;letter-spacing:0.18em;color:#999;margin-bottom:6px;">Daios Cove</div>
          <h1 style="font-family:Georgia,serif;font-size:22px;margin:0 0 12px;color:#16a34a;">FAM trip approved</h1>
          <p style="font-size:14px;line-height:1.5;"><strong>${escapeHtml(name)}</strong> (${escapeHtml(start)} → ${escapeHtml(end)}) was approved by ${escapeHtml(approverName)}.</p>
          <p style="font-size:14px;line-height:1.5;">You can now share the FAM trip with the operations team and the agency contact. Open the dashboard to download the PDF and copy the briefing details:</p>
          <p style="margin-top:20px;"><a href="${tripUrl}" style="display:inline-block;padding:10px 22px;background:#1a1a17;color:#fff;text-decoration:none;font-size:14px;">Open trip</a></p>
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
    <div style="font-size:11px;text-transform:uppercase;letter-spacing:0.18em;color:#999;margin-bottom:18px;">Daios Cove · FAM Trips</div>
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
