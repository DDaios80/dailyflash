"""Document-sync poller — fam trips, site inspections, and group PDFs only.

Phase 70 — decouples the OneDrive PDF document syncs from the heavy flash
xlsx pipeline so they can run on a frequent, lightweight Railway cron
(every 30 min) independent of whether the daily flash xlsx has changed.

Background: previously the 3 PDF syncs ran only at the tail of cron.py's
full pipeline (Phase 28/44/14b). On Railway that pipeline fired once/day at
19:30 Athens; the Mac filled the gap with extra evening runs but was an
unreliable host (sleep/network/travel). Worse, in --auto-quick mode cron.py
`return 0`s the moment the xlsx is unchanged — BEFORE reaching the PDF
syncs — so a fam trip uploaded on a day the xlsx didn't change was skipped.
This entrypoint runs ONLY the syncs, via the Microsoft Graph API, so a
fam trip / inspection / group PDF is ingested within the poll interval of
upload regardless of the flash xlsx or any workstation being awake.

Each sync is best-effort and isolated: one failing does not block the
others, and the process exits 0 unless EVERY sync errored (so a transient
Graph hiccup on one folder doesn't spam Railway with failed-deploy noise).

Env vars required (same as cron.py — set on the Railway service):
    INGEST_FAM_TRIP_URL, INGEST_SITE_INSPECTION_URL, INGEST_GROUP_URL
    PIPELINE_SECRET
    ONEDRIVE_FAM_IMPORT_USER_ID (or legacy ONE_DRIVE_FAM_IMPORT_USER_ID)
    Microsoft Graph refresh-token credentials (MSGRAPH_* / as used by onedrive.py)

Exit codes:
    0 — at least one sync ran without raising (or all ran clean)
    1 — every sync raised (likely a shared cause: Graph auth / network)
"""
from __future__ import annotations

import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env", override=True)


# (label, module name, callable name) — mirrors cron.py's Phase 28/44/14b.
_SYNCS = [
    ("fam-trip", "fam_trip_sync", "sync"),
    ("site-inspection", "site_inspection_sync", "sync"),
    ("group", "group_sync", "sync"),
]


def main() -> int:
    ran_ok = 0
    raised = 0
    for label, module_name, fn_name in _SYNCS:
        try:
            module = __import__(module_name)
            result = getattr(module, fn_name)()
            print(f"[doc-sync] {label}: {result}")
            ran_ok += 1
        except Exception as e:
            raised += 1
            print(
                f"[doc-sync] {label} sync failed: {type(e).__name__}: {e}",
                file=sys.stderr,
            )

    # Optional heartbeat — best-effort, never fatal.
    try:
        from heartbeat import ping_heartbeat
        ping_heartbeat(label="doc-sync")
    except Exception as e:
        print(f"[doc-sync] heartbeat failed (non-fatal): {type(e).__name__}: {e}",
              file=sys.stderr)

    # Only a hard failure if EVERY sync raised — that signals a shared cause
    # (Graph auth expired, network down) worth surfacing as a failed run.
    if raised and ran_ok == 0:
        print("[doc-sync] all syncs failed — likely shared auth/network cause",
              file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
