// ingest-fam-trip-from-onedrive — Lovable Cloud edge function.
//
// Phase 28. The Python cron pipeline lists FAM TRIPS PDFs from OneDrive,
// parses name + date range from the filename, and POSTs each one here.
// We:
//   1. Dedup by pdf_filename → skip if already imported
//   2. Decode the base64 PDF, upload to fam-trip-pdfs storage
//   3. Insert a fam_trips row with status='pending_approval'
//   4. Fire the existing parse-fam-trip-itinerary edge function so the
//      itinerary_by_day JSONB lands before an admin reviews
//   5. Return { ok, trip_id, skipped, parsed }
//
// Auth: Bearer <PIPELINE_SECRET> — same as ingest-flash-report and
// parse-fam-trip-itinerary. No caller JWT.
//
// Contract:
//   POST /functions/v1/ingest-fam-trip-from-onedrive
//   Body: {
//     pdf_filename: string,
//     pdf_base64: string,           // raw base64, no data: prefix
//     pdf_size_bytes: number,
//     name: string,                 // parsed from filename, e.g. "GOSSIP+"
//     start_date: string,           // YYYY-MM-DD
//     end_date: string,             // YYYY-MM-DD
//     created_by_user_id: string,   // UUID — env on the Python side
//   }

import { createClient } from "jsr:@supabase/supabase-js@2";

const CORS_HEADERS = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Methods": "POST, OPTIONS",
  "Access-Control-Allow-Headers": "authorization, content-type",
};

