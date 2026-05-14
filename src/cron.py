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
    ap.add_argument("--quick", action="store_true",
                    help="Skip slow steps (LLM extractions + A-lister research). For intra-day re-syncs after a room change. Existing extractions / findings stay attached via Phase 28.4 UPSERT, so the dashboard sees fresh occupancy + special-attention without a 5-min wait.")
    ap.add_argument("--auto-quick", action="store_true",
                    help="Polling mode: detect if OneDrive xlsx was modified after the last upload's uploaded_at, and only run quick pipeline if so. Use as a 15-30 min Railway cron to pick up Thelxi's intra-day room-change re-uploads automatically.")
    ap.add_argument("--force", action="store_true",
                    help="Bypass Phase 47 deploy-skip check (run even if RAILWAY_DEPLOYMENT_ID just changed).")
    args = ap.parse_args()

    # Phase 47 — skip cron runs triggered by Railway redeploys. Railway's
    # cron service fires the start command on every deploy AND on the
    # configured schedule. Without this guard, every git push to main
    # double-runs the pipeline (waste + reissue email spam to Thelxi).
    #
    # Mechanism: store the current RAILWAY_DEPLOYMENT_ID in app_settings.
    # If env's deployment_id doesn't match what we stored, this is a
    # fresh deploy — persist the new ID and exit. The next scheduled fire
    # will see matching IDs and proceed.
    #
    # --force bypasses this. Local runs without RAILWAY_DEPLOYMENT_ID set
    # also bypass (the env var is only present on Railway).
    if not args.force and os.environ.get("RAILWAY_DEPLOYMENT_ID"):
        deployment_id = os.environ["RAILWAY_DEPLOYMENT_ID"]
        try:
            from supa import client as _supa_client
            _sb = _supa_client()
            _row = (
                _sb.table("app_settings")
                .select("value")
                .eq("key", "cron_last_deployment_id")
                .maybe_single()
                .execute()
            )
            _stored = (_row.data or {}).get("value") if _row else None
            if _stored != deployment_id:
                _sb.table("app_settings").upsert(
                    {"key": "cron_last_deployment_id", "value": deployment_id},
                    on_conflict="key",
                ).execute()
                print(
                    f"[cron] Phase 47 deploy-skip: RAILWAY_DEPLOYMENT_ID changed "
                    f"({_stored} → {deployment_id}). Skipping this run; "
                    f"next scheduled fire will run normally."
                )
                return 0
        except Exception as _e:
            print(
                f"[cron] Phase 47 deploy-skip check failed (continuing): "
                f"{type(_e).__name__}: {_e}",
                file=sys.stderr,
            )

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
    #
    # Strategy: look for a file whose name contains today's date (the EXPORT
    # date — Maria/Thelxi export once per day). On miss, fall back to the
    # latest-modified xlsx and log a stale warning so operators can catch
    # the missed upload.
    birthdays_path: Path | None = None
    today_athens = datetime.now().date()  # Railway service runs with TZ=Europe/Athens
    if os.environ.get("MSGRAPH_CLIENT_ID") and os.environ.get("MSGRAPH_REFRESH_TOKEN"):
        from onedrive import (
            fetch_daily_flash_for_date, fetch_birthdays_for_date, GraphError,
        )
        try:
            tmp = Path(os.environ.get("DAILY_FLASH_TMP", "/tmp/daily-flash"))
            # Phase 40 — Thelxi's filename convention shifted from PREP date
            # (today) to REPORT date (tomorrow). Try the report date first,
            # then prep date for backward compat, before falling back to
            # latest-modified.
            xlsx, is_stale = fetch_daily_flash_for_date(target, tmp)
            if is_stale:
                xlsx2, is_stale2 = fetch_daily_flash_for_date(today_athens, tmp)
                if not is_stale2:
                    xlsx, is_stale = xlsx2, is_stale2
            tag = "STALE FALLBACK" if is_stale else "exact date match"
            print(f"[cron] pulled daily flash ({tag}): {xlsx.name}")
            if is_stale:
                print(
                    f"[cron] WARNING: no xlsx filename matched {target.isoformat()} "
                    f"or {today_athens.isoformat()} "
                    f"— using latest-modified '{xlsx.name}'. Operator may have forgotten "
                    f"to upload today's export.",
                    file=sys.stderr,
                )
        except GraphError as e:
            print(f"[cron] OneDrive fetch failed: {e}", file=sys.stderr)
            return 2
        # Best-effort pull of the comprehensive birthdays file. Missing file
        # is NOT fatal — pipeline falls back to Opera-comment extraction.
        try:
            birthdays_path, b_stale = fetch_birthdays_for_date(today_athens, tmp)
            if birthdays_path:
                btag = "STALE FALLBACK" if b_stale else "exact date match"
                print(f"[cron] pulled birthdays ({btag}): {birthdays_path.name}")
                if b_stale:
                    print(
                        f"[cron] WARNING: no birthdays xlsx matched {today_athens.isoformat()} "
                        f"— using latest-modified '{birthdays_path.name}'.",
                        file=sys.stderr,
                    )
            else:
                print("[cron] no birthdays file found in OneDrive Birthdays/ subfolder")
        except GraphError as e:
            print(f"[cron] birthdays fetch failed (continuing): {e}", file=sys.stderr)
            birthdays_path = None
    else:
        inbox = Path(args.inbox).expanduser()
        xlsx = find_xlsx_for(target, inbox)
        if xlsx is None and args.fallback_latest:
            xlsx = find_latest_xlsx(inbox)
        if xlsx is None:
            print(f"[cron] no xlsx found for {target} in {inbox}", file=sys.stderr)
            return 2
        # Look for a birthdays file in the local DailyFlash/Birthdays/ folder
        local_birthdays_dir = inbox / "Birthdays"
        if local_birthdays_dir.exists():
            candidates = sorted(
                [p for p in local_birthdays_dir.iterdir()
                 if p.is_file() and p.suffix.lower() == ".xlsx"],
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )
            if candidates:
                birthdays_path = candidates[0]
                print(f"[cron] local birthdays file: {birthdays_path.name}")

    # Phase 35 — auto-quick polling. Compare the OneDrive xlsx's
    # lastModifiedDateTime against the latest uploads.uploaded_at. If
    # OneDrive isn't newer, exit silently — nothing changed since the
    # last cron. Used as a 15-30 min Railway poll job that picks up
    # Thelxi's intra-day room-change re-exports without operator clicks.
    if args.auto_quick:
        try:
            from datetime import timezone
            xlsx_mtime = datetime.fromtimestamp(
                Path(xlsx).stat().st_mtime, tz=timezone.utc,
            )
            from supa import client as _supa_client
            sb = _supa_client()
            rows = sb.table("uploads").select("uploaded_at").order(
                "uploaded_at", desc=True,
            ).limit(1).execute().data or []
            last_upload_at = (
                datetime.fromisoformat(rows[0]["uploaded_at"].replace("Z", "+00:00"))
                if rows else None
            )
            if last_upload_at and xlsx_mtime <= last_upload_at:
                print(
                    f"[cron] auto-quick: OneDrive xlsx ({xlsx_mtime.isoformat()}) "
                    f"is not newer than last upload ({last_upload_at.isoformat()}). "
                    f"Nothing to do."
                )
                return 0
            print(
                f"[cron] auto-quick: OneDrive xlsx ({xlsx_mtime.isoformat()}) "
                f"is newer than last upload "
                f"({last_upload_at.isoformat() if last_upload_at else 'none'}). "
                f"Running quick pipeline."
            )
            args.quick = True   # auto-quick implies --quick
        except Exception as e:
            print(
                f"[cron] auto-quick check failed ({type(e).__name__}: {e}). "
                f"Falling through to normal run.",
                file=sys.stderr,
            )

    print(f"[cron] pipeline — date={target}, file={xlsx}{', QUICK MODE' if args.quick else ''}")
    from daily import run_daily
    try:
        run_daily(
            str(xlsx), target,
            run_extract=not args.quick,
            run_alister=not args.quick,
            birthdays_xlsx_path=str(birthdays_path) if birthdays_path else None,
        )
    except Exception as e:
        print(f"[cron] pipeline failed: {type(e).__name__}: {e}", file=sys.stderr)
        return 1

    # Phase 28 — sync FAM trip PDFs from OneDrive.
    # Best-effort: failures don't fail the cron, the flash report itself
    # is already written above.
    try:
        from fam_trip_sync import sync as fam_sync
        fam_sync()
    except Exception as e:
        print(f"[cron] fam-trip sync failed (non-fatal): {type(e).__name__}: {e}",
              file=sys.stderr)

    # Phase 44 — sync site inspection PDFs from OneDrive.
    # No-op until INGEST_SITE_INSPECTION_URL is wired up on the Lovable
    # side. Best-effort: failures don't fail the cron.
    try:
        from site_inspection_sync import sync as inspection_sync
        inspection_sync()
    except Exception as e:
        print(f"[cron] site-inspection sync failed (non-fatal): {type(e).__name__}: {e}",
              file=sys.stderr)

    # Phase 60.3 — extraction-freshness sanity check.
    # ingest-flash-report has historically silently dropped comment_extractions
    # writes (April 21 → May 9 incident: 18 days of frozen extractions despite
    # nightly cron success). After every cron run, query the DB and warn loudly
    # if max(extracted_at) is older than today. Non-fatal but visible — the
    # warning lands in Railway logs and surfaces the regression early.
    if not args.quick:
        try:
            _check_extraction_freshness()
        except Exception as e:
            print(f"[cron] freshness check failed (non-fatal): {type(e).__name__}: {e}",
                  file=sys.stderr)

    # Phase 68 — uptime heartbeat. Ping the external monitoring service to
    # confirm this run completed. If HEARTBEAT_URL env var is unset (local
    # dev or ops without monitoring configured), this is a no-op. If the
    # ping fails for any reason, log to stderr and continue — heartbeat
    # outages must never cause cron to fail. See src/heartbeat.py + the
    # README's "Monitoring" section for setup details.
    try:
        from heartbeat import ping_heartbeat
        ping_heartbeat(label="auto-quick" if args.auto_quick else ("quick" if args.quick else "daily"))
    except Exception as e:
        print(f"[cron] heartbeat module failed (non-fatal): {type(e).__name__}: {e}",
              file=sys.stderr)

    return 0


