"""Zoho notes fetcher — Phase 24.

Reads the zoho_notes table (populated by the ingest-zoho-notes edge
function in Lovable) and returns rows bucketed for the daily flash
payload. The Python pipeline does NOT ingest Zoho data; that's owned
by the edge function. This module is purely the read side.

Buckets returned:
  - allergies:           note_kind = 'allergy'
  - medical:             note_kind = 'medical'
  - pending_complaints:  source_type = 'pending_complaints', resolved_at IS NULL
  - boat_trips:          source_type = ZOHO_EXCURSIONS_SOURCE_TYPE, note_date = report_date
  - hsk_orders:          source_type = 'housekeeping_notes', last 24h
"""
from __future__ import annotations

import os
from datetime import date, datetime, timedelta, timezone
from typing import Any

from supa import client as supa_client


# The Lovable-side enum value covering activity reports (RIB boat trips
# and any future excursion-type reports). Confirmed with admin 26 Apr 2026.
ZOHO_EXCURSIONS_SOURCE_TYPE = os.environ.get("ZOHO_EXCURSIONS_SOURCE_TYPE", "excursions")


_SELECT_FIELDS = (
    "id,external_id,note_kind,source_type,note_date,note_created_at,"
    "guest_name,room,reservation_ref,subject,body,status,resolved_at,"
    "category_slug,subcategory_slug,severity,sentiment,ai_tags,ai_confidence,"
    "ingested_at"
)


def fetch_zoho_for_report(report_date: date) -> dict[str, list[dict]]:
    """Pull zoho_notes buckets relevant for the given report_date.

    Returns a dict with five keys; each value is a list of zoho_notes rows
    (already filtered server-side; further in-house filtering happens in
    compute.merge_zoho_into_flash).

    Failures are caught at the call site (daily.py) — raise here so the
    caller can decide whether to abort or continue with empty zoho data.
    """
    sb = supa_client()

    def _q(table: str, **filters):
        q = sb.from_(table).select(_SELECT_FIELDS)
        for col, expr in filters.items():
            op, _, val = expr.partition(".")
            if op == "eq":
                q = q.eq(col, val)
            elif op == "is":
                q = q.is_(col, val)  # supports 'null'
            elif op == "gt":
                q = q.gt(col, val)
            elif op == "gte":
                q = q.gte(col, val)
            else:
                raise ValueError(f"unsupported op: {expr}")
        return q

    twentyfourh_ago = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
    rd_iso = report_date.isoformat()

    out: dict[str, list[dict]] = {}

    # 1. Allergies — note_kind=allergy. No date filter; allergy notes persist
    #    for the whole stay. In-house filter happens at merge time.
    out["allergies"] = (
        _q("zoho_notes", note_kind="eq.allergy")
        .order("note_created_at", desc=True)
        .limit(500)
        .execute()
        .data or []
    )

    # 2. Medical — note_kind=medical. Same logic as allergies.
    out["medical"] = (
        _q("zoho_notes", note_kind="eq.medical")
        .order("note_created_at", desc=True)
        .limit(200)
        .execute()
        .data or []
    )

    # 3. Pending complaints — source_type=pending_complaints, resolved_at IS NULL.
    out["pending_complaints"] = (
        sb.from_("zoho_notes").select(_SELECT_FIELDS)
        .eq("source_type", "pending_complaints")
        .is_("resolved_at", "null")
        .order("note_created_at", desc=True)
        .limit(200)
        .execute()
        .data or []
    )

    # 4. Boat trips for today — note_date = report_date.
    out["boat_trips"] = (
        sb.from_("zoho_notes").select(_SELECT_FIELDS)
        .eq("source_type", ZOHO_EXCURSIONS_SOURCE_TYPE)
        .eq("note_date", rd_iso)
        .order("note_created_at", desc=False)
        .limit(100)
        .execute()
        .data or []
    )

    # 5. HSK orders — source_type=housekeeping_notes, last 24h.
    out["hsk_orders"] = (
        sb.from_("zoho_notes").select(_SELECT_FIELDS)
        .eq("source_type", "housekeeping_notes")
        .gt("note_created_at", twentyfourh_ago)
        .order("note_created_at", desc=True)
        .limit(300)
        .execute()
        .data or []
    )

    return out


def size_summary(zoho: dict[str, list[dict]]) -> str:
    parts = [f"{k}={len(v)}" for k, v in zoho.items()]
    return ", ".join(parts)
