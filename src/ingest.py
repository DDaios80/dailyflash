"""Parse the Opera Daily Flash .xlsx export into normalized reservation records.

Phase 1 scope: read file, filter junk rows (POS interface virtuals, cancellations, test rows),
and emit a list of dicts shaped like the `reservations` table.
"""
from __future__ import annotations

import math
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable

import pandas as pd


# Map Excel header → normalized snake_case column name.
COLUMN_MAP: dict[str, str] = {
    "RESV_STATUS": "resv_status",
    "CUSTOM_REFERENCE": "custom_reference",
    "GUEST_NAME": "guest_name",
    "GUEST_FIRST_NAME": "guest_first_name",
    "GUEST_MIDDLE_NAME": "guest_middle_name",
    "GUEST_PHONE": "guest_phone",
    "CONFIRMATION_NO": "confirmation_no",
    "ARRIVAL": "arrival",
    "DEPARTURE": "departure",
    "DEPOSIT_PAID": "deposit_paid",
    "PASSPORT": "passport",
    "ROOM": "room",
    "SPECIAL_REQUESTS": "special_requests",
    "COMMENTS": "comments",
    "VIP": "vip",
    "UDFC05": "udfc05",
    "HOUSE_USE_YN": "house_use_yn",
    "GUEST_VIP_DESC": "guest_vip_desc",
    "TRAVEL_AGENT_NAME": "travel_agent_name",
    "GROUP_NAME": "group_name",
    "BIRTH_DATE": "birth_date",
    "ARRIVAL_TIME": "arrival_time",
    "DEPARTURE_TIME": "departure_time",
    "CHILDREN1": "children1",
    "CHILDREN2": "children2",
    "CHILDREN3": "children3",
    "CHILDREN4": "children4",
    "CHILDREN5": "children5",
    "CRIBS": "cribs",
    "EXTRA_BEDS": "extra_beds",
    "ACCOMPANYING_NAMES": "accompanying_names",
    "CANCELLATION_REASON_DESC": "cancellation_reason_desc",
    "GUEST_EMAIL": "guest_email",
    "GUEST_TITLE": "guest_title",
    "ACTUAL_CHECK_IN_DATE": "actual_check_in_date",
    "ACTUAL_CHECK_OUT_DATE": "actual_check_out_date",
    "GUEST_TITLE_DESC": "guest_title_desc",
    "CANCELLATION_NO": "cancellation_no",
    "CANCELLATION_REASON_CODE": "cancellation_reason_code",
    "CHANNEL": "channel",
    "COMPANY_NAME": "company_name",
    "DISCOUNT_AMT": "discount_amt",
    "DISCOUNT_PRCNT": "discount_prcnt",
    "DISCOUNT_REASON_CODE": "discount_reason_code",
    "EXTERNAL_REFERENCE": "external_reference",
    "GUARANTEE_CODE": "guarantee_code",
    "GUEST_COUNTRY": "guest_country",
    "GUEST_COUNTRY_DESC": "guest_country_desc",
    "GUEST_LANGUAGE": "guest_language",
    "GUEST_LANGUAGE_DESC": "guest_language_desc",
    "GUEST_NAME_ID": "guest_name_id",
    "MARKET_CODE": "market_code",
    "MARKET_DESC": "market_desc",
    "NO_OF_ROOMS": "no_of_rooms",
    "PRODUCTS": "products",
    "RESV_NAME_ID": "resv_name_id",
    "SOURCE_CODE": "source_code",
    "SOURCE_DESC": "source_desc",
    "CANCELLATION_DATE": "cancellation_date",
    "ADULTS": "adults",
    "CHILDREN": "children",
    "PAX": "pax",
    "SOURCE_NAME": "source_name",
    "GUARANTEE_CODE_DESC": "guarantee_code_desc",
    "RATE_CODE": "rate_code",
    "BLOCK_CODE": "block_code",
    "BOOKED_DATE": "booked_date",
    "ROOM_CATEGORY_LABEL": "room_category_label",
    "BOOKED_ROOM_CATEGORY_LABEL": "booked_room_category_label",
    "ROOM_CLASS": "room_class",
    "TOTAL_RATE": "total_rate",
    "FIXED_CHARGE": "fixed_charge",
    "TERMS": "terms",
    "SPO": "spo",
    "NIGHTS": "nights",
    "NATIONALITY": "nationality",
    "COMPLIMENTARY_YN": "complimentary_yn",
    "PARRIVAL": "parrival",
    "PDEPARTURE": "pdeparture",
    "VOUCHER_ISSUE_DATE": "voucher_issue_date",
    "SPO_PERC": "spo_perc",
    "COMM": "comm",
    "COMM_CHANNEL": "comm_channel",
    "AGES": "ages",
}

