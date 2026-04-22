"""Email dispatcher — Railway cron entrypoint (06:00 Europe/Athens).

Triggers the Lovable Cloud `send-flash-email` edge function, which fans out
role-tailored flash emails via Resend to every user in the auth table.

Thin by design — all content logic lives in the edge function. This script
just POSTs with the right secret and logs the response.

Environment:
    SEND_FLASH_EMAIL_URL   https://<project-ref>.supabase.co/functions/v1/send-flash-email
    PIPELINE_SECRET        shared secret, same as the ingest function
    DAILY_FLASH_DATE       optional — YYYY-MM-DD to override; otherwise today
                           (the edge function computes today in Europe/Athens)
    DAILY_FLASH_DRY_RUN    optional — "1" / "true" to skip actual sending

Exit codes:
    0 — all deliveries succeeded (or dry-run)
    1 — POST failed or edge function returned error
    2 — some deliveries failed (edge function still returned 200 but with failures)
"""
from __future__ import annotations

import json
import os
import sys
from datetime import date
from pathlib import Path

import requests
from dotenv import load_dotenv


load_dotenv(Path(__file__).resolve().parent.parent / ".env", override=True)


def main() -> int:
    url = os.environ.get("SEND_FLASH_EMAIL_URL")
    secret = os.environ.get("PIPELINE_SECRET")
    override_date = os.environ.get("DAILY_FLASH_DATE")
    dry_run = os.environ.get("DAILY_FLASH_DRY_RUN", "").lower() in ("1", "true", "yes")

    if not url:
        print("[email] SEND_FLASH_EMAIL_URL not set", file=sys.stderr)
        return 1
    if not secret:
        print("[email] PIPELINE_SECRET not set", file=sys.stderr)
        return 1

    body: dict = {}
    if override_date:
        body["date"] = override_date
    if dry_run:
        body["dry_run"] = True

    print(f"[email] POST {url}  body={json.dumps(body)}")
    try:
        resp = requests.post(
            url,
            headers={
                "Authorization": f"Bearer {secret}",
                "Content-Type": "application/json",
            },
            json=body,
            timeout=120,
        )
    except requests.RequestException as e:
        print(f"[email] request failed: {type(e).__name__}: {e}", file=sys.stderr)
        return 1

    if resp.status_code != 200:
        print(f"[email] HTTP {resp.status_code}: {resp.text[:500]}", file=sys.stderr)
        return 1

    try:
        payload = resp.json()
    except ValueError:
        print(f"[email] non-JSON response: {resp.text[:500]}", file=sys.stderr)
        return 1

    sent = payload.get("sent", 0)
    failed = payload.get("failed", 0)
    skipped = payload.get("skipped", 0)
    total = payload.get("total_recipients", 0)
    print(
        f"[email] report_date={payload.get('report_date')} "
        f"sent={sent} failed={failed} skipped={skipped} total={total}"
    )

    # Surface any failures in the log
    for d in payload.get("deliveries", []):
        if d.get("status") == "failed":
            print(
                f"[email]   FAIL {d.get('recipient_email')} ({d.get('role')}): "
                f"{d.get('error')}",
                file=sys.stderr,
            )

    if failed > 0:
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
