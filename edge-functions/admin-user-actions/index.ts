// admin-user-actions — Lovable Cloud edge function.
//
// Single endpoint for all admin lifecycle operations on users:
//   * create_user          — create a single user + assign role + email credentials
//   * reset_password       — send a set-password link via Resend
//   * suspend              — set auth.users.banned_until
//   * unsuspend            — clear banned_until
//   * delete               — auth.admin.deleteUser (cascades via FKs)
//   * role_change          — upsert into user_roles
//   * send_credentials_batch — bulk plaintext credentials email
//
// Auth: requires the caller's Supabase JWT. The function verifies the
// caller is an admin (user_roles.role='admin') before performing any
// action.
//
// Every action writes a row to admin_user_events for audit.
//
// Contract:
//   POST /functions/v1/admin-user-actions
//   Headers:  Authorization: Bearer <CALLER_JWT>
//   Body: one of:
//     { action: "create_user",
//         email: string, full_name: string, role: text,
//         phone?: string, password?: string,         // password auto-generated if absent
//         send_credentials?: boolean,                // default true
//         subject_template?: string, body_template?: string
//     }
//     { action: "reset_password",   target_user_id: uuid }
//     { action: "suspend",          target_user_id: uuid, until?: ISO8601, reason?: string }
//     { action: "unsuspend",        target_user_id: uuid }
//     { action: "delete",           target_user_id: uuid }
//     { action: "role_change",      target_user_id: uuid, new_role: text }
//     { action: "send_credentials_batch",
//         subject_template: string,
//         body_template: string,
//         rows: [{ email, password, name? }]
//     }

import { createClient } from "jsr:@supabase/supabase-js@2";

Deno.serve(async (req) => {
  if (req.method !== "POST") return json({ error: "POST only" }, 405);

  const supabaseUrl = Deno.env.get("SUPABASE_URL");
  const serviceKey  = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY");
  const resendKey   = Deno.env.get("RESEND_API_KEY");
  const fromAddress = Deno.env.get("EMAIL_FROM") ?? "Daios Cove Flash <flash@daioshotels.com>";
  const dashboardUrl = Deno.env.get("DASHBOARD_URL") ?? "https://flashreport.daioscove.com";
  if (!supabaseUrl || !serviceKey) return json({ error: "Supabase env missing" }, 500);
  if (!resendKey) return json({ error: "RESEND_API_KEY not set" }, 500);

  // Admin auth: use the caller's JWT to identify them
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
  const action: string = body?.action ?? "";

  try {
    switch (action) {
      case "create_user":
        return await handleCreateUser({
          supa, callerId: caller.user.id,
          email: body.email, fullName: body.full_name, role: body.role,
          phone: body.phone, password: body.password,
          sendCredentials: body.send_credentials !== false,
          subjectTemplate: body.subject_template,
          bodyTemplate: body.body_template,
          resendKey, fromAddress, dashboardUrl,
        });

      case "reset_password":
        return await handleResetPassword({
          supa, callerId: caller.user.id, targetId: body.target_user_id,
          resendKey, fromAddress, dashboardUrl,
        });

      case "suspend":
        return await handleSuspend({
          supa, callerId: caller.user.id, targetId: body.target_user_id,
          until: body.until, reason: body.reason,
        });

      case "unsuspend":
        return await handleUnsuspend({
          supa, callerId: caller.user.id, targetId: body.target_user_id,
        });

      case "delete":
        return await handleDelete({
          supa, callerId: caller.user.id, targetId: body.target_user_id,
        });

      case "role_change":
        return await handleRoleChange({
          supa, callerId: caller.user.id, targetId: body.target_user_id,
          newRole: body.new_role,
        });

      case "send_credentials_batch":
        return await handleCredentialsBatch({
          supa, callerId: caller.user.id,
          subjectTemplate: body.subject_template,
          bodyTemplate: body.body_template,
          rows: body.rows,
          resendKey, fromAddress, dashboardUrl,
        });

      default:
        return json({ error: `unknown action: ${action}` }, 400);
    }
  } catch (e) {
    return json({ error: String((e as Error)?.message ?? e).slice(0, 500) }, 500);
  }
});

// ─── Safety helpers ─────────────────────────────────────────────────────────

async function ensureNotSelf(callerId: string, targetId: string) {
  if (callerId === targetId) {
    throw new Error("cannot perform this action on yourself");
  }
}