def _check_extraction_freshness() -> None:
    """Query comment_extractions.max(extracted_at) and warn if stale.

    Runs after the bridge POST has completed. If the upsert in
    ingest-flash-report silently drops writes, this catches it on the
    very next cron — instead of waiting 18 days and a hand-audit.
    """
    from datetime import datetime, timezone, timedelta
    try:
        from zoneinfo import ZoneInfo
        today_athens = datetime.now(ZoneInfo("Europe/Athens")).date()
    except Exception:
        today_athens = (datetime.now(timezone.utc) + timedelta(hours=3)).date()

    from supa import client
    sb = client()
    res = (
        sb.from_("comment_extractions")
        .select("extracted_at")
        .order("extracted_at", desc=True)
        .limit(1)
        .execute()
    )
    rows = res.data or []
    if not rows:
        print(
            "[freshness-check] *** WARNING: comment_extractions table is "
            "EMPTY. ingest-flash-report may be silently dropping writes. ***",
            file=sys.stderr,
        )
        return

    last = (rows[0].get("extracted_at") or "")[:10]
    days_stale = (today_athens - _parse_iso_date(last)).days if last else None

    if not last:
        print(
            "[freshness-check] *** WARNING: comment_extractions row has no "
            "extracted_at. Schema or write bug. ***",
            file=sys.stderr,
        )
    elif days_stale is not None and days_stale > 1:
        print(
            f"[freshness-check] *** WARNING: comment_extractions max(extracted_at) "
            f"= {last} (today is {today_athens}, stale by {days_stale} days). "
            f"ingest-flash-report is dropping writes. Investigate the edge "
            f"function upsert (must be ON CONFLICT DO UPDATE, not DO NOTHING). ***",
            file=sys.stderr,
        )
    else:
        print(
            f"[freshness-check] OK — comment_extractions fresh as of {last} "
            f"(today is {today_athens})."
        )


def _parse_iso_date(s: str):
    """Parse 'YYYY-MM-DD' to a date; return today on failure."""
    from datetime import date as _d
    try:
        y, m, d = s.split("-")
        return _d(int(y), int(m), int(d))
    except Exception:
        return _d.today()


if __name__ == "__main__":
    sys.exit(main())
