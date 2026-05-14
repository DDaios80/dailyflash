"""Reissue webhook — Phase 23.

Tiny HTTP server that lets an authorised caller (the Supabase edge function)
fire the daily pipeline on demand. Deployed as a separate Railway service
alongside the cron service.

Routes
    POST /reissue    — kick the pipeline (async, returns run_id immediately)
    GET  /status     — liveness check + last run snapshot
    GET  /health     — 200 OK for uptime probes

Auth
    Bearer PIPELINE_SECRET (shared with all other Phase 6+ triggers)

Runtime
    startCommand = uvicorn src.webhook:app --host 0.0.0.0 --port $PORT
    Add FastAPI + uvicorn to requirements.txt (see the bump in that file).

Concurrency model
    Pipeline runs in a background task via asyncio.to_thread. Only one run
    is permitted at a time — the second call within 10 minutes returns 409
    with the in-flight run_id. On finish the state is retained so the last
    run's stdout/stderr can be inspected via GET /status.
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import urllib.request
import urllib.error
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse


load_dotenv(Path(__file__).resolve().parent.parent / ".env", override=True)

PIPELINE_SECRET = os.environ.get("PIPELINE_SECRET", "").strip()
SEND_FLASH_EMAIL_URL = os.environ.get("SEND_FLASH_EMAIL_URL", "").strip()

# Phase 41 — /reissue debounce. The bridge edge function calls /reissue
# back at the end of every cron run (reason: "preview_already_sent"),
# which spawns ANOTHER cron subprocess that re-ingests. With OneDrive
# sometimes holding multiple dated xlsx files, the second ingest can
# pick a different file and clobber the first run's data via the upload
# UPSERT chain. Debounce: if the last successful run finished within
# REISSUE_DEBOUNCE_SECONDS, skip re-running. Caller can override with
# {"force": true} in body. Default 5 min — long enough to absorb the
# bridge's loop, short enough that a genuine manual reissue 6 min later
# still works.
REISSUE_DEBOUNCE_SECONDS = int(os.environ.get("REISSUE_DEBOUNCE_SECONDS", "300"))

# Single-slot run state. Protected by asyncio.Lock.
_lock = asyncio.Lock()
_current: dict[str, Any] | None = None
_last: dict[str, Any] | None = None

app = FastAPI(title="Daily Flash — Reissue Webhook", version="1.0")


def _check_auth(authorization: str | None) -> None:
    if not PIPELINE_SECRET:
        raise HTTPException(500, "PIPELINE_SECRET not configured")
    if not authorization or authorization.strip() != f"Bearer {PIPELINE_SECRET}":
        raise HTTPException(401, "unauthorized")


def _athens_tomorrow_iso() -> str:
    """Tomorrow's date in Athens — matches what `cron.py` writes as the
    flash_reports.report_date when invoked without --date / --today."""
    try:
        from zoneinfo import ZoneInfo
        athens = datetime.now(ZoneInfo("Europe/Athens"))
    except Exception:
        # Fallback: UTC + 3 (Athens summer). Acceptable rough estimate.
        athens = datetime.now(timezone.utc) + timedelta(hours=3)
    return (athens.date() + timedelta(days=1)).isoformat()


def _trigger_email_sync(target_date_iso: str) -> dict:
    """POST send-flash-email mode=preview. Synchronous (blocking) so we can
    record the result on _current. Uses urllib to avoid adding httpx dep."""
    if not SEND_FLASH_EMAIL_URL or not PIPELINE_SECRET:
        return {"ok": False, "error": "SEND_FLASH_EMAIL_URL or PIPELINE_SECRET unset"}
    payload = json.dumps({"date": target_date_iso, "mode": "preview"}).encode("utf-8")
    req = urllib.request.Request(
        SEND_FLASH_EMAIL_URL,
        data=payload,
        headers={
            "Authorization": f"Bearer {PIPELINE_SECRET}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = resp.read().decode("utf-8", errors="replace")[:500]
            return {"ok": resp.status < 300, "status": resp.status, "body": body}
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")[:500]
        return {"ok": False, "status": e.code, "body": body}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


# Phase 23b — write back to flash_reissue_log when the cron pipeline
# actually finishes. Single source of truth: the Python process owns
# the subprocess and KNOWS when it exits. Replaces the edge function's
# unreliable finishLog call (which never reaches the 90s timeout path —
# see docs/phase23b-reissue-architectural-fix.md for the diagnosis).
#
# Architectural note: flash_reissue_log lives on the Lovable Cloud
# project (wgbghdbfmapuqbfeiygb), which Python cannot reach directly via
# PostgREST (no service-role key exposure on Lovable projects). So we
# POST to a dedicated edge function — `finalize-reissue-log` — that
# Lovable hosts. It validates PIPELINE_SECRET and calls the
# flash_reissue_log_finish RPC internally with its own service-role.
def _finish_reissue_log(
    log_run_id: str,
    status: str,
    payload_updated: bool,
    email_triggered: bool,
    error: str | None,
) -> dict:
    """Best-effort POST to the `finalize-reissue-log` Lovable edge function.

    Errors are swallowed (logged to stderr only). Phase 23a's 2-min sweep
    is the safety net: a 'running' row > 2 min old gets superseded by the
    next preview read, so even if this write fails Thelxi won't be locked
    out. The write here is what turns the UI's polling indicator green.
    """
    url = os.environ.get("FINALIZE_REISSUE_LOG_URL", "").strip()
    secret = os.environ.get("PIPELINE_SECRET", "").strip()
    if not log_run_id or not url or not secret:
        return {
            "ok": False,
            "skipped": "missing log_run_id / FINALIZE_REISSUE_LOG_URL / PIPELINE_SECRET",
        }

    payload = json.dumps({
        "run_id": log_run_id,
        "status": status,
        "payload_updated": payload_updated,
        "email_triggered": email_triggered,
        "error": error,
    }).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=payload,
        headers={
            "Authorization": f"Bearer {secret}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = resp.read().decode("utf-8", errors="replace")[:300]
            return {"ok": resp.status < 300, "status": resp.status, "body": body}
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")[:300]
        print(f"[webhook] _finish_reissue_log HTTP {e.code}: {body}", file=sys.stderr)
        return {"ok": False, "status": e.code, "body": body}
    except Exception as e:
        print(f"[webhook] _finish_reissue_log failed (non-fatal): {type(e).__name__}: {e}",
              file=sys.stderr)
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


async def _run_pipeline(
    run_id: str,
    flags: list[str],
    target_date_iso: str | None,
    log_run_id: str | None = None,
) -> None:
    """Execute src/cron.py in a subprocess; capture stdout/stderr.

    On successful completion (returncode == 0), auto-trigger
    send-flash-email so the [RE-ISSUED] email goes out without depending
    on the edge function still polling. Without this, runs that exceed
    the edge function's 90s poll deadline would silently update the
    payload but never email anyone.

    Phase 23b — if `log_run_id` is provided (passed by the edge function
    in the POST body), finalize the matching flash_reissue_log row when
    cron exits. This replaces the edge function's unreliable finishLog
    call. Best-effort: errors don't fail the run.
    """
    global _current, _last
    cmd = [sys.executable, "-u", "src/cron.py", *flags]
    _current["cmd"] = cmd

    # Phase 23b — track final values for flash_reissue_log_finish.
    log_status: str = "failed"
    log_payload_updated: bool = False
    log_email_triggered: bool = False
    log_error: str | None = None

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(Path(__file__).resolve().parent.parent),
        )
        stdout, stderr = await proc.communicate()
        _current["returncode"] = proc.returncode
        _current["stdout_tail"] = (stdout or b"").decode("utf-8", errors="replace")[-4000:]
        _current["stderr_tail"] = (stderr or b"").decode("utf-8", errors="replace")[-4000:]
        _current["status"] = "ok" if proc.returncode == 0 else "failed"

        # Auto-trigger send-flash-email on success. Run sync inline (small
        # request) — happens after pipeline completes so timing isn't
        # critical. Result is captured on _current for /status visibility.
        if proc.returncode == 0:
            log_status = "ok"
            log_payload_updated = True
            email_target = target_date_iso or _athens_tomorrow_iso()
            email_result = await asyncio.to_thread(_trigger_email_sync, email_target)
            _current["email_target"] = email_target
            _current["email_result"] = email_result
            log_email_triggered = bool(email_result.get("ok"))
        else:
            log_status = "failed"
            tail = _current.get("stderr_tail") or ""
            log_error = (tail[-500:] if tail else f"cron exited with code {proc.returncode}")
    except Exception as e:
        _current["status"] = "failed"
        _current["error"] = f"{type(e).__name__}: {e}"
        log_status = "failed"
        log_error = f"{type(e).__name__}: {e}"[:500]
    finally:
        _current["finished_at"] = datetime.now(timezone.utc).isoformat()

        # Phase 23b — finalize flash_reissue_log row (best-effort).
        if log_run_id:
            finish_result = await asyncio.to_thread(
                _finish_reissue_log,
                log_run_id,
                log_status,
                log_payload_updated,
                log_email_triggered,
                log_error,
            )
            _current["log_finish_result"] = finish_result

        _last = dict(_current)
        _current = None


@app.post("/reissue")
async def reissue(request: Request, authorization: str | None = Header(None)):
    global _current
    _check_auth(authorization)

    body: dict[str, Any] = {}
    try:
        body = await request.json()
    except Exception:
        pass

    # Optional body knobs:
    #   { "today": true }   -> pass --today to cron.py (rebuild today's flash,
    #                          not tomorrow's). Matches the same-evening
    #                          reissue path (target_date = tomorrow anyway
    #                          since cron.py defaults to tomorrow).
    #   { "date": "YYYY-MM-DD" } -> explicit target date
    #   { "log_run_id": "uuid" } or { "run_id": "uuid" } -> Phase 23b — the
    #     flash_reissue_log row id assigned by the edge function. We use it
    #     to finalize the row when cron exits. Accepted under either key for
    #     forward-compat with whichever name the edge function uses.
    flags: list[str] = []
    target_date_iso: str | None = None
    if isinstance(body.get("date"), str):
        flags += ["--date", body["date"]]
        target_date_iso = body["date"]
    elif body.get("today"):
        flags.append("--today")
    if body.get("fallback_latest"):
        flags.append("--fallback-latest")

    log_run_id: str | None = None
    if isinstance(body.get("log_run_id"), str):
        log_run_id = body["log_run_id"]
    elif isinstance(body.get("run_id"), str):
        log_run_id = body["run_id"]

    # Stale-run guard: clear _current if it's been hung > 15 min so a wedged
    # subprocess doesn't permanently lock out reissues.
    if _current is not None:
        age_s = 0
        try:
            started = datetime.fromisoformat(_current["started_at"].replace("Z", "+00:00"))
            age_s = (datetime.now(timezone.utc) - started).total_seconds()
        except Exception:
            pass
        if age_s > 900:
            _current = None  # treat as crashed

    # Phase 41 — debounce. The bridge edge function calls /reissue at the
    # end of every cron run, which would spawn another cron subprocess
    # and race against the just-completed ingest. If the last successful
    # run finished within the debounce window, skip — the data is already
    # fresh. Caller can override with {"force": true}.
    if not body.get("force") and _last is not None and _last.get("status") == "ok":
        try:
            finished = datetime.fromisoformat(
                _last["finished_at"].replace("Z", "+00:00")
            )
            age_since_finish_s = (
                datetime.now(timezone.utc) - finished
            ).total_seconds()
        except Exception:
            age_since_finish_s = REISSUE_DEBOUNCE_SECONDS + 1
        if age_since_finish_s < REISSUE_DEBOUNCE_SECONDS:
            return {
                "ok": True,
                "skipped": True,
                "reason": (
                    f"debounced: last successful run finished "
                    f"{int(age_since_finish_s)}s ago "
                    f"(< {REISSUE_DEBOUNCE_SECONDS}s threshold). "
                    f"Pass force:true to override."
                ),
                "last_run_id": _last.get("run_id"),
                "last_finished_at": _last.get("finished_at"),
            }

    async with _lock:
        if _current is not None:
            return JSONResponse(
                {"ok": False, "error": "a pipeline is already running",
                 "in_flight_run_id": _current["run_id"],
                 "started_at": _current["started_at"]},
                status_code=409,
            )
        run_id = str(uuid.uuid4())
        _current = {
            "run_id": run_id,
            "log_run_id": log_run_id,
            "status": "running",
            "started_at": datetime.now(timezone.utc).isoformat(),
            "flags": flags,
        }
        asyncio.create_task(_run_pipeline(run_id, flags, target_date_iso, log_run_id))

    return {
        "ok": True,
        "run_id": run_id,
        "log_run_id": log_run_id,
        "status": "running",
        "started_at": _current["started_at"],
        "flags": flags,
    }


@app.get("/status")
async def status(authorization: str | None = Header(None)):
    _check_auth(authorization)
    return {
        "current": _current,
        "last": _last,
    }


@app.get("/health")
async def health():
    return {"ok": True, "service": "reissue-webhook"}
