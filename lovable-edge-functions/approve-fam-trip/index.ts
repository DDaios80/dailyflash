// approve-fam-trip — Lovable Cloud edge function.
//
// GET /functions/v1/approve-fam-trip?token=<token>&action=approve|reject
//
// Phase 43 — on approve, stamp sent_at = now() so the lifecycle is closed.
// Phase 46 — on approve, fan out to the FAM trip distribution list pulled
// from app_settings.fam_trip_recipients (same source the admin manages
// via /admin → Distribution → FAM Trip Recipients). The recipient format
// is "Name <email>; Name <email>; ..." (RFC 5322 ish). Recipients see
// approved trip name, dates, and a dashboard link to grab the PDF.
//
// Reject branch unchanged: still notifies the creator with the rejection.

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

    // Phase 46 — fan out to the FAM trip distribution list.
    let recipientCount = 0;
    let recipientFailures = 0;
    try {
      const result = await fanOutDistributionEmail(supa, trip);
      recipientCount = result.sent;
      recipientFailures = result.failed;
    } catch (e) {
      console.error("FAM distribution fan-out failed:", String((e as Error)?.message ?? e));
    }

    const summary = recipientCount > 0
      ? `Distribution email sent to ${recipientCount} recipient${recipientCount === 1 ? "" : "s"}${recipientFailures > 0 ? ` (${recipientFailures} failed)` : ""}.`
      : `Distribution list could not be reached. The trip is approved; verify the recipient list in /admin and resend manually if needed.`;

    return htmlPage(
      200,
      "Approved",
      `The FAM trip <strong>${escapeHtml(trip.name)}</strong> (${escapeHtml(trip.start_date)} → ${escapeHtml(trip.end_date)}) is approved. ${summary}`,
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

// Phase 46 — parse "Name <email>; Name <email>;" format from app_settings
// and chunk-send via Resend. Returns { sent, failed } per delivery.
async function fanOutDistributionEmail(
  supa: ReturnType<typeof createClient>,
  trip: Record<string, unknown>,
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
    .eq("key", "fam_trip_recipients")
    .maybeSingle();
  const raw = (setting?.value ?? "").toString();
  const recipients = parseRecipientList(raw);
  if (recipients.length === 0) {
    console.error("fam_trip_recipients setting empty");
    return { sent: 0, failed: 0 };
  }

  const tripUrl = `${dashboardUrl}/fam-trips/${trip.id}`;
  const name = String(trip.name ?? "");
  const start = String(trip.start_date ?? "");
  const end = String(trip.end_date ?? "");
  const totalPax = trip.total_pax ?? null;
  const sourceMarket = String(trip.source_market ?? "");
  const inCharge = String(trip.in_charge ?? "");

  const html = `<!doctype html><html><body style="margin:0;padding:24px;background:#F8F5EE;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;color:#1a1a17;">
    <div style="max-width:600px;margin:0 auto;background:#fff;padding:32px;border:1px solid #e5e0d3;">
      <div style="font-size:11px;text-transform:uppercase;letter-spacing:0.18em;color:#999;margin-bottom:8px;">Daios Cove · FAM Trip</div>
      <h1 style="font-family:Georgia,serif;font-size:24px;margin:0 0 16px;">${escapeHtml(name)}</h1>
      <table style="width:100%;border-collapse:collapse;font-size:14px;line-height:1.6;">
        <tr><td style="padding:6px 0;color:#666;width:140px;">Dates</td><td>${escapeHtml(start)} → ${escapeHtml(end)}</td></tr>
        ${totalPax ? `<tr><td style="padding:6px 0;color:#666;">Pax</td><td>${escapeHtml(String(totalPax))}</td></tr>` : ""}
        ${sourceMarket ? `<tr><td style="padding:6px 0;color:#666;">Source market</td><td>${escapeHtml(sourceMarket)}</td></tr>` : ""}
        ${inCharge ? `<tr><td style="padding:6px 0;color:#666;">In charge</td><td>${escapeHtml(inCharge)}</td></tr>` : ""}
      </table>
      <p style="font-size:14px;line-height:1.5;margin-top:24px;">Full briefing, room plan, and itinerary in the dashboard:</p>
      <p style="margin-top:16px;"><a href="${tripUrl}" style="display:inline-block;padding:10px 22px;background:#1a1a17;color:#fff;text-decoration:none;font-size:14px;">Open FAM trip</a></p>
      <p style="font-size:12px;color:#999;margin-top:32px;">You're receiving this because you're on the FAM trip distribution list. To update, contact the admin team.</p>
    </div>
  </body></html>`;

  const subject = `[FAM TRIP] ${name} — ${start} → ${end}`;
  let sent = 0;
  let failed = 0;

  // Chunk to 50 recipients per Resend call (Resend default `to:` array limit).
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

// Parse "Name <email>; Name <email>;" or comma-separated or one-per-line.
// Returns lowercase, deduped, valid-looking email addresses.
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
