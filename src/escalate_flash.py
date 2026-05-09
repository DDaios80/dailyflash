"""Phase 55 — morning escalation for unapproved flash reports.

Run on a Railway cron at 07:30 Athens. Checks whether the latest
flash_email_approvals row for today's report_date has approved_at
populated. If not, calls send-flash-email with the escalation
approver address (d.daios by default, configurable via
app_settings.flash_escalation_approver_email).

The escalation address gets a fresh preview/approval email and can
click Approve to release the mass-send before the 08:00 morning send
window closes.

Env vars:
    SEND_FLASH_EMAIL_URL    Lovable edge function endpoint
    PIPELINE_SECRET         shared bearer token

Exit codes:
    0  no escalation needed (already approved) OR escalation fired OK
    1  fatal error
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env", override=True)


def _athens_today() -> date:
    """Today's date in Athens, matching the report_date used by cron.py."""
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("Europe/Athens")).date()
    except Exception:
        # Fallback: UTC + 3 (Athens summer). Off by 1 hour twice a year.
        return (datetime.now(timezone.utc) + timedelta(hours=3)).date()


def _check_approval_status(report_date: date) -> dict | None:
    """Return the flash_email_approvals row for report_date, or None."""
    from supa import client
    sb = client()
    rows = (
        sb.from_("flash_email_approvals")
        .select(
            "report_date, preview_sent_at, approved_at, fanned_out_at, "
            "rejected_at"
        )
        .eq("report_date", report_date.isoformat())
        .limit(1)
        .execute()
        .data
        or []
    )
    return rows[0] if rows else None


def _get_escalation_email() -> str:
    """Look up app_settings.flash_escalation_approver_email; fallback hardcoded."""
    try:
        from supa import client
        sb = client()
        rows = (
            sb.from_("app_settings")
            .select("value")
            .eq("key", "flash_escalation_approver_email")
            .limit(1)
            .execute()
            .data
            or []
        )
        if rows and rows[0].get("value"):
            return rows[0]["value"].strip()
    except Exception as e:
        print(
            f"[escalate-flash] couldn't read app_settings, using fallback: "
            f"{type(e).__name__}: {e}",
            file=sys.stderr,
        )
    return "d.daios@daioshotels.com"


def _trigger_escalation_email(report_date: date, approver_email: str) -> int:
    """Call send-flash-email with mode=preview and the escalation approver.
    Returns 0 on success, 1 on failure."""
    url = os.environ.get("SEND_FLASH_EMAIL_URL", "").strip()
    secret = os.environ.get("PIPELINE_SECRET", "").strip()
    if not url or not secret:
        print(
            "[escalate-flash] SEND_FLASH_EMAIL_URL or PIPELINE_SECRET not set",
            file=sys.stderr,
        )
        return 1
    payload = json.dumps({
        "date": report_date.isoformat(),
        "mode": "preview",
        "approver_email": approver_email,
        "escalation": True,
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
        with urllib.request.urlopen(req, timeout=60) as resp:
            body = resp.read().decode("utf-8", errors="replace")[:500]
            print(f"[escalate-flash] {resp.status}: {body}")
            return 0 if resp.status < 400 else 1
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")[:500]
        print(f"[escalate-flash] HTTP {e.code}: {body}", file=sys.stderr)
        return 1
    except Exception as e:
        print(
            f"[escalate-flash] request failed: {type(e).__name__}: {e}",
            file=sys.stderr,
        )
        return 1


def main() -> int:
    today = _athens_today()
    print(f"[escalate-flash] checking approval for report_date={today}")

    row = _check_approval_status(today)
    if row is None:
        print(
            f"[escalate-flash] no flash_email_approvals row for {today} — "
            f"likely cron didn't run last night. Nothing to escalate."
        )
        return 0

    if row.get("approved_at"):
        print(
            f"[escalate-flash] already approved at {row['approved_at']} "
            f"(fanned_out_at={row.get('fanned_out_at')}). No escalation needed."
        )
        return 0

    if row.get("rejected_at"):
        print(
            f"[escalate-flash] flash was REJECTED at {row['rejected_at']}. "
            f"Not escalating."
        )
        return 0

    # preview_sent_at populated, approved_at NULL, rejected_at NULL → escalate
    escalation_email = _get_escalation_email()
    print(
        f"[escalate-flash] not approved yet "
        f"(preview_sent_at={row.get('preview_sent_at')}). "
        f"Escalating to {escalation_email}"
    )
    return _trigger_escalation_email(today, escalation_email)


if __name__ == "__main__":
    sys.exit(main())
