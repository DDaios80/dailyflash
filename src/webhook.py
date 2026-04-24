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
import os
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse


load_dotenv(Path(__file__).resolve().parent.parent / ".env", override=True)

PIPELINE_SECRET = os.environ.get("PIPELINE_SECRET", "").strip()

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


async def _run_pipeline(run_id: str, flags: list[str]) -> None:
    """Execute src/cron.py in a subprocess; capture stdout/stderr."""
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
    if isinstance(body.get("date"), str):
        flags += ["--date", body["date"]]
    elif body.get("today"):
        flags.append("--today")
    if body.get("fallback_latest"):
        flags.append("--fallback-latest")

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
        asyncio.create_task(_run_pipeline(run_id, flags))

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
