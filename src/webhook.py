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


async def _run_pipeline(run_id: str, flags: list[str], target_date_iso: str | None) -> None:
    """Execute src/cron.py in a subprocess; capture stdout/stderr.

    On successful completion (returncode == 0), auto-trigger
    send-flash-email so the [RE-ISSUED] email goes out without depending
    on the edge function still polling. Without this, runs that exceed
    the edge function's 90s poll deadline would silently update the
    payload but never email anyone.
    """
    global _current, _last
    cmd = [sys.executable, "-u", "src/cron.py", *flags]
    _current["cmd"] = cmd
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
            email_target = target_date_iso or _athens_tomorrow_iso()
            email_result = await asyncio.to_thread(_trigger_email_sync, email_target)
            _current["email_target"] = email_target
            _current["email_result"] = email_result
    except Exception as e:
        _current["status"] = "failed"
        _current["error"] = f"{type(e).__name__}: {e}"
    finally:
        _current["finished_at"] = datetime.now(timezone.utc).isoformat()
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
    flags: list[str] = []
    target_date_iso: str | None = None
    if isinstance(body.get("date"), str):
        flags += ["--date", body["date"]]
        target_date_iso = body["date"]
    elif body.get("today"):
        flags.append("--today")
    if body.get("fallback_latest"):
        flags.append("--fallback-latest")

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
            "status": "running",
            "started_at": datetime.now(timezone.utc).isoformat(),
            "flags": flags,
        }
        asyncio.create_task(_run_pipeline(run_id, flags, target_date_iso))

    return {
        "ok": True,
        "run_id": run_id,
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
