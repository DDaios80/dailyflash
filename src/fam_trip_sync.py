"""Phase 28 — FAM trip auto-import from OneDrive.

Lists PDFs in DailyFlash/FAM TRIPS/, parses name + date range from the
filename, and POSTs each new one to the ingest-fam-trip-from-onedrive
edge function. The edge function dedups by pdf_filename, uploads to
storage, inserts a fam_trips row with status='pending_approval', and
fires the existing parse-fam-trip-itinerary edge function.

Filename patterns observed:
    DC x BELEON_FAM TRIP_03.-05.05.2026.pdf       → name="DC x BELEON FAM TRIP", 03 May → 05 May 2026
    GOSSIP+ 25.-29.04.2026.pdf                    → name="GOSSIP+",               25 Apr → 29 Apr 2026
    UK CARRIER FAM 25.04 - 28.04.2026.pdf         → name="UK CARRIER FAM",        25 Apr → 28 Apr 2026
    FAM TRIP ECLECTIC 24.-25.04.2026.pdf          → name="FAM TRIP ECLECTIC",     24 Apr → 25 Apr 2026

Files that don't match any pattern are skipped with a warning. The
weekly xlsx report ('WEEKLY FAM TRIPS - SITE INSPECTIONS REPORT.xlsx')
is filtered upstream in onedrive.list_fam_trip_pdfs (PDFs only).

Env vars required:
    INGEST_FAM_TRIP_URL          edge function endpoint, e.g.
                                 https://<project>.supabase.co/functions/v1/ingest-fam-trip-from-onedrive
    PIPELINE_SECRET              same secret used for ingest-flash-report
    ONEDRIVE_FAM_IMPORT_USER_ID  the admin user_id to record as created_by
"""
from __future__ import annotations

import base64
import os
import re
import sys
from datetime import date
from typing import Optional

import requests

from onedrive import GraphError, download_pdf_bytes, list_fam_trip_pdfs


# Filename regexes — order matters. First match wins.
# Pattern A: <NAME> DD.-DD.MM.YYYY.pdf  (most common)
_PATTERN_A = re.compile(
    r"^(?P<name>.+?)[\s_]+(?P<d1>\d{1,2})\.-(?P<d2>\d{1,2})\.(?P<m>\d{1,2})\.(?P<y>\d{4})\.pdf$",
    re.IGNORECASE,
)
# Pattern B: <NAME> DD.MM - DD.MM.YYYY.pdf
_PATTERN_B = re.compile(
    r"^(?P<name>.+?)[\s_]+(?P<d1>\d{1,2})\.(?P<m1>\d{1,2})\s*-\s*(?P<d2>\d{1,2})\.(?P<m2>\d{1,2})\.(?P<y>\d{4})\.pdf$",
    re.IGNORECASE,
)


def parse_filename(name: str) -> Optional[tuple[str, date, date]]:
    """Parse a FAM trip PDF filename. Returns (display_name, start, end)
    or None if no pattern matches. Display name is cleaned of underscores
    and trailing punctuation."""
    m = _PATTERN_A.match(name)
    if m:
        try:
            d1 = int(m["d1"]); d2 = int(m["d2"])
            mo = int(m["m"]);  yr = int(m["y"])
            start = date(yr, mo, d1)
            end   = date(yr, mo, d2)
        except ValueError:
            return None
        return (_clean_name(m["name"]), start, end)

    m = _PATTERN_B.match(name)
    if m:
        try:
            d1 = int(m["d1"]); m1 = int(m["m1"])
            d2 = int(m["d2"]); m2 = int(m["m2"])
            yr = int(m["y"])
            start = date(yr, m1, d1)
            end   = date(yr, m2, d2)
        except ValueError:
            return None
        return (_clean_name(m["name"]), start, end)

    return None


def _clean_name(raw: str) -> str:
    """Strip underscores, collapse whitespace, drop trailing punctuation."""
    s = raw.replace("_", " ")
    s = re.sub(r"\s+", " ", s).strip()
    s = re.sub(r"[\s\-:.]+$", "", s)
    return s


