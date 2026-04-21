"""Daily cron entry point. Finds today's xlsx in the OneDrive-synced inbox and
runs the full pipeline. Safe to run multiple times per day — idempotent
(replaces the upload for that date).

Environment:
    DAILY_FLASH_INBOX  path to the OneDrive-synced folder containing daily xlsx
                       files. Defaults to a macOS-ish path for this workstation.

Exit codes:
    0 — success
    1 — pipeline error
    2 — no file found for today (NOT treated as hard error; cron will retry)

Schedule:
    macOS launchd example (~/Library/LaunchAgents/com.daioscove.dailyflash.plist):
      ProgramArguments = /full/path/to/.venv/bin/python /full/path/to/src/cron.py
      StartCalendarInterval = {Hour=5, Minute=30}
      StandardOutPath = /Users/dimitriosdaios/Library/Logs/daily-flash.log

    Windows Task Scheduler:
      Action: py -3 C:\\path\\to\\src\\cron.py
      Trigger: Daily 05:30
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

from dotenv import load_dotenv


load_dotenv(Path(__file__).resolve().parent.parent / ".env", override=True)

DEFAULT_INBOX = os.environ.get(
    "DAILY_FLASH_INBOX",
    str(Path.home() / "Library/CloudStorage/OneDrive-HellasHolidayHotelsSA/DailyFlash"),
)


# Known naming conventions — ordered by preference (ISO date first).
_DATE_PATTERNS = [
    # e.g. DailyFlash_2026-04-20.xlsx
    (re.compile(r"DailyFlash_(\d{4})-(\d{2})-(\d{2})\.xlsx$", re.I),
     lambda m: date(int(m.group(1)), int(m.group(2)), int(m.group(3)))),
    # e.g. Daily Flash 20.04.2026.xlsx
    (re.compile(r"Daily[ _-]?Flash[ _-]+(\d{2})\.(\d{2})\.(\d{4})\.xlsx$", re.I),
     lambda m: date(int(m.group(3)), int(m.group(2)), int(m.group(1)))),
    # e.g. 2026-04-20.xlsx
    (re.compile(r"^(\d{4})-(\d{2})-(\d{2})\.xlsx$"),
     lambda m: date(int(m.group(1)), int(m.group(2)), int(m.group(3)))),
]


def find_xlsx_for(target: date, inbox: Path) -> Path | None:
    """Return the path of the .xlsx file whose name encodes `target`, if any."""
    if not inbox.exists():
        print(f"[cron] inbox does not exist: {inbox}", file=sys.stderr)
        return None
    for entry in inbox.iterdir():
        if not entry.is_file() or not entry.name.lower().endswith(".xlsx"):
            continue
        for pat, extract in _DATE_PATTERNS:
            m = pat.search(entry.name)
            if m:
                try:
                    if extract(m) == target:
                        return entry
                except Exception:
                    continue
    return None


def find_latest_xlsx(inbox: Path) -> Path | None:
    """Fallback: newest .xlsx in the inbox regardless of name."""
    if not inbox.exists():
        return None
    xs = [p for p in inbox.iterdir() if p.is_file() and p.suffix.lower() == ".xlsx"]
    if not xs:
        return None
    return max(xs, key=lambda p: p.stat().st_mtime)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--inbox", default=DEFAULT_INBOX,
                    help="OneDrive-synced folder containing the daily xlsx")
    ap.add_argument("--date", default=None,
                    help="YYYY-MM-DD (default: tomorrow's date — cron runs at 23:00 for next-day flash)")
    ap.add_argument("--today", action="store_true",
                    help="Override: target today's date instead of tomorrow (useful for manual re-runs)")
    ap.add_argument("--fallback-latest", action="store_true",
                    help="If no dated file for the target date, use the newest xlsx in the inbox")
    args = ap.parse_args()

    # Pipeline runs the night before at 23:00 local → default to TOMORROW.
    # Manual re-runs for today's flash use --today.
    if args.date:
        target = datetime.strptime(args.date, "%Y-%m-%d").date()
    elif args.today:
        target = date.today()
    else:
        target = date.today() + timedelta(days=1)

    # Production (Railway): pull today's xlsx from OneDrive via Graph API.
    # Dev/local (launchd): read from a OneDrive-synced folder on disk.
    if os.environ.get("MSGRAPH_CLIENT_ID") and os.environ.get("MSGRAPH_REFRESH_TOKEN"):
        from onedrive import fetch_latest_xlsx, GraphError
        try:
            tmp = Path(os.environ.get("DAILY_FLASH_TMP", "/tmp/daily-flash"))
            xlsx = fetch_latest_xlsx(tmp)
            print(f"[cron] pulled from OneDrive via Graph API: {xlsx.name}")
        except GraphError as e:
            print(f"[cron] OneDrive fetch failed: {e}", file=sys.stderr)
            return 2
    else:
        inbox = Path(args.inbox).expanduser()
        xlsx = find_xlsx_for(target, inbox)
        if xlsx is None and args.fallback_latest:
            xlsx = find_latest_xlsx(inbox)
        if xlsx is None:
            print(f"[cron] no xlsx found for {target} in {inbox}", file=sys.stderr)
            return 2

    print(f"[cron] pipeline — date={target}, file={xlsx}")
    from daily import run_daily
    try:
        run_daily(str(xlsx), target)
    except Exception as e:
        print(f"[cron] pipeline failed: {type(e).__name__}: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
