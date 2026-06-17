"""Heartbeat writer for cron.py — best-effort production write via edge function.

Uses the same Bearer-auth pattern as the ingest endpoints. Reads URL and secret
from env vars RECORD_CRON_HEARTBEAT_URL and PIPELINE_SECRET. Never raises.
"""
from __future__ import annotations
import os
import sys
import socket
import time
from typing import Optional
from contextlib import contextmanager

import requests


def _write(service: str, status: str, details: Optional[dict] = None,
           duration_ms: Optional[int] = None) -> None:
    url = os.environ.get("RECORD_CRON_HEARTBEAT_URL")
    secret = os.environ.get("PIPELINE_SECRET")
    if not url or not secret:
        return
    try:
        requests.post(
            url,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {secret}",
            },
            json={
                "service": service,
                "status": status,
                "details": details,
                "hostname": socket.gethostname(),
                "pid": os.getpid(),
                "duration_ms": duration_ms,
            },
            timeout=(5, 5),
        )
    except Exception as e:
        print(f"[heartbeat] write failed (non-fatal): {type(e).__name__}: {e}",
              file=sys.stderr)


@contextmanager
def heartbeat(service: str, details: Optional[dict] = None):
    t0 = time.monotonic()
    _write(service, "started", details=details)
    try:
        yield
        elapsed_ms = int((time.monotonic() - t0) * 1000)
        _write(service, "completed", details=details, duration_ms=elapsed_ms)
    except Exception as e:
        elapsed_ms = int((time.monotonic() - t0) * 1000)
        err_details = {**(details or {}), "error": str(e), "error_type": type(e).__name__}
        _write(service, "error", details=err_details, duration_ms=elapsed_ms)
        raise


def ping_heartbeat(label: str = "") -> None:
    """Phase 68 — end-of-run liveness ping. Writes a 'completed' heartbeat row
    via the same edge function (cron.py's Phase 68 expected this helper; it was
    never created, so every run logged a non-fatal ImportError from
    2026-05 → 2026-06-12). Additionally pings HEARTBEAT_URL (external uptime
    monitor, e.g. healthchecks.io) when that env var is set. Never raises."""
    _write("daily-flash-cron", "completed",
           details={"label": label} if label else None)
    url = os.environ.get("HEARTBEAT_URL")
    if not url:
        return
    try:
        requests.get(url, params={"label": label} if label else None,
                     timeout=(5, 5))
    except Exception as e:
        print(f"[heartbeat] external ping failed (non-fatal): {type(e).__name__}: {e}",
              file=sys.stderr)