async function ensureLastAdminGuard(supa: any, targetId: string, mode: "demote" | "remove") {
  // Would the action leave us with 0 active admins?
  const { data: targetRole } = await supa
    .from("user_roles").select("role").eq("user_id", targetId).maybeSingle();
  if (targetRole?.role !== "admin") return; // not touching an admin row

  const { data: countRow } = await supa.rpc("count_active_admins");
  const n = typeof countRow === "number" ? countRow : 0;
  if (n <= 1) {
    throw new Error(`cannot ${mode} the last active admin — promote someone else first`);
  }
}

async function logEvent(
  supa: any,
  actorId: string,
  targetId: string | null,
  action: string,
  details: Record<string, unknown> = {},
): Promise<void> {
  try {
    await supa.from("admin_user_events").insert({
      actor_user_id: actorId,
      target_user_id: targetId,
      action,
      details,
    });
  } catch (_) { /* best-effort */ }
}

// ─── Action handlers ───────────────────────────────────────────────────────

async function handleCreateUser(opts: {
  supa: any; callerId: string;
  email: string; fullName: string; role: string;
  phone?: string; password?: string;
  sendCredentials: boolean;
  subjectTemplate?: string; bodyTemplate?: string;
  resendKey: string; fromAddress: string; dashboardUrl: string;
}): Promise<Response> {
  const email = String(opts.email ?? "").trim().toLowerCase();
  const fullName = String(opts.fullName ?? "").trim();
  const role = String(opts.role ?? "").trim();
  const phone = String(opts.phone ?? "").trim();

  if (!email || !/^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(email)) {
    return json({ error: "valid email required" }, 400);
  }
  if (!fullName) return json({ error: "full_name required" }, 400);
  if (!role)     return json({ error: "role required" }, 400);

  // Password: use caller-provided if present and >=8 chars, else auto-generate.
  const password = (opts.password && String(opts.password).length >= 8)
    ? String(opts.password)
    : generatePassword(16);

  // Create the auth user. email_confirm=true so they can sign in without
  // a confirmation-email round trip.
  const { data: created, error: cErr } = await opts.supa.auth.admin.createUser({
    email,
    password,
    email_confirm: true,
    phone: phone || undefined,
    user_metadata: { full_name: fullName, ...(phone ? { phone } : {}) },
  });
  if (cErr) {
    const msg = cErr.message ?? String(cErr);
    if (/already (been )?registered|already exists|duplicate/i.test(msg)) {
      return json({ error: "a user with this email already exists" }, 409);
    }
    return json({ error: "createUser failed: " + msg }, 502);
  }
  const newUserId = created?.user?.id;
  if (!newUserId) return json({ error: "createUser returned no id" }, 502);

  // Assign role
  const { error: rErr } = await opts.supa
    .from("user_roles")
    .upsert({ user_id: newUserId, role }, { onConflict: "user_id" });
  if (rErr) {
    // Roll back the auth user so we don't leave an orphan with no role.
    await opts.supa.auth.admin.deleteUser(newUserId).catch(() => {});
    return json({ error: "role assignment failed: " + rErr.message }, 502);
  }

  // Email the credentials if asked (default true)
  let messageId: string | null = null;
  let emailStatus: "sent" | "failed" | "skipped" = "skipped";
  let emailError: string | null = null;

  if (opts.sendCredentials) {
    const subjectTpl = opts.subjectTemplate
      || "Your Daios Cove Flash Report access";
    const bodyTpl = opts.bodyTemplate || DEFAULT_WELCOME_BODY;
    const subject = renderTemplate(subjectTpl, {
      email, password, name: fullName, dashboard: opts.dashboardUrl,
    });
    const renderedBody = renderTemplate(bodyTpl, {
      email, password, name: fullName, dashboard: opts.dashboardUrl,
    });
    const html = renderCredentialsEmail(renderedBody, opts.dashboardUrl);

    try {
      messageId = await sendViaResend({
        apiKey: opts.resendKey, from: opts.fromAddress, to: email, subject, html,
      });
      emailStatus = "sent";
    } catch (e) {
      emailStatus = "failed";
      emailError = String((e as Error)?.message ?? e).slice(0, 500);
    }
  }

  await logEvent(opts.supa, opts.callerId, newUserId, "create_user", {
    email, full_name: fullName, role, phone: phone || null,
    password_generated: !opts.password,
    email_status: emailStatus,
    resend_message_id: messageId,
    email_error: emailError,
  });

  return json({
    ok: true,
    user_id: newUserId,
    email,
    role,
    email_status: emailStatus,
    resend_message_id: messageId,
    ...(emailError ? { email_error: emailError } : {}),
  });
}