# Columns we promote to real schema columns; rest goes into `raw` jsonb.
STRUCTURED_COLUMNS = {
    "resv_status", "guest_name", "guest_first_name", "guest_middle_name",
    "guest_title", "guest_title_desc", "guest_email", "guest_phone",
    "confirmation_no", "guest_name_id", "resv_name_id", "birth_date",
    "guest_country", "guest_country_desc", "nationality",
    "guest_language", "guest_language_desc",
    "arrival", "departure", "arrival_time", "departure_time",
    "actual_check_in_date", "actual_check_out_date",
    "nights", "adults", "children", "pax", "ages",
    "cribs", "extra_beds", "accompanying_names",
    "room", "room_category_label", "booked_room_category_label", "room_class",
    "vip", "guest_vip_desc", "special_requests", "comments",
    "house_use_yn", "complimentary_yn",
    "travel_agent_name", "group_name",
    "source_code", "source_desc", "source_name",
    "market_code", "market_desc",
    "channel", "company_name", "rate_code", "block_code",
    "no_of_rooms", "total_rate", "booked_date",
}


def _clean(v: Any) -> Any:
    """Turn pandas NaN / NaT / empty strings into None; keep datetimes and numbers as-is."""
    if v is None:
        return None
    # pandas NaT is not None and not float-NaN, but isinstance(pd.NaT, datetime) is True —
    # so it would sneak past later coerce checks and blow up date comparisons downstream.
    # pd.isna handles None, NaN, NaT, and pandas NA in one shot (scalars only).
    try:
        if pd.isna(v):
            return None
    except (TypeError, ValueError):
        pass  # non-scalar (list/dict/array) — fall through
    if isinstance(v, str):
        s = v.strip()
        return s if s else None
    return v


def _coerce_date(v: Any) -> date | None:
    v = _clean(v)
    if v is None:
        return None
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    try:
        return pd.to_datetime(v).date()
    except Exception:
        return None


def _coerce_int(v: Any) -> int | None:
    v = _clean(v)
    if v is None:
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


# Excel epoch: Excel counts days since 1899-12-30 (with the 1900 leap-year bug).
_EXCEL_EPOCH = datetime(1899, 12, 30)


def _coerce_timestamp(v: Any) -> datetime | None:
    """Normalise a value into a timezone-naive datetime.

    Handles datetimes (pass through), strings (parse), and Excel-serial numbers
    (pandas sometimes leaves date columns as floats e.g. 46132.0).
    """
    v = _clean(v)
    if v is None:
        return None
    if isinstance(v, datetime):
        return v
    if isinstance(v, date):
        return datetime(v.year, v.month, v.day)
    if isinstance(v, (int, float)):
        try:
            return _EXCEL_EPOCH + pd.Timedelta(days=float(v))
        except Exception:
            return None
    try:
        return pd.to_datetime(v).to_pydatetime()
    except Exception:
        return None


def _to_jsonable(v: Any) -> Any:
    """Convert values to JSON-serialisable forms (datetimes → ISO strings)."""
    v = _clean(v)
    if v is None:
        return None
    if isinstance(v, (datetime, date)):
        return v.isoformat()
    if isinstance(v, float) and math.isnan(v):
        return None
    return v


