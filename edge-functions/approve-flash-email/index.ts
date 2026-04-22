// approve-flash-email — Lovable Cloud edge function.
//
// Endpoint Thelxi hits when she clicks "Approve" or "Reject" in the preview
// email. Validates the token, updates flash_email_approvals, and — on
// approve — fires the full fanout by calling send-flash-email.
//
// Contract:
//   GET /functions/v1/approve-flash-email?token=<uuid>&action=approve|reject
//
// Returns a minimal HTML confirmation page (rendered in the browser tab the
// link opens).

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
  const sendFlashEmailUrl = Deno.env.get("SEND_FLASH_EMAIL_URL");
  const pipelineSecret = Deno.env.get("PIPELINE_SECRET");

  if (!supabaseUrl || !serviceKey) {
    return htmlPage(500, "Server misconfigured", "Supabase env vars missing.");
  }
  if (!sendFlashEmailUrl || !pipelineSecret) {
    return htmlPage(500, "Server misconfigured", "SEND_FLASH_EMAIL_URL or PIPELINE_SECRET not set.");
  }

  const supa = createClient(supabaseUrl, serviceKey);

  // 1. Look up the approval row by token
  const { data: approval, error } = await supa
    .from("flash_email_approvals")
    .select("*")
    .eq("approval_token", token)
    .maybeSingle();
  if (error) return htmlPage(500, "Database error", error.message);
  if (!approval) return htmlPage(404, "Unknown or expired link", "This approval link is no longer valid.");
  if (approval.fanned_out_at) {
    return htmlPage(200, "Already sent", `The flash for <strong>${approval.report_date}</strong> was already approved and sent at ${approval.fanned_out_at}.`);
  }
  if (approval.rejected_at) {
    return htmlPage(200, "Already rejected", `The flash for <strong>${approval.report_date}</strong> was rejected at ${approval.rejected_at}.`);
  }
  if (approval.approved_at && action === "approve") {
    // Idempotent: already approved, re-clicking shouldn't double-send.
    return htmlPage(200, "Already approved", `The flash for <strong>${approval.report_date}</strong> was already approved. If the fanout didn't run, contact the admin.`);
  }

  const now = new Date().toISOString();
  const approverEmail = Deno.env.get("APPROVER_EMAIL") ??
    "thelxi.smyrnaki@daioshotels.com";

  if (action === "reject") {
    const { error: rejErr } = await supa
      .from("flash_email_approvals")
      .update({ rejected_at: now, rejected_by: approverEmail })
      .eq("report_date", approval.report_date);
    if (rejErr) return htmlPage(500, "Database error", rejErr.message);
    return htmlPage(
      200,
      "Rejected",
      `The flash for <strong>${approval.report_date}</strong> will not be sent. You can re-trigger from the Lovable dashboard if needed.`,
    );
  }

  // action === "approve"
  const { error: apprErr } = await supa
    .from("flash_email_approvals")
    .update({ approved_at: now, approved_by: approverEmail })
    .eq("report_date", approval.report_date);
  if (apprErr) return htmlPage(500, "Database error", apprErr.message);

  // Fire the fanout (in-process HTTP call — blocks until done, which is fine
  // for a ~30s synchronous operation on ~60 recipients).
  let fanoutResult = "";
  try {
    const resp = await fetch(sendFlashEmailUrl, {
      method: "POST",
      headers: {
        "Authorization": `Bearer ${pipelineSecret}`,
        "Content-Type": "application/json",
      },
      body: JSON.stringify({
        mode: "fanout",
        date: approval.report_date,
        approval_token: token,
      }),
    });
    const text = await resp.text();
    if (!resp.ok) {
      return htmlPage(
        500,
        "Fanout failed",
        `Approval recorded but the fanout returned HTTP ${resp.status}:<br><pre style="white-space:pre-wrap;font-size:12px;color:#666">${escapeHtml(text.slice(0, 500))}</pre>`,
      );
    }
    try {
      const parsed = JSON.parse(text);
      fanoutResult = `Sent to <strong>${parsed.sent}</strong> recipients` +
        (parsed.failed > 0 ? ` (${parsed.failed} failed)` : "") +
        (parsed.skipped > 0 ? `, ${parsed.skipped} skipped` : "") + ".";
    } catch {
      fanoutResult = "Sent — fanout returned non-JSON response.";
    }
  } catch (e) {
    return htmlPage(
      500,
      "Fanout failed",
      `Approval recorded but the HTTP call to send-flash-email failed: ${(e as Error)?.message ?? e}`,
    );
  }

  return htmlPage(
    200,
    "Approved &amp; sent",
    `The flash for <strong>${approval.report_date}</strong> has been sent. ${fanoutResult}`,
  );
});

function htmlPage(status: number, heading: string, body: string): Response {
  const color = status >= 500 ? "#dc2626" : status >= 400 ? "#f59e0b" : "#16a34a";
  const html = `<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Daily Flash — ${heading.replace(/<[^>]+>/g, "")}</title>
</head>
<body style="margin:0;padding:40px 20px;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Helvetica,Arial,sans-serif;background:#f5f5f0;color:#1a1a1a">
<div style="max-width:540px;margin:0 auto;background:#ffffff;border-radius:8px;overflow:hidden;box-shadow:0 1px 3px rgba(0,0,0,0.08)">
  <div style="padding:24px 28px;border-bottom:3px solid ${color}">
    <div style="font-size:11px;letter-spacing:2px;color:#a38a6a;text-transform:uppercase;font-weight:600">Daios Cove · Daily Flash</div>
    <div style="font-size:22px;font-weight:600;margin-top:4px;color:${color}">${heading}</div>
  </div>
  <div style="padding:20px 28px 28px;font-size:14px;line-height:1.6;color:#333">
    ${body}
  </div>
  <div style="padding:16px 28px;background:#fafaf6;font-size:11px;color:#888;text-align:center">
    You can close this window.
  </div>
</div>
</body>
</html>`;
  return new Response(html, {
    status,
    headers: { "content-type": "text/html; charset=utf-8" },
  });
}

function escapeHtml(s: unknown): string {
  return String(s ?? "")
    .replace(/&/g, "&amp;").replace(/</g, "&lt;")
    .replace(/>/g, "&gt;").replace(/"/g, "&quot;");
}
