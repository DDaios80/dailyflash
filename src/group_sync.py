"""Phase 14b — Groups auto-import from OneDrive.

Mirror of site_inspection_sync.py for the groups table. Lists PDFs in
DailyFlash/GROUPS/, parses group_name + dates from the filename, and POSTs
each new one to the ingest-group-from-onedrive edge function.

The Groups folder holds MIXED types — tour groups, weddings, corporate
retreats, MICE/conferences. The ingest edge function classifies the
group_type from filename + PDF content (the heuristic lives on the Lovable
side, not here — Python just ships the PDF + metadata).

Filename pattern (same convention as inspections + FAM trips, defensive
parser accepting hyphen or underscore separators):

    GROUP - <NAME> - DD.MM.YYYY.pdf            → <NAME>, dated DD.MM.YYYY
    GROUP_NAME_15.05.2026.pdf                   → NAME, dated 15.05.2026

The date in the filename is treated as start_date. If end_date is needed
(multi-day group), it's extracted server-side from the PDF content (or
defaults to start_date for single-day events).

Env vars required:
    INGEST_GROUP_URL              edge function endpoint, e.g.
                                  https://<project>.supabase.co/functions/v1/ingest-group-from-onedrive
    PIPELINE_SECRET               shared secret with the edge function
    ONEDRIVE_FAM_IMPORT_USER_ID   admin user_id used as created_by
                                  (re-used — same admin imports all three:
                                   FAM trips, inspections, groups)

Phase 8b dual-approver routing applies: the ingest edge function reads
app_settings.group_ingest_approver_email (primary) +
app_settings.group_secondary_approver_email (secondary) and dispatches
to both — handled server-side, Python is unaware.
"""
from __future__ import annotations

import base64
import os
import re
import sys
from datetime import date
from typing import Optional

import requests

from onedrive import GraphError, download_pdf_bytes, list_group_pdfs


# Filename regex — GROUP <sep> <NAME> <sep> DD.MM.YY(YY).pdf
# Same defensive parser as site_inspection_sync.py: tolerant of hyphen OR
# underscore separators, extra whitespace, 2- or 4-digit year.
_PATTERN = re.compile(
    r"^GROUP\s*[-_]\s*(?P<name>.+?)\s*[-_]\s*"
    r"(?P<d>\d{1,2})\.(?P<m>\d{1,2})\.(?P<y>\d{2,4})\.pdf$",
    re.IGNORECASE,
)


def parse_filename(name: str) -> Optional[tuple[str, date]]:
    """Parse a group PDF filename. Returns (group_name, start_date) or None
    if no pattern matches. Two-digit years are normalised to 20YY."""
    m = _PATTERN.match(name)
    if not m:
        return None
    try:
        d = int(m["d"])
        mo = int(m["m"])
        y = int(m["y"])
        if y < 100:
            y += 2000
        start_date = date(y, mo, d)
    except ValueError:
        return None
    return (_clean_name(m["name"]), start_date)


def _clean_name(raw: str) -> str:
    """Strip underscores, collapse whitespace, drop trailing punctuation."""
    s = raw.replace("_", " ")
    s = re.sub(r"\s+", " ", s).strip()
    s = re.sub(r"[\s\-:.]+$", "", s)
    return s


def sync() -> dict:
    """List OneDrive PDFs and POST each to the ingest edge function.
    Returns {imported, skipped, errors, warnings} for logging.
    Safe to run repeatedly — server-side dedups by onedrive_item_id."""
    ingest_url = os.environ.get("INGEST_GROUP_URL")
    secret     = os.environ.get("PIPELINE_SECRET")
    # Re-use the same admin user_id env var as FAM trips + inspections.
    # The mismatch ONE_DRIVE_ vs ONEDRIVE_ historical: accept both.
    user_id    = (
        os.environ.get("ONEDRIVE_FAM_IMPORT_USER_ID")
        or os.environ.get("ONE_DRIVE_FAM_IMPORT_USER_ID")
    )
    if not ingest_url or not secret or not user_id:
        print("[group-sync] skipped — INGEST_GROUP_URL / PIPELINE_SECRET / "
              "ONEDRIVE_FAM_IMPORT_USER_ID (or legacy ONE_DRIVE_FAM_IMPORT_USER_ID) not set",
              file=sys.stderr)
        return {"imported": 0, "skipped": 0, "errors": 0, "warnings": 1}

    try:
        items = list_group_pdfs()
    except GraphError as e:
        print(f"[group-sync] OneDrive listing failed: {e}", file=sys.stderr)
        return {"imported": 0, "skipped": 0, "errors": 1, "warnings": 0}

    if not items:
        print("[group-sync] no PDFs in GROUPS folder")
        return {"imported": 0, "skipped": 0, "errors": 0, "warnings": 0}

    imported = 0
    skipped  = 0
    errors   = 0
    warnings = 0

    for it in items:
        fname = it.get("name") or ""
        parsed = parse_filename(fname)
        if not parsed:
            print(f"[group-sync] WARNING: filename pattern not recognised, skipping: {fname!r}",
                  file=sys.stderr)
            warnings += 1
            continue
        group_name, start_date = parsed

        try:
            pdf_bytes = download_pdf_bytes(it)
        except GraphError as e:
            print(f"[group-sync] download failed for {fname!r}: {e}", file=sys.stderr)
            errors += 1
            continue

        body = {
            "pdf_filename":      fname,
            "pdf_base64":        base64.b64encode(pdf_bytes).decode("ascii"),
            "pdf_size_bytes":    len(pdf_bytes),
            "group_name":        group_name,
            "start_date":        start_date.isoformat(),
            "onedrive_item_id":  it.get("id"),
            "onedrive_etag":     it.get("eTag") or it.get("@odata.etag"),
            "created_by_user_id": user_id,
        }

        try:
            r = requests.post(
                ingest_url,
                headers={
                    "content-type": "application/json",
                    "authorization": f"Bearer {secret}",
                },
                data=__import__("json").dumps(body),
                timeout=120,
            )
        except requests.RequestException as e:
            print(f"[group-sync] POST failed for {fname!r}: {e}", file=sys.stderr)
            errors += 1
            continue

        if r.status_code >= 400:
            print(f"[group-sync] ingest returned {r.status_code} for {fname!r}: {r.text[:300]}",
                  file=sys.stderr)
            errors += 1
            continue

        resp = r.json() if r.content else {}
        if resp.get("skipped"):
            skipped += 1
            print(f"[group-sync] skipped (already imported): {fname!r}")
        else:
            imported += 1
            print(f"[group-sync] imported {fname!r} → "
                  f"group_id={resp.get('group_id')}")

    print(f"[group-sync] done: imported={imported}, skipped={skipped}, "
          f"errors={errors}, warnings={warnings}")
    return {"imported": imported, "skipped": skipped, "errors": errors, "warnings": warnings}


if __name__ == "__main__":
    # Standalone runner — load .env, run sync, print summary.
    from pathlib import Path
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
    result = sync()
    sys.exit(0 if result["errors"] == 0 else 1)