def load_workbook(path: str | Path) -> pd.DataFrame:
    """Load the Opera export as a DataFrame with normalised column names."""
    df = pd.read_excel(path, sheet_name=0, header=0, engine="openpyxl")
    df = df.rename(columns={k: v for k, v in COLUMN_MAP.items() if k in df.columns})

    # Row 2 in the source file has a stray "RESORT: 6907" marker — drop it.
    if "resv_status" in df.columns:
        df = df[df["resv_status"].notna()]
        df = df[~df["resv_status"].astype(str).str.startswith("RESORT")]
    return df.reset_index(drop=True)


def is_real_reservation(row: dict[str, Any]) -> bool:
    """Filter out POS interface virtuals, cancellations, and obvious test rows."""
    if _clean(row.get("resv_status")) == "CANCELLED":
        return False
    if _clean(row.get("room_class")) == "VIRT":
        # Opera POS interface postings (rooms 9000–9999)
        return False
    comments = _clean(row.get("comments")) or ""
    if comments.strip().upper() == "TEST":
        return False
    # House-use rooms are operational (e.g. inspections / staff) — keep but flag.
    return True


def _coerce_numeric(v: Any) -> float | None:
    """Coerce to float for numeric DB columns. Returns None for non-numeric strings
    (e.g. Opera summary rows that leak free-text into rate columns like
    'TRate=20,252,634')."""
    v = _clean(v)
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    try:
        return float(str(v).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def _coerce_room(v: Any) -> str | None:
    """Room codes are alphanumeric (e.g. 108, 9000, PI) but pandas reads purely numeric
    ones as float. Normalise to a clean string without a trailing .0."""
    v = _clean(v)
    if v is None:
        return None
    if isinstance(v, float):
        if v.is_integer():
            return str(int(v))
        return str(v)
    return str(v).strip() or None


def normalize_row(row: dict[str, Any]) -> dict[str, Any]:
    """Project a raw Excel row into the `reservations` record shape."""
    rec: dict[str, Any] = {}

    for col in STRUCTURED_COLUMNS:
        rec[col] = _clean(row.get(col))

    # Specific coercions
    rec["birth_date"] = _coerce_date(row.get("birth_date"))
    rec["room"] = _coerce_room(row.get("room"))
    for tscol in ("arrival", "departure", "actual_check_in_date",
                  "actual_check_out_date", "booked_date"):
        rec[tscol] = _coerce_timestamp(row.get(tscol))
    for intcol in ("nights", "adults", "children", "pax", "cribs", "extra_beds", "no_of_rooms"):
        rec[intcol] = _coerce_int(row.get(intcol))
    for bigintcol in ("guest_name_id", "resv_name_id"):
        rec[bigintcol] = _coerce_int(row.get(bigintcol))
    # Numeric money columns — Opera leaks summary-row free-text here occasionally.
    rec["total_rate"] = _coerce_numeric(row.get("total_rate"))

    # Everything else → raw
    raw = {}
    for k, v in row.items():
        if k in STRUCTURED_COLUMNS:
            continue
        jv = _to_jsonable(v)
        if jv is not None:
            raw[k] = jv
    rec["raw"] = raw
    return rec


def parse_file(path: str | Path) -> list[dict[str, Any]]:
    """End-to-end: load file → filter junk → normalize → dedupe → return records.

    Opera exports occasionally duplicate a reservation verbatim. We keep the
    first occurrence of each resv_name_id.
    """
    df = load_workbook(path)
    records: list[dict[str, Any]] = []
    seen_ids: set[int] = set()
    for _, series in df.iterrows():
        row = series.to_dict()
        if not is_real_reservation(row):
            continue
        rec = normalize_row(row)
        rid = rec.get("resv_name_id")
        if rid is not None:
            if rid in seen_ids:
                continue
            seen_ids.add(rid)
        records.append(rec)
    return records


def summarize(records: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """Quick sanity numbers for a parsed file."""
    records = list(records)
    by_status: dict[str, int] = {}
    for r in records:
        by_status[r["resv_status"]] = by_status.get(r["resv_status"], 0) + 1
    return {
        "total_real_reservations": len(records),
        "by_status": by_status,
    }