Deno.serve(async (req) => {
  if (req.method === "OPTIONS") {
    return new Response(null, { status: 204, headers: CORS_HEADERS });
  }
  if (req.method !== "POST") return json({ error: "POST only" }, 405);

  const secret = Deno.env.get("PIPELINE_SECRET");
  const auth = req.headers.get("authorization") ?? "";
  if (!secret || auth !== `Bearer ${secret}`) {
    return json({ error: "unauthorized" }, 401);
  }

  const supabaseUrl = Deno.env.get("SUPABASE_URL");
  const serviceKey  = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY");
  if (!supabaseUrl || !serviceKey) {
    return json({ error: "Supabase env missing" }, 500);
  }

  const body = await req.json().catch(() => ({}));
  const pdfFilename: string  = (body?.pdf_filename ?? "").toString();
  const pdfBase64: string    = (body?.pdf_base64 ?? "").toString();
  const pdfSizeBytes: number = Number(body?.pdf_size_bytes ?? 0);
  const name: string         = (body?.name ?? "").toString().trim();
  const startDate: string    = (body?.start_date ?? "").toString();
  const endDate: string      = (body?.end_date ?? "").toString();
  const createdBy: string    = (body?.created_by_user_id ?? "").toString();
  // Optional — defaults to the creator (admin imports + approves their own
  // OneDrive uploads). If you want a separate approver, pass it explicitly.
  const approverId: string   = (body?.approver_user_id ?? createdBy).toString();

  if (!pdfFilename || !pdfBase64 || !name || !startDate || !endDate || !createdBy) {
    return json({ error: "missing required fields" }, 400);
  }

  const supa = createClient(supabaseUrl, serviceKey);

  // ── 1. Dedup ─────────────────────────────────────────────────────────
  const { data: existing, error: dupErr } = await supa
    .from("fam_trips")
    .select("id, status")
    .eq("pdf_filename", pdfFilename)
    .maybeSingle();
  if (dupErr) return json({ error: `dedup: ${dupErr.message}` }, 500);
  if (existing) {
    return json({ ok: true, skipped: true, trip_id: existing.id, status: existing.status });
  }

  // ── 2. Storage upload ────────────────────────────────────────────────
  const tripId = crypto.randomUUID();
  const storagePath = `auto/${tripId}.pdf`;

  const pdfBytes = base64ToUint8(pdfBase64);
  const { error: upErr } = await supa
    .storage
    .from("fam-trip-pdfs")
    .upload(storagePath, pdfBytes, {
      contentType: "application/pdf",
      upsert: false,
    });
  if (upErr) return json({ error: `storage: ${upErr.message}` }, 500);

  // ── 3. Look up the approver and validate they're admin/management ────
  // Mirrors submit_fam_trip_for_approval RPC — without this the approval
  // email function bails with "approver not in list_fam_trip_approvers".
  const { data: approverRow, error: appErr } = await supa
    .from("user_roles")
    .select("user_id, role")
    .eq("user_id", approverId)
    .in("role", ["admin", "management"])
    .maybeSingle();
  if (appErr || !approverRow) {
    await supa.storage.from("fam-trip-pdfs").remove([storagePath]);
    return json({ error: "approver_user_id must have role admin or management" }, 400);
  }

  // Pull the approver's display name from auth.users (best-effort)
  let approverName: string | null = null;
  try {
    const { data: authUser } = await supa.auth.admin.getUserById(approverId);
    const meta = (authUser?.user?.user_metadata as Record<string, unknown> | undefined) ?? {};
    approverName = (meta.full_name as string) || (meta.name as string) || authUser?.user?.email || null;
  } catch (_e) {
    approverName = null;
  }

  // Generate the approval token (same shape as submit_fam_trip_for_approval RPC)
  const approvalToken = crypto.randomUUID().replace(/-/g, "");

  // ── 4. Insert fam_trips row with approver + token pre-filled ─────────
  const { data: inserted, error: insErr } = await supa
    .from("fam_trips")
    .insert({
      id: tripId,
      name,
      start_date: startDate,
      end_date: endDate,
      pdf_path: storagePath,
      pdf_filename: pdfFilename,
      pdf_size_bytes: pdfSizeBytes,
      created_by_user_id: createdBy,
      created_by_name: "OneDrive auto-import",
      approver_user_id: approverId,
      approver_name: approverName,
      approval_token: approvalToken,
      status: "pending_approval",
      submitted_at: new Date().toISOString(),
    })
    .select("id")
    .single();
  if (insErr) {
    await supa.storage.from("fam-trip-pdfs").remove([storagePath]);
    return json({ error: `insert: ${insErr.message}` }, 500);
  }

  // ── 5. Fire parse-fam-trip-itinerary ─────────────────────────────────
  let parsed = false;
  try {
    const parseUrl = `${supabaseUrl}/functions/v1/parse-fam-trip-itinerary`;
    const r = await fetch(parseUrl, {
      method: "POST",
      headers: {
        "content-type": "application/json",
        "authorization": `Bearer ${secret}`,
      },
      body: JSON.stringify({ trip_id: inserted.id }),
    });
    parsed = r.ok;
  } catch (_e) {
    parsed = false;
  }

  // ── 6. Fire send-fam-trip-approval ───────────────────────────────────
  // This is the email approver receives with Approve/Reject buttons + PDF.
  let emailed = false;
  try {
    const emailUrl = `${supabaseUrl}/functions/v1/send-fam-trip-approval`;
    const r = await fetch(emailUrl, {
      method: "POST",
      headers: {
        "content-type": "application/json",
        "authorization": `Bearer ${secret}`,
      },
      body: JSON.stringify({ trip_id: inserted.id }),
    });
    emailed = r.ok;
    if (!r.ok) {
      console.error("approval email failed:", r.status, await r.text().catch(() => ""));
    }
  } catch (e) {
    console.error("approval email exception:", String((e as Error)?.message ?? e));
    emailed = false;
  }

  return json({
    ok: true,
    trip_id: inserted.id,
    skipped: false,
    parsed,
    emailed,
  });
});

function base64ToUint8(b64: string): Uint8Array {
  const bin = atob(b64);
  const out = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
  return out;
}

function json(p: unknown, status = 200): Response {
  return new Response(JSON.stringify(p), {
    status,
    headers: { "content-type": "application/json", ...CORS_HEADERS },
  });
}