def sync() -> dict:
    """List OneDrive PDFs and POST each to the ingest edge function.
    Returns {imported, skipped, errors, warnings} for logging.
    Safe to run repeatedly — server-side dedups by pdf_filename."""
    ingest_url = os.environ.get("INGEST_FAM_TRIP_URL")
    secret     = os.environ.get("PIPELINE_SECRET")
    # Accept either spelling — Railway's env var was historically named
    # ONE_DRIVE_FAM_IMPORT_USER_ID (underscore between ONE and DRIVE) while
    # the code uses ONEDRIVE_FAM_IMPORT_USER_ID. The mismatch silently
    # no-op'd FAM + inspection sync since Phase 28 / Phase 44. Fall back
    # to the legacy name so either works.
    user_id    = (
        os.environ.get("ONEDRIVE_FAM_IMPORT_USER_ID")
        or os.environ.get("ONE_DRIVE_FAM_IMPORT_USER_ID")
    )
    if not ingest_url or not secret or not user_id:
        print("[fam-sync] skipped — INGEST_FAM_TRIP_URL / PIPELINE_SECRET / "
              "ONEDRIVE_FAM_IMPORT_USER_ID (or legacy ONE_DRIVE_FAM_IMPORT_USER_ID) not set",
              file=sys.stderr)
        return {"imported": 0, "skipped": 0, "errors": 0, "warnings": 1}

    try:
        items = list_fam_trip_pdfs()
    except GraphError as e:
        print(f"[fam-sync] OneDrive listing failed: {e}", file=sys.stderr)
        return {"imported": 0, "skipped": 0, "errors": 1, "warnings": 0}

    if not items:
        print("[fam-sync] no PDFs in FAM TRIPS folder")
        return {"imported": 0, "skipped": 0, "errors": 0, "warnings": 0}

    imported = 0
    skipped  = 0
    errors   = 0
    warnings = 0

    for it in items:
        fname = it.get("name") or ""
        parsed = parse_filename(fname)
        if not parsed:
            print(f"[fam-sync] WARNING: filename pattern not recognised, skipping: {fname!r}",
                  file=sys.stderr)
            warnings += 1
            continue
        name, start, end = parsed

        try:
            pdf_bytes = download_pdf_bytes(it)
        except GraphError as e:
            print(f"[fam-sync] download failed for {fname!r}: {e}", file=sys.stderr)
            errors += 1
            continue

        body = {
            "pdf_filename":   fname,
            "pdf_base64":     base64.b64encode(pdf_bytes).decode("ascii"),
            "pdf_size_bytes": len(pdf_bytes),
            "name":           name,
            "start_date":     start.isoformat(),
            "end_date":       end.isoformat(),
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
            print(f"[fam-sync] POST failed for {fname!r}: {e}", file=sys.stderr)
            errors += 1
            continue

        if r.status_code >= 400:
            print(f"[fam-sync] ingest returned {r.status_code} for {fname!r}: {r.text[:300]}",
                  file=sys.stderr)
            errors += 1
            continue

        resp = r.json() if r.content else {}
        if resp.get("skipped"):
            skipped += 1
            print(f"[fam-sync] skipped (already imported): {fname!r}")
        else:
            imported += 1
            print(f"[fam-sync] imported {fname!r} → trip_id={resp.get('trip_id')}, "
                  f"parsed={resp.get('parsed')}")

    print(f"[fam-sync] done: imported={imported}, skipped={skipped}, "
          f"errors={errors}, warnings={warnings}")
    return {"imported": imported, "skipped": skipped, "errors": errors, "warnings": warnings}


if __name__ == "__main__":
    # Standalone runner — load .env, run sync, print summary.
    from pathlib import Path
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
    result = sync()
    sys.exit(0 if result["errors"] == 0 else 1)
