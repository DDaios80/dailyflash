// reissue-flash — Phase 23 edge function.
//
// Admin-only endpoint that orchestrates a full reissue:
//
//   1. Preview mode: returns a cheap DB-only diff so the admin can see
//      what would change before committing to the pipeline run.
//
//   2. Confirm mode:
//        a. Writes a flash_reissue_log row (status=running)
//        b. POSTs the Python webhook on Railway (fires src/cron.py)
//        c. Polls flash_reports.updated_at (max 90s) for the target report_date
//        d. POSTs send-flash-email mode=preview to fire the re-issue email
//        e. Finalises the log row with status=ok|failed|timeout
//
// Contract
//   POST /functions/v1/reissue-flash
//   Headers: Authorization: Bearer <CALLER_JWT>
//   Body:
//     { mode: "preview" }                      → returns flash_reissue_preview RPC output
//     { mode: "confirm", diff: <preview obj> } → performs the reissue
//
// Env vars
//   SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY, PIPELINE_SECRET,
//   REISSUE_WEBHOOK_URL (Railway public domain /reissue),
//   SEND_FLASH_EMAIL_URL (same one cron uses)

import { createClient } from "jsr:@supabase/supabase-js@2";

Deno.serve(async (req) => {
  if (req.method !== "POST") return json({ error: "POST only" }, 405);

  const supabaseUrl = Deno.env.get("SUPABASE_URL");
  const serviceKey  = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY");
  const pipelineSecret = Deno.env.get("PIPELINE_SECRET");
  const webhookUrl = Deno.env.get("REISSUE_WEBHOOK_URL");
  const sendFlashEmailUrl = Deno.env.get("SEND_FLASH_EMAIL_URL");
  if (!supabaseUrl || !serviceKey) return json({ error: "Supabase env missing" }, 500);
  if (!pipelineSecret) return json({ error: "PIPELINE_SECRET not set" }, 500);
  if (!webhookUrl) return json({ error: "REISSUE_WEBHOOK_URL not set" }, 500);
  if (!sendFlashEmailUrl) return json({ error: "SEND_FLASH_EMAIL_URL not set" }, 500);

  // Verify caller is an admin via their JWT
  const callerJwt = (req.headers.get("authorization") ?? "").replace(/^Bearer\s+/i, "");
  if (!callerJwt) return json({ error: "authorization header required" }, 401);

  const supaCaller = createClient(supabaseUrl, serviceKey, {
    global: { headers: { Authorization: `Bearer ${callerJwt}` } },
  });
  const { data: caller } = await supaCaller.auth.getUser();
  if (!caller?.user?.id) return json({ error: "invalid caller JWT" }, 401);

  const supa = createClient(supabaseUrl, serviceKey);
  const { data: roleRow } = await supa
    .from("user_roles").select("role").eq("user_id", caller.user.id).maybeSingle();
  if (roleRow?.role !== "admin") return json({ error: "admin only" }, 403);

  const body = await req.json().catch(() => ({}));
  const mode: string = body?.mode ?? "preview";

  try {
    if (mode === "preview") {
      return await handlePreview(supaCaller);
    }
    if (mode === "confirm") {
      return await handleConfirm({
        supa, supaCaller, callerJwt,
        diff: body.diff ?? {},
        pipelineSecret,
        webhookUrl,
        sendFlashEmailUrl,
      });
    }
    return json({ error: `unknown mode: ${mode}` }, 400);
  } catch (e) {
    return json({ error: String((e as Error)?.message ?? e).slice(0, 500) }, 500);
  }
});


async function handlePreview(supaCaller: any): Promise<Response> {
  // Use caller's JWT so the RPC's can_admin() passes in its own context.
  const { data, error } = await supaCaller.rpc("flash_reissue_preview");
  if (error) return json({ error: error.message }, 400);
  return json({ ok: true, preview: data });
}


