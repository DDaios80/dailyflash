// tts-elevenlabs — Phase 26.3
//
// Streams ElevenLabs Flash v2.5 audio back to the client. Designed to
// be called by the Ask Daios drawer right after ask-daios returns the
// answer text. Keeps the API key server-side.
//
// Why a separate function (not folded into ask-daios):
//   * Faster perceived UX — the answer text appears in the drawer
//     immediately when ask-daios returns; audio starts playing as soon
//     as tts-elevenlabs streams its first bytes.
//   * Cleaner billing visibility — TTS calls separate from LLM calls.
//   * Lets future flows (re-listen, listen to history) reuse the same
//     endpoint without re-running the LLM.
//
// Contract:
//   POST /functions/v1/tts-elevenlabs
//   Headers: Authorization: Bearer <CALLER_JWT>
//   Body:    { text: string, language?: 'en'|'el', voice_id?: string,
//              qa_log_id?: string }
//   Returns: audio/mpeg stream (chunked)
//
// Env vars required:
//   ELEVENLABS_API_KEY
//   ELEVENLABS_VOICE_ID         (default: Sarah — EXAVITQu4vr4xnSDxMaL)
//   ELEVENLABS_MODEL_ID         (default: eleven_flash_v2_5 — multilingual, 75ms TTFB)

import { createClient } from "jsr:@supabase/supabase-js@2";

const CORS_HEADERS = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Methods": "POST, OPTIONS",
  "Access-Control-Allow-Headers": "authorization, x-client-info, apikey, content-type",
  "Access-Control-Expose-Headers": "x-tts-cost-usd, x-tts-chars, x-tts-voice",
  "Access-Control-Max-Age": "86400",
};

// Default voice + model. Sarah (warm professional female, multilingual via flash_v2_5).
const DEFAULT_VOICE_ID = "EXAVITQu4vr4xnSDxMaL";
const DEFAULT_MODEL_ID = "eleven_flash_v2_5";

// Pricing for usage telemetry. Flash v2.5 costs 0.5 credits per char.
// Creator tier ($22/mo) = 100K credits = 200K chars. Per-char cost
// approximation in USD — useful for /super-admin cost dashboard.
const USD_PER_1K_CHARS = 0.10;  // ~$22 / 220K chars rounded

Deno.serve(async (req) => {
  if (req.method === "OPTIONS") {
    return new Response(null, { status: 204, headers: CORS_HEADERS });
  }
  if (req.method !== "POST") return jsonError("POST only", 405);

  const supabaseUrl = Deno.env.get("SUPABASE_URL");
  const serviceKey  = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY");
  const elevenKey   = Deno.env.get("ELEVENLABS_API_KEY");
  if (!supabaseUrl || !serviceKey || !elevenKey) {
    return jsonError("server env missing", 500);
  }

  // Auth — require a valid Supabase JWT to prevent abuse of the TTS budget.
  const callerJwt = (req.headers.get("authorization") ?? "").replace(/^Bearer\s+/i, "");
  if (!callerJwt) return jsonError("authorization header required", 401);

  const supaCaller = createClient(supabaseUrl, serviceKey, {
    global: { headers: { Authorization: `Bearer ${callerJwt}` } },
  });
  const { data: caller } = await supaCaller.auth.getUser();
  if (!caller?.user?.id) return jsonError("invalid caller JWT", 401);

  const body = await req.json().catch(() => ({}));
  const text: string = (body?.text ?? "").toString().trim();
  const language: "en" | "el" = body?.language === "el" ? "el" : "en";
  const voiceId: string = (body?.voice_id || Deno.env.get("ELEVENLABS_VOICE_ID") || DEFAULT_VOICE_ID);
  const modelId: string = Deno.env.get("ELEVENLABS_MODEL_ID") || DEFAULT_MODEL_ID;
  const qaLogId: string | null = body?.qa_log_id ?? null;

  if (!text || text.length > 5000) return jsonError("text must be 1-5000 chars", 400);

  // ── Call ElevenLabs streaming endpoint ──────────────────────────────
  const elevenUrl = `https://api.elevenlabs.io/v1/text-to-speech/${voiceId}/stream`;

  const payload = {
    text,
    model_id: modelId,
    // Lightweight voice settings — calm hospitality tone.
    voice_settings: {
      stability: 0.5,         // 0.0=variable, 1.0=monotone. 0.5 = natural
      similarity_boost: 0.75, // hold the voice character firmly
      style: 0.0,             // off → most neutral
      use_speaker_boost: true,
    },
    // Apply text normalization for room numbers, percentages, etc.
    apply_text_normalization: "auto",
  };

  const t0 = Date.now();
  const upstream = await fetch(elevenUrl, {
    method: "POST",
    headers: {
      "xi-api-key": elevenKey,
      "Content-Type": "application/json",
      "Accept": "audio/mpeg",
    },
    body: JSON.stringify(payload),
  });

  if (!upstream.ok || !upstream.body) {
    const errBody = await upstream.text().catch(() => "");
    return jsonError(
      `ElevenLabs upstream failed: ${upstream.status} ${errBody.slice(0, 300)}`,
      upstream.status,
    );
  }

  // ── Telemetry: log usage best-effort, don't block the stream ───────
  const charCount = text.length;
  const costUsd = (charCount / 1000) * USD_PER_1K_CHARS;
  // Async best-effort log to the existing daios_qa_log row when present.
  if (qaLogId) {
    queueMicrotask(async () => {
      try {
        const supa = createClient(supabaseUrl, serviceKey);
        await supa.from("daios_qa_log")
          .update({ tts_chars: charCount, tts_cost_usd: costUsd,
                    tts_voice_id: voiceId, tts_model: modelId,
                    tts_latency_ms: Date.now() - t0 })
          .eq("id", qaLogId);
      } catch (_) { /* best-effort */ }
    });
  }

  // ── Pipe the audio bytes straight back to the client ───────────────
  return new Response(upstream.body, {
    status: 200,
    headers: {
      ...CORS_HEADERS,
      "Content-Type": "audio/mpeg",
      "Cache-Control": "no-store",
      "x-tts-cost-usd": costUsd.toFixed(5),
      "x-tts-chars": String(charCount),
      "x-tts-voice": voiceId,
    },
  });
});


function jsonError(message: string, status: number): Response {
  return new Response(JSON.stringify({ ok: false, error: message }), {
    status,
    headers: { "content-type": "application/json", ...CORS_HEADERS },
  });
}
