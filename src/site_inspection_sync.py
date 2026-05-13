"""Phase 44 — Site inspection auto-import from OneDrive.

Mirror of fam_trip_sync.py for the site_inspections table. Lists PDFs
in DailyFlash/SITE INSPECTIONS/, parses travel_agency + inspection_date
from the filename, and POSTs each new one to the
ingest-site-inspection-from-onedrive edge function.

Filename patterns observed in the wild:
    INSPECTION VISIT - EASYJET - 29.04.26.pdf            → EASYJET, 29 Apr 2026
    INSPECTION VISIT - SO HOSPITALITY - 02.05.2026.pdf   → SO HOSPITALITY, 2 May 2026

Files that don't match any pattern are skipped with a warning.
.msg/.eml Outlook exports in the same folder are out of scope (PDFs only).

Env vars required:
    INGEST_SITE_INSPECTION_URL   edge function endpoint, e.g.
                                 https://<project>.supabase.co/functions/v1/ingest-site-inspection-from-onedrive
    PIPELINE_SECRET              same secret used for FAM trip sync
    ONEDRIVE_FAM_IMPORT_USER_ID  the admin user_id to record as created_by
                                 (re-used — same admin imports both)

Phase 45 (DB trigger) auto-reassigns the d.daios default approver to
Thelxi at INSERT time, so the ingest edge function doesn't need to know
about Thelxi's UUID.
"""
from __future__ import annotations

import base64
import os
import re
import sys
from datetime import date
from typing import Optional

import requests

from onedrive import GraphError, download_pdf_bytes, list_site_inspection_pdfs


# Filename regex — INSPECTION VISIT - <AGENCY> - DD.MM.YY(YY).pdf
# Tolerant of extra whitespace and either 2-digit or 4-digit year.
_PATTERN = re.compile(
    r"^INSPECTION\s*VISIT\s*-\s*(?P<agency>.+?)\s*-\s*"
    r"(?P<d>\d{1,2})\.(?P<m>\d{1,2})\.(?P<y>\d{2,4})\.pdf$",
    re.IGNORECASE,
)


def parse_filename(name: str) -> Optional[tuple[str, date]]:
    """Parse a site inspection PDF filename. Returns (agency, inspection_date)
    or None if no pattern matches. Two-digit years are normalised to 20YY."""
    m = _PATTERN.match(name)
    if not m:
        return None
    try:
        d = int(m["d"])
        mo = int(m["m"])
        y = int(m["y"])
        if y < 100:
            y += 2000
        inspection_date = date(y, mo, d)
    except ValueError:
        return None
    return (_clean_agency(m["agency"]), inspection_date)


def _clean_agency(raw: str) -> str:
    """Strip underscores, collapse whitespace, drop trailing punctuation."""
    s = raw.replace("_", " ")
    s = re.sub(r"\s+", " ", s).strip()
    s = re.sub(r"[\s\-:.]+$", "", s)
    return s


def sync() -> dict:
    """List OneDrive PDFs and POST each to the ingest edge function.
    Returns {imported, skipped, errors, warnings} for logging.
    Safe to run repeatedly — server-side dedups by attachment_path / onedrive_item_id."""
    ingest_url = os.environ.get("INGEST_SITE_INSPECTION_URL")
    secret     = os.environ.get("PIPELINE_SECRET")
    # Accept either spelling — Railway's env var was historically named
    # ONE_DRIVE_FAM_IMPORT_USER_ID (with underscore between ONE and DRIVE)
    # while the docstring + code reference ONEDRIVE_FAM_IMPORT_USER_ID.
    # The mismatch silently no-op'd both syncs from Phase 28 / Phase 44
    # until 2026-05-12. Fall back to the legacy spelling so either works.
    user_id    = (
        os.environ.get("ONEDRIVE_FAM_IMPORT_USER_ID")
        or os.environ.get("ONE_DRIVE_FAM_IMPORT_USER_ID")
    )
    if not ingest_url or not secret or not user_id:
        print("[inspection-sync] skipped — INGEST_SITE_INSPECTION_URL / PIPELINE_SECRET / "
              "ONEDRIVE_FAM_IMPORT_USER_ID (or legacy ONE_DRIVE_FAM_IMPORT_USER_ID) not set",
              file=sys.stderr)
        return {"imported": 0, "skipped": 0, "errors": 0, "warnings": 1}

    try:
        items = list_site_inspection_pdfs()
    except GraphError as e:
        print(f"[inspection-sync] OneDrive listing failed: {e}", file=sys.stderr)
        return {"imported": 0, "skipped": 0, "errors": 1, "warnings": 0}

    if not items:
        print("[inspection-sync] no PDFs in SITE INSPECTIONS folder")
        return {"imported": 0, "skipped": 0, "errors": 0, "warnings": 0}

    imported = 0
    skipped  = 0
    errors   = 0
    warnings = 0

    for it in items:
        fname = it.get("name") or ""
        parsed = parse_filename(fname)
        if not parsed:
            print(f"[inspection-sync] WARNING: filename pattern not recognised, skipping: {fname!r}",
                  file=sys.stderr)
            warnings += 1
            continue
        agency, inspection_date = parsed

        try:
            pdf_bytes = download_pdf_bytes(it)
        except GraphError as e:
            print(f"[inspection-sync] download failed for {fname!r}: {e}", file=sys.stderr)
            errors += 1
            continue

        body = {
            "pdf_filename":      fname,
            "pdf_base64":        base64.b64encode(pdf_bytes).decode("ascii"),
            "pdf_size_bytes":    len(pdf_bytes),
            "travel_agency":     agency,
            "inspection_date":   inspection_date.isoformat(),
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
            print(f"[inspection-sync] POST failed for {fname!r}: {e}", file=sys.stderr)
            errors += 1
            continue

        if r.status_code >= 400:
            print(f"[inspection-sync] ingest returned {r.status_code} for {fname!r}: {r.text[:300]}",
                  file=sys.stderr)
            errors += 1
            continue

        resp = r.json() if r.content else {}
        if resp.get("skipped"):
            skipped += 1
            print(f"[inspection-sync] skipped (already imported): {fname!r}")
        else:
            imported += 1
            print(f"[inspection-sync] imported {fname!r} → "
                  f"inspection_id={resp.get('inspection_id')}")

    print(f"[inspection-sync] done: imported={imported}, skipped={skipped}, "
          f"errors={errors}, warnings={warnings}")
    return {"imported": imported, "skipped": skipped, "errors": errors, "warnings": warnings}


if __name__ == "__main__":
    # Standalone runner — load .env, run sync, print summary.
    from pathlib import Path
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
    result = sync()
    sys.exit(0 if result["errors"] == 0 else 1)