async function handleConfirm(opts: {
  supa: any; supaCaller: any; callerJwt: string;
  diff: Record<string, unknown>;
  pipelineSecret: string; webhookUrl: string; sendFlashEmailUrl: string;
}): Promise<Response> {
  // ── 1. Start the log row (admin JWT) ────────────────────────────────
  const reportDate = (opts.diff as any)?.report_date as string | undefined;
  if (!reportDate) return json({ error: "diff.report_date required" }, 400);

  const { data: runId, error: startErr } = await opts.supaCaller
    .rpc("flash_reissue_log_start", {
      p_report_date: reportDate,
      p_diff: opts.diff,
    });
  if (startErr) return json({ error: "log_start failed: " + startErr.message }, 500);

  // Snapshot current payload timestamp so we can detect a fresh write
  const { data: before } = await opts.supa
    .from("flash_reports")
    .select("updated_at")
    .eq("report_date", reportDate)
    .maybeSingle();
  const beforeTs = before?.updated_at as string | null;

  // ── 2. Fire the Railway webhook (async, returns immediately) ──────
  let webhookAck: any = null;
  try {
    const r = await fetch(opts.webhookUrl, {
      method: "POST",
      headers: {
        "Authorization": `Bearer ${opts.pipelineSecret}`,
        "Content-Type": "application/json",
      },
      body: JSON.stringify({}),   // no flags = pipeline default (tomorrow's flash)
    });
    webhookAck = await safeJson(r);
    if (!r.ok) {
      await finishLog(opts.supa, runId,
        "failed", false, false,
        `webhook HTTP ${r.status}: ${JSON.stringify(webhookAck).slice(0, 200)}`);
      return json({ error: "pipeline webhook failed",
                    status: r.status, body: webhookAck }, 502);
    }
  } catch (e) {
    await finishLog(opts.supa, runId, "failed", false, false,
      `webhook fetch threw: ${(e as Error)?.message}`);
    return json({ error: "pipeline webhook unreachable" }, 502);
  }

  // ── 3. Poll flash_reports.updated_at for the fresh write ─────────
  const deadline = Date.now() + 90_000;   // 90s cap
  let payloadUpdated = false;
  let afterTs: string | null = null;
  while (Date.now() < deadline) {
    await sleep(2000);
    const { data: after } = await opts.supa
      .from("flash_reports")
      .select("updated_at")
      .eq("report_date", reportDate)
      .maybeSingle();
    if (after?.updated_at && after.updated_at !== beforeTs) {
      payloadUpdated = true;
      afterTs = after.updated_at;
      break;
    }
  }
  if (!payloadUpdated) {
    await finishLog(opts.supa, runId, "timeout", false, false,
      "pipeline did not update flash_reports within 90s");
    return json({
      ok: false, run_id: runId,
      error: "pipeline timed out — check Railway webhook logs",
      webhook_ack: webhookAck,
    }, 504);
  }

  // ── 4. Fire send-flash-email in preview mode ─────────────────────
  // The edge function detects the existing approved row for this date and
  // sends with the re-issue banner + [RE-ISSUED] subject prefix.
  let emailTriggered = false;
  try {
    const r = await fetch(opts.sendFlashEmailUrl, {
      method: "POST",
      headers: {
        "Authorization": `Bearer ${opts.pipelineSecret}`,
        "Content-Type": "application/json",
      },
      body: JSON.stringify({ date: reportDate, mode: "preview" }),
    });
    emailTriggered = r.ok;
    if (!r.ok) {
      const errBody = await safeJson(r);
      await finishLog(opts.supa, runId, "failed", true, false,
        `send-flash-email HTTP ${r.status}: ${JSON.stringify(errBody).slice(0, 200)}`);
      return json({
        ok: false, run_id: runId, payload_updated: true,
        error: "payload rebuilt but re-issue email failed",
        status: r.status, body: errBody,
      }, 502);
    }
  } catch (e) {
    await finishLog(opts.supa, runId, "failed", true, false,
      `send-flash-email threw: ${(e as Error)?.message}`);
    return json({ ok: false, run_id: runId, payload_updated: true,
                  error: "re-issue email unreachable" }, 502);
  }

  // ── 5. Finalise ──────────────────────────────────────────────────
  await finishLog(opts.supa, runId, "ok", true, true, null);

  return json({
    ok: true,
    run_id: runId,
    report_date: reportDate,
    payload_updated_at: afterTs,
    reissue_email_triggered: true,
    webhook_ack: webhookAck,
  });
}


async function finishLog(
  supa: any, runId: string, status: string,
  payloadUpdated: boolean, emailTriggered: boolean, error: string | null,
): Promise<void> {
  try {
    await supa.rpc("flash_reissue_log_finish", {
      p_id: runId,
      p_status: status,
      p_payload_updated: payloadUpdated,
      p_email_triggered: emailTriggered,
      p_error: error,
    });
  } catch (_) { /* best-effort */ }
}

function sleep(ms: number): Promise<void> {
  return new Promise((r) => setTimeout(r, ms));
}

async function safeJson(r: Response): Promise<any> {
  try { return await r.json(); } catch { return await r.text(); }
}

function json(p: unknown, s = 200): Response {
  return new Response(JSON.stringify(p), {
    status: s,
    headers: { "content-type": "application/json" },
  });
}