async function handleResetPassword(opts: {
  supa: any; callerId: string; targetId: string;
  resendKey: string; fromAddress: string; dashboardUrl: string;
}): Promise<Response> {
  if (!opts.targetId) return json({ error: "target_user_id required" }, 400);

  // Fetch target email
  const { data: targetUser, error: uErr } = await opts.supa.auth.admin.getUserById(opts.targetId);
  if (uErr || !targetUser?.user?.email) return json({ error: "target user not found" }, 404);
  const targetEmail = targetUser.user.email;

  // Generate recovery link
  const { data: linkRes, error: lErr } = await opts.supa.auth.admin.generateLink({
    type: "recovery",
    email: targetEmail,
  });
  if (lErr) return json({ error: "generateLink failed: " + lErr.message }, 502);
  const resetLink = linkRes?.properties?.action_link;
  if (!resetLink) return json({ error: "generateLink returned no link" }, 502);

  // Send email
  const subject = "Reset your Daios Cove Flash Report password";
  const html = renderResetEmail(resetLink, opts.dashboardUrl);
  let messageId: string | null = null;
  try {
    messageId = await sendViaResend({
      apiKey: opts.resendKey, from: opts.fromAddress, to: targetEmail, subject, html,
    });
  } catch (e) {
    await logEvent(opts.supa, opts.callerId, opts.targetId, "reset_password", {
      email: targetEmail, status: "failed", error: String((e as Error)?.message ?? e),
    });
    return json({ ok: false, error: String((e as Error)?.message ?? e) }, 502);
  }

  await logEvent(opts.supa, opts.callerId, opts.targetId, "reset_password", {
    email: targetEmail, status: "sent", resend_message_id: messageId,
  });
  return json({ ok: true, recipient: targetEmail, message_id: messageId });
}

async function handleSuspend(opts: {
  supa: any; callerId: string; targetId: string;
  until?: string; reason?: string;
}): Promise<Response> {
  if (!opts.targetId) return json({ error: "target_user_id required" }, 400);
  await ensureNotSelf(opts.callerId, opts.targetId);
  await ensureLastAdminGuard(opts.supa, opts.targetId, "demote");

  // Default suspension = 30 days from now
  const until = opts.until ?? new Date(Date.now() + 30 * 86400_000).toISOString();

  const { error } = await opts.supa.auth.admin.updateUserById(opts.targetId, {
    ban_duration: computeBanDuration(until),
  });
  if (error) return json({ error: "updateUserById failed: " + error.message }, 502);

  await logEvent(opts.supa, opts.callerId, opts.targetId, "suspend", {
    until, reason: opts.reason ?? null,
  });
  return json({ ok: true, target_user_id: opts.targetId, banned_until: until });
}

async function handleUnsuspend(opts: {
  supa: any; callerId: string; targetId: string;
}): Promise<Response> {
  if (!opts.targetId) return json({ error: "target_user_id required" }, 400);
  const { error } = await opts.supa.auth.admin.updateUserById(opts.targetId, {
    ban_duration: "none",
  });
  if (error) return json({ error: "updateUserById failed: " + error.message }, 502);

  await logEvent(opts.supa, opts.callerId, opts.targetId, "unsuspend", {});
  return json({ ok: true, target_user_id: opts.targetId });
}

async function handleDelete(opts: {
  supa: any; callerId: string; targetId: string;
}): Promise<Response> {
  if (!opts.targetId) return json({ error: "target_user_id required" }, 400);
  await ensureNotSelf(opts.callerId, opts.targetId);
  await ensureLastAdminGuard(opts.supa, opts.targetId, "remove");

  // Capture email before delete for audit
  const { data: targetUser } = await opts.supa.auth.admin.getUserById(opts.targetId);
  const targetEmail = targetUser?.user?.email ?? null;

  const { error } = await opts.supa.auth.admin.deleteUser(opts.targetId);
  if (error) return json({ error: "deleteUser failed: " + error.message }, 502);

  await logEvent(opts.supa, opts.callerId, opts.targetId, "delete", {
    email_at_deletion: targetEmail,
  });
  return json({ ok: true, target_user_id: opts.targetId });
}

