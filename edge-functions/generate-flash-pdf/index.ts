// generate-flash-pdf — Lovable Cloud edge function.
//
// Renders the Daily Flash one-pager as a PDF using Browserless.io (headless
// Chrome). Called by send-flash-email twice per day:
//   1. { date, include_alister: true }  → full PDF attached to Tier-A-with-
//                                          alister recipients
//   2. { date, include_alister: false } → redacted PDF (no A-lister block)
//                                          attached to everyone else
//
// Contract:
//   POST /functions/v1/generate-flash-pdf
//   Headers:  Authorization: Bearer <PIPELINE_SECRET>
//   Body:     { "date": "YYYY-MM-DD", "include_alister": boolean }
//   200:      { "pdf_base64": "..." }
//   401:      bad secret
//   500:      Browserless error / missing payload / env not set

import { createClient } from "jsr:@supabase/supabase-js@2";
import { renderFlashPdfHtml } from "./template.ts";

Deno.serve(async (req) => {
  if (req.method !== "POST") return json({ error: "POST only" }, 405);

  const secret = Deno.env.get("PIPELINE_SECRET");
  const auth = req.headers.get("authorization") ?? "";
  if (!secret || auth !== `Bearer ${secret}`) {
    return json({ error: "unauthorized" }, 401);
  }

  const browserlessKey = Deno.env.get("BROWSERLESS_API_KEY");
  const supabaseUrl = Deno.env.get("SUPABASE_URL");
  const serviceKey = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY");
  if (!browserlessKey) return json({ error: "BROWSERLESS_API_KEY not set" }, 500);
  if (!supabaseUrl || !serviceKey) return json({ error: "Supabase env missing" }, 500);

  const body = await req.json().catch(() => ({}));
  const date: string | undefined = body?.date;
  const includeAlister: boolean = !!body?.include_alister;
  if (!date) return json({ error: "date required" }, 400);

  const supa = createClient(supabaseUrl, serviceKey);

  // 1. Fetch flash payload
  const { data: flash, error: flashErr } = await supa
    .from("flash_reports")
    .select("payload")
    .eq("report_date", date)
    .maybeSingle();
  if (flashErr) return json({ error: "flash lookup failed: " + flashErr.message }, 500);
  if (!flash) return json({ error: `no flash_reports row for ${date}` }, 404);

  // 2. Render HTML
  const html = renderFlashPdfHtml(flash.payload, date, includeAlister);

  // 3. POST HTML to Browserless for PDF rendering
  const blResp = await fetch(
    `https://chrome.browserless.io/pdf?token=${encodeURIComponent(browserlessKey)}`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        html,
        options: {
          format: "A4",
          landscape: true,
          printBackground: true,
          margin: { top: "10mm", right: "10mm", bottom: "10mm", left: "10mm" },
        },
      }),
    },
  );

  if (!blResp.ok) {
    const text = await blResp.text();
    return json({
      error: `Browserless ${blResp.status}: ${text.slice(0, 500)}`,
    }, 500);
  }

  // 4. Convert binary PDF to base64 (chunked — the naive btoa(String.fromCharCode(...arr))
  //    blows the call stack on ~500KB+ PDFs)
  const pdfBuffer = new Uint8Array(await blResp.arrayBuffer());
  const pdfBase64 = bufferToBase64(pdfBuffer);

  return json({
    ok: true,
    date,
    include_alister: includeAlister,
    pdf_base64: pdfBase64,
    size_bytes: pdfBuffer.length,
  });
});

// ─── Helpers ────────────────────────────────────────────────────────────────

function json(payload: unknown, status = 200): Response {
  return new Response(JSON.stringify(payload), {
    status,
    headers: { "content-type": "application/json" },
  });
}

function bufferToBase64(buf: Uint8Array): string {
  // Chunked conversion to avoid call stack overflow on large buffers
  const chunkSize = 32768;
  let binary = "";
  for (let i = 0; i < buf.length; i += chunkSize) {
    const chunk = buf.subarray(i, i + chunkSize);
    binary += String.fromCharCode.apply(null, Array.from(chunk) as number[]);
  }
  return btoa(binary);
}
