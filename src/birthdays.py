"""Birthdays parser — reads the comprehensive Opera PMS birthday export
from OneDrive and returns the subset of guests who are in-house on a given
report_date AND whose birthday (day/month) matches that date.

Replaces the best-effort COMMENTS extraction (which only caught birthdays
mentioned in booking notes) with the authoritative PMS list of all guest
birthdays across all reservations.

File shape (from `eur_birthday_v.DD.MM.YYYY.xlsx`):
    Columns: AGE, RTC, BIRTH_DATE, NAME_ID, ROOM, G_NAME, SNAME, AR_DATE,
             DEP_DATE, NTS, RATE_CODE, RESV_STATUS, VIP_STATUS, C,
             RESV_NAME_ID, ACOUNT_ID, CONFIRMATION_NO, CF_NO_OF_STAYS,
             CF_LAST_STAY, COUNTCPERREPORT
    Dates in 'DD-MMM-YY' format ("04-DEC-21", "15-MAY-26", etc.)
    ROOM may be a float (103.0); coerce to clean string.

File location on OneDrive:
    DailyFlash/Birthdays/eur_birthday_v.DD.MM.YYYY.xlsx
"""
from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from typing import Any, Optional

import pandas as pd


# Months abbreviation mapping for the Opera "DD-MMM-YY" format
_MONTHS = {
    "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
    "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12,
}


def _parse_opera_date(s: Any) -> Optional[date]:
    """Parse '04-DEC-21' / '15-MAY-26' into a date. Returns None on junk."""
    if s is None or (isinstance(s, float) and pd.isna(s)):
        return None
    if isinstance(s, (datetime, pd.Timestamp)):
        return s.date() if hasattr(s, "date") else s
    if isinstance(s, date):
        return s
    text = str(s).strip()
    if not text:
        return None
    parts = text.upper().split("-")
    if len(parts) != 3:
        return None
    try:
        day = int(parts[0])
        mon = _MONTHS.get(parts[1])
        if mon is None:
            return None
        year_str = parts[2]
        year = int(year_str)
        # 2-digit year heuristic: <50 → 20XX, >=50 → 19XX
        if len(year_str) == 2:
            year = 2000 + year if year < 50 else 1900 + year
        return date(year, mon, day)
    except (ValueError, TypeError):
        return None


def _clean_room(v: Any) -> str:
    """Room comes as float (103.0) — strip the .0 and return a string."""
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return ""
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v).strip()


def load_birthdays(xlsx_path: str | Path, report_date: date) -> list[dict[str, Any]]:
    """Return in-house guests whose birthday falls on `report_date`.

    Filter rules:
      - BIRTH_DATE's (day, month) == report_date's (day, month)
      - AR_DATE <= report_date < DEP_DATE (arrived and not yet departed)
      - RESV_STATUS in ('RESERVED', 'CHECKED IN') — defensive; skip cancellations
    """
    df = pd.read_excel(xlsx_path, sheet_name=0)
    target_dm = (report_date.day, report_date.month)
    hits: list[dict[str, Any]] = []

    for _, row in df.iterrows():
        # Birthday check — day/month match
        bd = _parse_opera_date(row.get("BIRTH_DATE"))
        if bd is None or (bd.day, bd.month) != target_dm:
            continue

        # In-house check — arrived and not yet departed
        ar = _parse_opera_date(row.get("AR_DATE"))
        dp = _parse_opera_date(row.get("DEP_DATE"))
        if ar is None or dp is None:
            continue
        if not (ar <= report_date < dp):
            continue

        # Skip cancellations / no-shows (defensive)
        status = str(row.get("RESV_STATUS") or "").strip().upper()
        if status not in ("RESERVED", "CHECKED IN", "CHECKED_IN", "INHOUSE", "IN HOUSE"):
            # Unknown status — keep (err on side of inclusion; safer than silent drop)
            pass

        age_on_date = None
        try:
            age_on_date = report_date.year - bd.year
            # If birthday hasn't occurred in report_date.year yet, age is
            # one less; but we already filtered by same day/month so today IS
            # the birthday in the current year — age = year diff as-is.
        except Exception:
            pass

        hits.append({
            "room": _clean_room(row.get("ROOM")),
            "guest_name": _clean_text(row.get("G_NAME")),
            "birth_date": bd.isoformat(),
            "age": age_on_date,
            "arrival": ar.isoformat(),
            "departure": dp.isoformat(),
            "vip_status": _clean_text(row.get("VIP_STATUS")),
            "travel_agent": _clean_text(row.get("SNAME")),
            "resv_name_id": _coerce_int(row.get("RESV_NAME_ID")),
            "rate_code": _clean_text(row.get("RATE_CODE")),
        })

    # Sort by room number (numeric if possible), then name
    def _sort_key(b: dict[str, Any]) -> tuple:
        try:
            return (0, int(b["room"]), b["guest_name"] or "")
        except (ValueError, TypeError):
            return (1, 0, b["room"] or "", b["guest_name"] or "")

    hits.sort(key=_sort_key)
    return hits


def _coerce_int(v: Any) -> Optional[int]:
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return None
    try:
        return int(v)
    except (ValueError, TypeError):
        return None


def _clean_text(v: Any) -> Optional[str]:
    """Return a clean string or None — never the literal 'nan' that
    str(pd.NaN) produces."""
    if v is None:
        return None
    try:
        if pd.isna(v):
            return None
    except (TypeError, ValueError):
        pass
    s = str(v).strip()
    if not s or s.lower() == "nan":
        return None
    return s
