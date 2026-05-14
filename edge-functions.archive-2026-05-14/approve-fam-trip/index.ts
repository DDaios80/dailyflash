// approve-fam-trip — Lovable Cloud edge function.
//
// GET /functions/v1/approve-fam-trip?token=<uuid>&action=approve|reject
// Same pattern as approve-inspection.

import { createClient } from "jsr:@supabase/supabase-js@2";

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
    const { error: updErr } = await supa.from("fam_trips")
      .update({ status: "approved", approved_at: now }).eq("id", trip.id);
    if (updErr) return htmlPage(500, "Database error", updErr.message);
    return htmlPage(
      200,
      "Approved",
      `The FAM trip <strong>${escapeHtml(trip.name)}</strong> (${escapeHtml(trip.start_date)} → ${escapeHtml(trip.end_date)}) is approved. The creator can now send it to the distribution list.`,
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
            html: `<!doctype html><html><body style="font-family:-apple-system,sans-serif;max-width:560px;margin:0 auto;padding:24px;color:#1a1a1a">
              <div style="font-size:11px;letter-spacing:2px;color:#a38a6a;text-transform:uppercase">Daios Cove</div>
              <h2 style="margin-top:4px">Your FAM trip was rejected</h2>
              <p>The approver rejected <strong>${escapeHtml(trip.name)}</strong> (${escapeHtml(trip.start_date)} → ${escapeHtml(trip.end_date)}).</p>
              <p>Revise and resubmit from the dashboard:</p>
              <p><a href="${tripUrl}" style="display:inline-block;padding:10px 20px;background:#a38a6a;color:#fff;text-decoration:none;border-radius:4px">Open trip</a></p>
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

function htmlPage(status: number, heading: string, body: string): Response {
  const color = status >= 500 ? "#dc2626" : status >= 400 ? "#f59e0b" : "#16a34a";
  const html = `<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>${heading.replace(/<[^>]+>/g, "")}</title></head><body style="margin:0;padding:40px 20px;font-family:-apple-system,sans-serif;background:#f5f5f0;color:#1a1a1a">
<div style="max-width:540px;margin:0 auto;background:#ffffff;border-radius:8px;overflow:hidden;box-shadow:0 1px 3px rgba(0,0,0,0.08)">
  <div style="padding:24px 28px;border-bottom:3px solid ${color}">
    <div style="font-size:11px;letter-spacing:2px;color:#a38a6a;text-transform:uppercase;font-weight:600">Daios Cove · FAM Trips</div>
    <div style="font-size:22px;font-weight:600;margin-top:4px;color:${color}">${heading}</div>
  </div>
  <div style="padding:20px 28px 28px;font-size:14px;line-height:1.6;color:#333">${body}</div>
  <div style="padding:16px 28px;background:#fafaf6;font-size:11px;color:#888;text-align:center">You can close this window.</div>
</div></body></html>`;
  return new Response(html, { status, headers: { "content-type": "text/html; charset=utf-8" } });
}

function escapeHtml(s: unknown): string {
  return String(s ?? "")
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
}