async function handleRoleChange(opts: {
  supa: any; callerId: string; targetId: string; newRole: string;
}): Promise<Response> {
  if (!opts.targetId || !opts.newRole) {
    return json({ error: "target_user_id and new_role required" }, 400);
  }
  // Prevent removing admin from the last admin
  if (opts.newRole !== "admin") {
    await ensureLastAdminGuard(opts.supa, opts.targetId, "demote");
  }

  const { data: prev } = await opts.supa
    .from("user_roles").select("role").eq("user_id", opts.targetId).maybeSingle();

  const { error } = await opts.supa
    .from("user_roles")
    .upsert({ user_id: opts.targetId, role: opts.newRole }, { onConflict: "user_id" });
  if (error) return json({ error: "role update failed: " + error.message }, 502);

  await logEvent(opts.supa, opts.callerId, opts.targetId, "role_change", {
    from: prev?.role ?? null, to: opts.newRole,
  });
  return json({ ok: true, target_user_id: opts.targetId, new_role: opts.newRole });
}

async function handleCredentialsBatch(opts: {
  supa: any; callerId: string;
  subjectTemplate: string; bodyTemplate: string;
  rows: Array<{ email: string; password: string; name?: string }>;
  resendKey: string; fromAddress: string; dashboardUrl: string;
}): Promise<Response> {
  if (!opts.subjectTemplate || !opts.bodyTemplate) {
    return json({ error: "subject_template and body_template required" }, 400);
  }
  if (!Array.isArray(opts.rows) || opts.rows.length === 0) {
    return json({ error: "rows must be a non-empty array" }, 400);
  }

  // Create batch row
  const { data: batch, error: bErr } = await opts.supa
    .from("credential_email_batches")
    .insert({
      created_by: opts.callerId,
      subject_template: opts.subjectTemplate,
      body_template: opts.bodyTemplate,
      total: opts.rows.length,
    })
    .select("id")
    .single();
  if (bErr || !batch?.id) return json({ error: "batch create failed: " + bErr?.message }, 500);

  let sent = 0, failed = 0;

  for (const row of opts.rows) {
    const email = String(row.email ?? "").trim();
    const password = String(row.password ?? "");
    const name = String(row.name ?? "").trim();

    if (!email || !/^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(email) || !password) {
      failed++;
      await opts.supa.from("credential_email_deliveries").insert({
        batch_id: batch.id, recipient_email: email || "(empty)",
        status: "skipped", error: "invalid email or password",
      });
      continue;
    }

    const subject = renderTemplate(opts.subjectTemplate, { email, password, name });
    const body    = renderTemplate(opts.bodyTemplate,    { email, password, name });
    const html    = renderCredentialsEmail(body, opts.dashboardUrl);

    try {
      const msgId = await sendViaResend({
        apiKey: opts.resendKey, from: opts.fromAddress, to: email, subject, html,
      });
      sent++;
      await opts.supa.from("credential_email_deliveries").insert({
        batch_id: batch.id, recipient_email: email, status: "sent",
        resend_message_id: msgId,
      });
    } catch (e) {
      failed++;
      await opts.supa.from("credential_email_deliveries").insert({
        batch_id: batch.id, recipient_email: email, status: "failed",
        error: String((e as Error)?.message ?? e).slice(0, 500),
      });
    }

    // Throttle for Resend rate limits (2 req/s on free tier)
    await new Promise((r) => setTimeout(r, 120));
  }

  // Finalise batch
  await opts.supa
    .from("credential_email_batches")
    .update({
      sent, failed,
      status: failed === 0 ? "completed" : (sent === 0 ? "failed" : "completed"),
      completed_at: new Date().toISOString(),
    })
    .eq("id", batch.id);

  await logEvent(opts.supa, opts.callerId, null, "credentials_batch_sent", {
    batch_id: batch.id, total: opts.rows.length, sent, failed,
  });

  return json({ ok: true, batch_id: batch.id, total: opts.rows.length, sent, failed });
}

// ─── Helpers ────────────────────────────────────────────────────────────────

// Default template used when the admin creates a user without supplying
// their own body copy. Kept short and professional.
const DEFAULT_WELCOME_BODY = `Hello {name},

Your Daios Cove Flash Report account has been created.

  Email:    {email}
  Password: {password}

Please sign in and change your password on first login.

Dashboard: {dashboard}`;

function generatePassword(len: number): string {
  // Mixed character set excluding look-alikes (0, O, 1, l, I).
  const chars = "ABCDEFGHJKLMNPQRSTUVWXYZ" +
                "abcdefghijkmnopqrstuvwxyz" +
                "23456789" +
                "!@#$%^&*-_=+";
  const out: string[] = [];
  const buf = new Uint32Array(len);
  crypto.getRandomValues(buf);
  for (let i = 0; i < len; i++) {
    out.push(chars[buf[i] % chars.length]);
  }
  return out.join("");
}

