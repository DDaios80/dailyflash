// ingest-site-inspection-from-onedrive — Lovable Cloud edge function.
//
// Phase 44. Mirror of ingest-fam-trip-from-onedrive but for the
// site_inspections table. Python's site_inspection_sync.py lists PDFs
// from DailyFlash/SITE INSPECTIONS/, parses travel_agency +
// inspection_date from the filename, and POSTs each one here.
//
// We:
//   1. Dedup by onedrive_item_id (or attachment_path fallback)
//   2. Decode the base64 PDF, upload to site-inspection-pdfs storage
//   3. Validate approver, generate approval token, insert site_inspections
//      row with status='pending_approval'
//   4. Fire send-site-inspection-approval (TODO — see Phase 43)
//   5. Return { ok, inspection_id, skipped, emailed }
//
// Auth: Bearer <PIPELINE_SECRET>.
//
// Phase 45 — DB trigger auto-reassigns approver from d.daios → Thelxi
// at INSERT time, so we don't need to know Thelxi's UUID here.

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
  const pdfFilename: string    = (body?.pdf_filename ?? "").toString();
  const pdfBase64: string      = (body?.pdf_base64 ?? "").toString();
  const pdfSizeBytes: number   = Number(body?.pdf_size_bytes ?? 0);
  const travelAgency: string   = (body?.travel_agency ?? "").toString().trim();
  const inspectionDate: string = (body?.inspection_date ?? "").toString();
  const onedriveItemId: string = (body?.onedrive_item_id ?? "").toString();
  const onedriveEtag: string   = (body?.onedrive_etag ?? "").toString();
  const createdBy: string      = (body?.created_by_user_id ?? "").toString();
  const approverId: string     = (body?.approver_user_id ?? createdBy).toString();

  if (!pdfFilename || !pdfBase64 || !travelAgency || !inspectionDate || !createdBy) {
    return json({ error: "missing required fields" }, 400);
  }

  const supa = createClient(supabaseUrl, serviceKey);

  // ── 1. Dedup ──────────────────────────────────────────────
  // Prefer onedrive_item_id (stable across renames); fall back to
  // onedrive_filename for legacy rows imported before the column existed.
  let dedupRow: { id: string; status: string } | null = null;
  if (onedriveItemId) {
    const { data, error } = await supa
      .from("site_inspections")
      .select("id, status")
      .eq("onedrive_item_id", onedriveItemId)
      .maybeSingle();
    if (error) return json({ error: `dedup-by-item-id: ${error.message}` }, 500);
    dedupRow = data;
  }
  if (!dedupRow) {
    const { data, error } = await supa
      .from("site_inspections")
      .select("id, status")
      .eq("onedrive_filename", pdfFilename)
      .maybeSingle();
    if (error) return json({ error: `dedup-by-filename: ${error.message}` }, 500);
    dedupRow = data;
  }
  if (dedupRow) {
    return json({
      ok: true,
      skipped: true,
      inspection_id: dedupRow.id,
      status: dedupRow.status,
    });
  }

  // ── 2. Storage upload ─────────────────────────────────────
  const inspectionId = crypto.randomUUID();
  const storagePath = `auto/${inspectionId}.pdf`;

  const pdfBytes = base64ToUint8(pdfBase64);
  const { error: upErr } = await supa
    .storage
    .from("site-inspection-pdfs")
    .upload(storagePath, pdfBytes, {
      contentType: "application/pdf",
      upsert: false,
    });
  if (upErr) return json({ error: `storage: ${upErr.message}` }, 500);

  // ── 3. Validate approver ──────────────────────────────────
  const { data: approverRow, error: appErr } = await supa
    .from("user_roles")
    .select("user_id, role")
    .eq("user_id", approverId)
    .in("role", ["admin", "management"])
    .maybeSingle();
  if (appErr || !approverRow) {
    await supa.storage.from("site-inspection-pdfs").remove([storagePath]);
    return json({ error: "approver_user_id must have role admin or management" }, 400);
  }

  let approverName: string | null = null;
  try {
    const { data: authUser } = await supa.auth.admin.getUserById(approverId);
    const meta = (authUser?.user?.user_metadata as Record<string, unknown> | undefined) ?? {};
    approverName = (meta.full_name as string) || (meta.name as string) || authUser?.user?.email || null;
  } catch (_e) {
    approverName = null;
  }

  const approvalToken = crypto.randomUUID().replace(/-/g, "");

  // ── 4. Insert site_inspections row ────────────────────────
  // The Phase 45 BEFORE INSERT trigger will swap approver_user_id from
  // d.daios → Thelxi automatically if the default is used.
  const { data: inserted, error: insErr } = await supa
    .from("site_inspections")
    .insert({
      id: inspectionId,
      travel_agency: travelAgency,
      inspection_date: inspectionDate,
      reason_of_visit: "site_inspection",
      attachment_path: storagePath,
      onedrive_item_id: onedriveItemId || null,
      onedrive_etag: onedriveEtag || null,
      onedrive_filename: pdfFilename,
      onedrive_last_modified: new Date().toISOString(),
      last_synced_at: new Date().toISOString(),
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
    await supa.storage.from("site-inspection-pdfs").remove([storagePath]);
    return json({ error: `insert: ${insErr.message}` }, 500);
  }

  // ── 5. Fire send-site-inspection-approval ─────────────────
  // Mirrors the pattern from ingest-fam-trip-from-onedrive. Requires the
  // edge function send-site-inspection-approval to exist. Until then,
  // emails won't fire on first import — operations team can approve via
  // the dashboard manually (the row IS visible there because the Phase 45
  // trigger reassigned approver to Thelxi).
  let emailed = false;
  try {
    const emailUrl = `${supabaseUrl}/functions/v1/send-site-inspection-approval`;
    const r = await fetch(emailUrl, {
      method: "POST",
      headers: {
        "content-type": "application/json",
        "authorization": `Bearer ${secret}`,
      },
      body: JSON.stringify({ inspection_id: inserted.id }),
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
    inspection_id: inserted.id,
    skipped: false,
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
