"""One-command daily pipeline — bridge edition.

Replaces direct Supabase writes with a single POST to the Lovable Cloud
`ingest-flash-report` edge function. The edge function does all inserts/upserts
server-side using the internal service role.

Flow:
  1. Parse xlsx → records (in memory).
  2. LLM-extract COMMENTS (in memory, keyed by resv_name_id).
  3. A-lister research (in memory; cache disabled on the bridge side — cache
     will warm up again via edge-function upserts over time).
  4. Fetch weather (Open-Meteo).
  5. Assemble the flash_reports.payload in memory.
  6. Build envelope + POST to edge function.

Usage:
    python src/daily.py <xlsx> --date YYYY-MM-DD [--no-extract] [--no-alister]
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from ingest import parse_file
from compute import compute_flash
from alister import (
    subjects_from_reservation,
    research_subjects,
    DEFAULT_CACHE_TTL_DAYS,
)
from weather import fetch_weather
from bridge import build_envelope, post_envelope, size_summary


load_dotenv(Path(__file__).resolve().parent.parent / ".env", override=True)


# ─── Payload helpers ────────────────────────────────────────────────────────

def _findings_by_resv(findings: list) -> dict[int, list[dict]]:
    out: dict[int, list[dict]] = {}
    for subj, f in findings:
        rnid = subj.resv_name_id
        if rnid is None:
            continue
        out.setdefault(rnid, []).append({
            "matched_name": f.matched_name,
            "relationship": f.relationship,
            "is_notable": f.is_notable,
            "confidence": f.confidence,
            "category": f.category,
            "summary": f.summary,
            "evidence_urls": f.evidence_urls or [],
            "reasoning": f.reasoning,
        })
    return out


def _researched_subjects_from_findings(findings: list) -> list[dict]:
    rows = []
    seen: set = set()
    now = datetime.utcnow().replace(microsecond=0).isoformat() + "Z"
    for subj, f in findings:
        key = (
            (subj.first_name or "").strip().lower(),
            (subj.last_name or "").strip().lower(),
            subj.nationality,
        )
        if not key[0] and not key[1]:
            continue
        if key in seen:
            continue
        seen.add(key)
        rows.append({
            "first_name": key[0],
            "last_name": key[1],
            "nationality": key[2],
            "matched_name": f.matched_name,
            "relationship": f.relationship,
            "is_notable": f.is_notable,
            "confidence": f.confidence,
            "category": f.category,
            "summary": f.summary,
            "reasoning": f.reasoning,
            "evidence_urls": f.evidence_urls or [],
            "researched_at": now,
        })
    return rows


def assemble_payload_in_memory(
    records: list[dict],
    extractions_by_rnid: dict[int, dict],
    findings_by_rnid: dict[int, list[dict]],
    report_date: date,
    weather: list[dict] | None,
    *,
    promoted_rooms: set | None = None,
    daily_briefing: dict | None = None,
    pool_heating: list[dict] | None = None,
    birthdays_override: list[dict] | None = None,
) -> dict:
    """Build the flash_reports.payload from in-memory data (no DB query).

    If `birthdays_override` is provided (from the comprehensive OneDrive
    birthdays file), it replaces the Opera-reservations-derived
    birthdays_today list from compute_flash.
    """
    promoted_rooms = promoted_rooms or set()
    flash = compute_flash(records, report_date, promoted_rooms=promoted_rooms)

    def _enrich(guests: list[dict]) -> list[dict]:
        out = []
        for g in guests:
            rnid = g.get("resv_name_id")
            g2 = dict(g)
            g2["extraction"] = extractions_by_rnid.get(rnid)
            g2["alister"] = findings_by_rnid.get(rnid, [])
            out.append(g2)
        return out

    # A-lister panel: confidence ≥ 70, sorted by confidence desc
    rec_by_rnid = {r.get("resv_name_id"): r for r in records if r.get("resv_name_id")}
    al_panel_rows: list[dict] = []
    for rnid, finds in findings_by_rnid.items():
        for f in finds:
            if (f.get("confidence") or 0) < 70:
                continue
            rec = rec_by_rnid.get(rnid) or {}
            guest = f"{(rec.get('guest_first_name') or '').strip()} {(rec.get('guest_name') or '').strip()}".strip()
            al_panel_rows.append({
                **f,
                "room": rec.get("room"),
                "guest_on_booking": guest,
            })
    al_panel_rows.sort(key=lambda a: -(a.get("confidence") or 0))

    all_alister_count = sum(len(fs) for fs in findings_by_rnid.values())

    return {
        "report_date": report_date.isoformat(),
        "computed_at": datetime.utcnow().isoformat() + "Z",
        "occupancy": flash.occupancy,
        "weather": weather or [],
        "special_attention_arrivals": _enrich(flash.special_attention_arrivals),
        "special_attention_departures": _enrich(flash.special_attention_departures),
        "complimentary_partner_arrivals": _enrich(flash.complimentary_partner_arrivals),
        "pep_arrivals": _enrich(flash.pep_arrivals),
        "birthdays_in_house": (
            birthdays_override if birthdays_override is not None
            else _enrich(flash.birthdays_today)
        ),
        "allergies_in_house": _enrich(flash.allergies_in_house),
        "alister_findings": al_panel_rows,
        "alister_findings_count": len(al_panel_rows),
        "pool_heating": pool_heating or [],
        "daily_briefing": daily_briefing,
        "promoted_rooms": sorted(promoted_rooms),
        "totals": {
            "reservations": len(records),
            "extractions": len(extractions_by_rnid),
            "alister_researched": all_alister_count,
            "alister_notable": len(al_panel_rows),
        },
    }


# ─── Main orchestrator ──────────────────────────────────────────────────────

def run_daily(
    xlsx_path: str,
    report_date: date,
    *,
    run_extract: bool = True,
    run_alister: bool = True,
    use_cache: bool = False,       # default OFF — cache now lives in Lovable Cloud, warms via edge function
    extract_window_days: int = 7,
    dry_run: bool = False,
    birthdays_xlsx_path: str | None = None,
) -> dict:
    print(f"=== DAILY PIPELINE (bridge) — report_date={report_date} ===\n")

    # Step 1 — parse
    records = parse_file(xlsx_path)
    print(f"[1/5] Parsed {len(records)} reservations from {Path(xlsx_path).name}")

    # Step 1b — comprehensive birthdays file (optional)
    birthdays_override: list[dict] | None = None
    if birthdays_xlsx_path:
        from birthdays import load_birthdays
        try:
            birthdays_override = load_birthdays(birthdays_xlsx_path, report_date)
            print(f"[1b] Birthdays file: {len(birthdays_override)} in-house birthdays for {report_date}")
        except Exception as e:
            print(f"[1b] Birthdays file failed ({type(e).__name__}: {e}) — falling back to Opera extraction")
            birthdays_override = None

    # Step 2 — LLM COMMENTS extraction
    extractions_by_rnid: dict[int, dict] = {}
    if run_extract:
        from extract import extract_batch, _should_extract
        end = report_date + timedelta(days=extract_window_days)
        in_scope = [r for r in records if _should_extract(r, report_date, end)]
        print(f"[2/5] Extracting from {len(in_scope)} in-scope reservations...")
        extractions, errors, es = asyncio.run(extract_batch(in_scope))
        extractions_by_rnid = {rid: ext.model_dump() for rid, ext in extractions.items()}
        print(f"       {es['succeeded']}/{es['total']} ok, {es['failed']} failed")
    else:
        print(f"[2/5] --no-extract (skipped)")

    # Step 3 — A-lister
    findings_by_rnid: dict[int, list[dict]] = {}
    researched_subjects_list: list[dict] = []
    if run_alister:
        today_arrivals = [
            r for r in records
            if isinstance(r.get("arrival"), datetime) and r["arrival"].date() == report_date
        ]
        subjects = [s for r in today_arrivals for s in subjects_from_reservation(r)]
        print(f"[3/5] A-lister: {len(subjects)} subjects from {len(today_arrivals)} arrivals")
        findings, errors, stats = asyncio.run(research_subjects(
            subjects, use_cache=use_cache, cache_ttl_days=DEFAULT_CACHE_TTL_DAYS,
        ))
        print(f"       stats: {stats}")
        findings_by_rnid = _findings_by_resv(findings)
        researched_subjects_list = _researched_subjects_from_findings(findings)
    else:
        print(f"[3/5] --no-alister (skipped)")

    # Step 4 — weather
    weather = fetch_weather(report_date, days=3)
    print(f"[4/5] Weather: {len(weather or [])} days")

    # Step 5 — assemble + build envelope + POST
    flash_report_payload = assemble_payload_in_memory(
        records, extractions_by_rnid, findings_by_rnid,
        report_date, weather,
        birthdays_override=birthdays_override,
    )
    envelope = build_envelope(
        report_date=report_date,
        filename=Path(xlsx_path).name,
        records=records,
        extractions_by_rnid=extractions_by_rnid,
        findings_by_rnid=findings_by_rnid,
        researched_subjects=researched_subjects_list,
        flash_report_payload=flash_report_payload,
    )
    print(f"[5/5] Envelope size: {size_summary(envelope)}")

    if dry_run:
        out = Path("/tmp") / f"envelope-{report_date}.json"
        out.write_text(json.dumps(envelope))
        print(f"[dry-run] wrote envelope to {out} ({out.stat().st_size:,} bytes)")
        return {"dry_run": True, "envelope_path": str(out)}

    print("Posting envelope to edge function...")
    resp = post_envelope(envelope)
    print("Response:", json.dumps(resp, indent=2)[:1500])
    print(f"\n=== DONE — report_date={report_date} ===")
    return resp


def _parse_date(s: str) -> date:
    return datetime.strptime(s, "%Y-%m-%d").date()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("xlsx")
    ap.add_argument("--date", required=True, type=_parse_date)
    ap.add_argument("--no-extract", action="store_true")
    ap.add_argument("--no-alister", action="store_true")
    ap.add_argument("--use-cache", action="store_true",
                    help="Use the legacy Supabase cache (external project). OFF by default.")
    ap.add_argument("--dry-run", action="store_true",
                    help="Build the envelope but don't POST. Writes to /tmp.")
    ap.add_argument("--birthdays", default=None,
                    help="Path to the Opera birthdays xlsx (eur_birthday_v.*.xlsx). "
                         "If given, overrides the Opera reservations-derived birthday list.")
    args = ap.parse_args()

    try:
        run_daily(
            args.xlsx,
            args.date,
            run_extract=not args.no_extract,
            run_alister=not args.no_alister,
            use_cache=args.use_cache,
            dry_run=args.dry_run,
            birthdays_xlsx_path=args.birthdays,
        )
    except Exception as e:
        print(f"\npipeline failed: {type(e).__name__}: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