function computeBanDuration(untilIso: string): string {
  // Supabase expects a duration string like "24h" or "720h".
  const until = new Date(untilIso).getTime();
  const now = Date.now();
  const hours = Math.max(1, Math.round((until - now) / 3_600_000));
  return `${hours}h`;
}

function renderTemplate(tpl: string, vars: Record<string, string>): string {
  return tpl
    .replace(/\{email\}/g, vars.email)
    .replace(/\{password\}/g, vars.password)
    .replace(/\{name\}/g, vars.name || "")
    .replace(/\{dashboard\}/g, vars.dashboard || "");
}

function esc(s: unknown): string {
  return String(s ?? "")
    .replace(/&/g, "&amp;").replace(/</g, "&lt;")
    .replace(/>/g, "&gt;").replace(/"/g, "&quot;");
}

async function sendViaResend(opts: {
  apiKey: string; from: string; to: string; subject: string; html: string;
}): Promise<string> {
  const resp = await fetch("https://api.resend.com/emails", {
    method: "POST",
    headers: { "Authorization": `Bearer ${opts.apiKey}`, "Content-Type": "application/json" },
    body: JSON.stringify({ from: opts.from, to: [opts.to], subject: opts.subject, html: opts.html }),
  });
  const text = await resp.text();
  if (!resp.ok) throw new Error(`Resend ${resp.status}: ${text}`);
  return JSON.parse(text).id ?? "";
}

function renderResetEmail(link: string, dashboardUrl: string): string {
  return `<!doctype html><html><body style="margin:0;padding:40px 20px;font-family:-apple-system,BlinkMacSystemFont,sans-serif;background:#f5f5f0;color:#1a1a1a">
<div style="max-width:540px;margin:0 auto;background:#ffffff;border-radius:8px;padding:28px">
  <div style="font-size:11px;letter-spacing:2px;color:#a38a6a;text-transform:uppercase;font-weight:600">Daios Cove</div>
  <h2 style="margin:4px 0 16px;font-size:20px;font-weight:600">Reset your password</h2>
  <p style="font-size:13px;line-height:1.6;color:#333">Click the button below to set a new password for the Flash Report. The link expires in 24 hours.</p>
  <p style="margin:20px 0;text-align:center"><a href="${esc(link)}" style="display:inline-block;padding:12px 28px;background:#a38a6a;color:#fff;text-decoration:none;border-radius:4px;font-size:14px;font-weight:600">Set new password</a></p>
  <p style="font-size:12px;color:#888;margin-top:20px">If you didn't request this, you can ignore this email.</p>
  <p style="font-size:11px;color:#888;margin-top:24px">Dashboard: <a href="${esc(dashboardUrl)}" style="color:#a38a6a">${esc(dashboardUrl)}</a></p>
</div>
</body></html>`;
}

function renderCredentialsEmail(body: string, dashboardUrl: string): string {
  // The body is the admin-provided template, already merged with {email}/{password}/{name}.
  // Render it preserving line breaks, inside the branded shell.
  return `<!doctype html><html><body style="margin:0;padding:40px 20px;font-family:-apple-system,BlinkMacSystemFont,sans-serif;background:#f5f5f0;color:#1a1a1a">
<div style="max-width:580px;margin:0 auto;background:#ffffff;border-radius:8px;overflow:hidden">
  <div style="padding:20px 28px;border-bottom:3px solid #a38a6a">
    <div style="font-size:11px;letter-spacing:2px;color:#a38a6a;text-transform:uppercase;font-weight:600">Daios Cove</div>
    <div style="font-size:20px;font-weight:600;margin-top:4px">Flash Report access</div>
  </div>
  <div style="padding:24px 28px">
    <div style="font-size:13px;line-height:1.65;white-space:pre-wrap;color:#1a1a1a">${esc(body)}</div>
    <p style="margin-top:24px;text-align:center">
      <a href="${esc(dashboardUrl)}" style="display:inline-block;padding:10px 22px;background:#a38a6a;color:#fff;text-decoration:none;border-radius:4px;font-size:13px;font-weight:600">Open dashboard</a>
    </p>
    <p style="margin-top:24px;font-size:11px;color:#888;line-height:1.5">
      For security, please change your password on first login and do not share these credentials.
    </p>
  </div>
</div>
</body></html>`;
}

function json(p: unknown, s = 200) {
  return new Response(JSON.stringify(p), { status: s, headers: { "content-type": "application/json" } });
}
