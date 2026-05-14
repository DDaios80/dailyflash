# Phase 23b — Reissue webhook architectural fix

**Status:** planned for 2026-05-15 morning (after Phase 23a hotfix lands).
**Diagnosed:** 2026-05-14 ~22:30 Athens during Thelxi-can't-reissue crisis.

## The architectural problem

The current `reissue-flash` edge function (in `edge-functions.archive-2026-05-14/reissue-flash/index.ts`) tries to do four things in a single request handler:

1. `flash_reissue_log_start` — INSERT row
2. POST Railway `/reissue` (Python webhook)
3. **Poll `flash_reports.updated_at` for up to 90 seconds**
4. POST `send-flash-email`
5. `flash_reissue_log_finish` — close the row

Steps 3–5 are impossible to do reliably:

- The Python cron pipeline routinely takes **10–17 minutes** (Firecrawl A-lister step alone is variable).
- Supabase edge functions have a **150-second wall-clock limit**.
- The 90-second poll always times out before the pipeline finishes.
- The "timeout" branch calls `finishLog` — but evidence from `flash_reissue_log` proves it doesn't land. Every row has `pipeline_finished_at` = next row's `pipeline_started_at`, meaning only the supersede path in `flash_reissue_log_start` ever closes rows. `bf2be97c-...` sat 'running' for 12 days.

Best theory for why `finishLog` doesn't land: silent error in the `try { ... } catch (_) { /* best-effort */ }` block at line ~200 of the edge function, OR Supabase kills the function during the trailing email POST.

Either way, an edge function cannot wait 17 minutes for an external process. The architecture is wrong.

## Phase 23a (already shipped tonight)

Shrunk the supersede + has_running windows from 10 min → 2 min. Stuck rows still appear; they just clear themselves in 2 min. Worst-case lock-out for Thelxi: 2 min.

This is a band-aid. The real fix below.

## Phase 23b — proper architecture

**Single source of truth: the Python webhook writes `pipeline_finished_at` directly.**

### Flow

```
admin clicks "Reissue"
   │
   ▼
edge function reissue-flash
   │ 1. verify admin JWT
   │ 2. call flash_reissue_log_start RPC → run_id
   │ 3. POST Railway /reissue with body { run_id, date }
   │ 4. return { ok: true, run_id } IMMEDIATELY (≈200ms)
   │
   ▼
Python webhook /reissue (src/webhook.py)
   │ 1. receive run_id from body
   │ 2. spawn cron.py subprocess
   │ 3. return { ok: true, run_id } to edge function
   │ 4. background task: when subprocess finishes,
   │      UPDATE flash_reissue_log
   │        SET pipeline_finished_at = now(),
   │            status = 'ok'|'failed',
   │            payload_updated = (returncode == 0),
   │            error = stderr_tail
   │        WHERE id = run_id
   │ 5. background task: if ok, POST send-flash-email
   │ 6. background task: UPDATE flash_reissue_log
   │        SET reissue_email_triggered = (email_response.ok)
   │
   ▼
UI polls super_admin_reissue_history every 5s for ~20 min
   │ shows live status: running → ok / failed
   │ user gets a toast + the email when done
```

### Changes required

**`src/webhook.py`:**
- Import supabase client (or use `urllib` to call the RPCs directly).
- Add `SUPABASE_URL` + `SUPABASE_SERVICE_ROLE_KEY` env vars (already present in `.env`).
- In `/reissue` handler: parse `run_id` from body, attach it to `_current`.
- In `_run_pipeline`: after `proc.communicate()` returns, call `flash_reissue_log_finish` RPC via service role to close the row.
- After email POST: call `flash_reissue_log_finish` again to update `reissue_email_triggered`.
- All these calls are **best-effort** — log on failure, don't fail the run. The 2-min sweep from Phase 23a is the safety net.

**Edge function `reissue-flash` (re-deploy to Lovable):**
- Drop steps 3–5 entirely.
- After `log_start` + webhook POST, return `{ ok: true, run_id }` immediately.
- Keep the `mode === "preview"` path unchanged.
- Drop the `SEND_FLASH_EMAIL_URL` env var requirement (Python webhook handles that).

**Frontend (Lovable):**
- After clicking Reissue, show a "Running…" state.
- Poll `super_admin_reissue_history()` every 5s (cap at 20 min).
- When the matching row turns `status='ok'`, show success toast.
- When `status='failed'`, show the `error` field.
- This is a tiny UI change — `useEffect` with `setInterval`.

**No SQL changes** — the existing `flash_reissue_log_finish` RPC is fine. We're just calling it from the right place (Python) at the right time (when pipeline actually finishes).

### Why this is reliable

- Python webhook owns the process. It KNOWS when cron finishes.
- No 90s polling, no 150s wall-clock kill.
- Service role write to flash_reissue_log is one DB round-trip, ~50ms.
- Edge function does only fast work (RPC + webhook POST) — well under any limit.

### Risk

- The Python webhook becomes critical for log finalization. If it crashes mid-run, the row stays 'running' — but Phase 23a's 2-min sweep cleans it up.
- Edge function still does `log_start` (with admin auth). If that's broken, `run_id` never gets created. Tested today; works.
- Service role key in `.env` is the only secret needed — already there.

### Test plan

1. Apply Phase 23a SQL (done).
2. Update `src/webhook.py` to accept `run_id` + write finish.
3. Deploy to Railway.
4. Update `reissue-flash` edge function on Lovable.
5. Thelxi clicks Reissue once. Watch:
   - `flash_reissue_log` row created with `pipeline_started_at`.
   - Edge function returns < 1s.
   - 10-17 min later: `pipeline_finished_at` populated, `status='ok'`, `reissue_email_triggered=true`.
6. Click Reissue a second time within 2 min — supersede path closes the in-flight row, new one starts. OK.
7. Confirm UI shows running → ok transition.

### Out of scope for tomorrow

- Phase 47 deploy-skip fix (separate todo).
- A-lister Firecrawl hang (separate todo).
- comment_extractions silent-drop (separate todo).
