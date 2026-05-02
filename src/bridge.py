"""Bridge to the Lovable Cloud `ingest-flash-report` edge function.

Replaces the direct Supabase writes we used to do against the external project.
One POST per daily run, carrying the full envelope (reservations,
comment_extractions, alister_findings, researched_subjects, flash_report_payload,
optionally daily_briefing + pool_heating).

Authenticated via the shared `PIPELINE_SECRET` in the `Authorization` header.
Lovable's edge function runs with the internal service role and handles all
inserts/upserts server-side, including resv_name_id → reservation.id linking
and cascade replacement by (report_date, filename).
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable, Optional

from dotenv import load_dotenv


load_dotenv(Path(__file__).resolve().parent.parent / ".env", override=True)

INGEST_URL = os.environ.get("INGEST_FLASH_REPORT_URL") or ""
SHARED_SECRET = os.environ.get("PIPELINE_SECRET") or ""


# ─── JSON-safe serialisation (datetime/date → ISO) ──────────────────────────

def _json_safe(obj: Any) -> Any:
    if isinstance(obj, datetime):
        return obj.isoformat()
    if isinstance(obj, date):
        return obj.isoformat()
    if isinstance(obj, dict):
        return {k: _json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_json_safe(v) for v in obj]
    if isinstance(obj, set):
        return sorted(_json_safe(v) for v in obj)
    return obj


# ─── Envelope builder ───────────────────────────────────────────────────────

def build_envelope(
    *,
    report_date: date,
    filename: str,
    records: list[dict],
    extractions_by_rnid: dict[int, dict],
    findings_by_rnid: dict[int, list[dict]],
    researched_subjects: list[dict],
    flash_report_payload: dict,
    daily_briefing: Optional[dict] = None,
    pool_heating: Optional[list[dict]] = None,
    replace_existing: bool = True,
) -> dict:
    """Shape the envelope the edge function expects."""
    # reservations: ship every normalised field, including raw jsonb.
    # The edge function is responsible for generating reservations.id and
    # linking children via resv_name_id.
    reservations = [_json_safe(r) for r in records]

    # Map the richer Pydantic CommentExtraction fields onto the table schema:
    # late_checkout → lco ; merge ops_notes + payment_notes ; drop vip_flag /
    # already_in_house (not persisted, UI uses them via flash_report_payload).
    def _to_extraction_row(rnid: int, ext: dict) -> dict:
        # Prefer the LLM's structured ops_notes; fall back to the deprecated
        # ops_summary key for any in-flight extractions still on the old
        # schema. Append payment_notes so the team sees payment/credit terms
        # in the same field.
        primary = ext.get("ops_notes") or ext.get("ops_summary") or ""
        payment = ext.get("payment_notes") or ""
        if primary and payment:
            ops_notes = f"{primary}\n- {payment}" if not primary.endswith(payment) else primary
        else:
            ops_notes = primary or payment or None
        return {
            "resv_name_id": rnid,
            "allergies_present": bool(ext.get("allergies_present")),
            "allergies_text": ext.get("allergies_text"),
            "pool_fence": bool(ext.get("pool_fence")),
            "pool_heating": bool(ext.get("pool_heating")),
            "free_transfer": bool(ext.get("free_transfer")),
            "free_upgrade": bool(ext.get("free_upgrade")),
            "lco": bool(ext.get("lco") if "lco" in ext else ext.get("late_checkout")),
            "honeymoon": bool(ext.get("honeymoon")),
            "amenities": ext.get("amenities") or [],
            "ops_notes": ops_notes,
        }

    comment_extractions = [
        _to_extraction_row(rnid, _json_safe(ext))
        for rnid, ext in extractions_by_rnid.items()
    ]

    # alister_findings table has no 'reasoning' column (that lives on researched_subjects)
    def _to_alister_row(rnid: int, f: dict) -> dict:
        return {
            "resv_name_id": rnid,
            "matched_name": f.get("matched_name"),
            "relationship": f.get("relationship"),
            "is_notable": bool(f.get("is_notable")),
            "confidence": int(f.get("confidence") or 0),
            "category": f.get("category"),
            "summary": f.get("summary"),
            "evidence_urls": f.get("evidence_urls") or [],
            # Phase-10 hardening fields
            "photo_url": f.get("photo_url"),
            "disprove_confidence": int(f.get("disprove_confidence") or 0),
            "disprove_reasoning": f.get("disprove_reasoning"),
            "nationality_aligned": f.get("nationality_aligned"),
            "review_status": f.get("review_status") or "needs_review",
        }

    alister_findings = [
        _to_alister_row(rnid, _json_safe(f))
        for rnid, finds in findings_by_rnid.items()
        for f in finds
    ]

    return {
        "report_date": report_date.isoformat(),
        "filename": filename,
        "replace_existing": replace_existing,
        "reservations": reservations,
        "comment_extractions": comment_extractions,
        "alister_findings": alister_findings,
        "researched_subjects": _json_safe(researched_subjects),
        "pool_heating": _json_safe(pool_heating or []),
        "daily_briefing": _json_safe(daily_briefing),
        "flash_report_payload": _json_safe(flash_report_payload),
    }


# ─── POST ───────────────────────────────────────────────────────────────────

class BridgeError(RuntimeError):
    pass


def post_envelope(envelope: dict, *, timeout: float = 180.0) -> dict:
    """POST the envelope to the edge function. Returns parsed JSON response.

    Raises BridgeError on non-2xx. Response includes counts per table.
    """
    if not INGEST_URL:
        raise BridgeError("INGEST_FLASH_REPORT_URL not set in .env")
    if not SHARED_SECRET:
        raise BridgeError("PIPELINE_SECRET not set in .env")

    body = json.dumps(envelope).encode("utf-8")
    req = urllib.request.Request(
        INGEST_URL,
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {SHARED_SECRET}",
            "Content-Type": "application/json",
        },
    )
    try:
        resp = urllib.request.urlopen(req, timeout=timeout)
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")[:2000]
        raise BridgeError(f"HTTP {e.code}: {detail}") from e
    except Exception as e:
        raise BridgeError(f"network: {type(e).__name__}: {e}") from e

    raw = resp.read()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        # Function may return plain text on success
        return {"raw": raw.decode("utf-8", errors="replace")[:500]}


# ─── Envelope size sanity ───────────────────────────────────────────────────

def size_summary(envelope: dict) -> dict:
    """Quick diagnostics on envelope size."""
    return {
        "reservations": len(envelope.get("reservations") or []),
        "comment_extractions": len(envelope.get("comment_extractions") or []),
        "alister_findings": len(envelope.get("alister_findings") or []),
        "researched_subjects": len(envelope.get("researched_subjects") or []),
        "pool_heating": len(envelope.get("pool_heating") or []),
        "daily_briefing": bool(envelope.get("daily_briefing")),
        "flash_report_payload_keys": sorted(list((envelope.get("flash_report_payload") or {}).keys()))[:15],
        "approx_bytes": len(json.dumps(envelope)),
    }


# ─── CLI for ad-hoc replays ────────────────────────────────────────────────

def main() -> int:
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("envelope_json", help="Path to a JSON file with a pre-built envelope")
    args = ap.parse_args()
    envelope = json.loads(Path(args.envelope_json).read_text())
    print("size summary:", size_summary(envelope))
    resp = post_envelope(envelope)
    print("response:", json.dumps(resp, indent=2)[:2000])
    return 0


if __name__ == "__main__":
    sys.exit(main())
