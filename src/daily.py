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
from compute import compute_flash, _in_house_on, merge_zoho_into_flash
from zoho_fetch import fetch_zoho_for_report, size_summary as zoho_size_summary
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
            "photo_url": f.photo_url,
            "disprove_confidence": f.disprove_confidence,
            "disprove_reasoning": f.disprove_reasoning,
            "nationality_aligned": f.nationality_aligned,
            "review_status": f.review_status,
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
            "photo_url": f.photo_url,
            "disprove_confidence": f.disprove_confidence,
            "disprove_reasoning": f.disprove_reasoning,
            "nationality_aligned": f.nationality_aligned,
            "review_status": f.review_status,
            "schema_version": 2,
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
    zoho_data: dict[str, list[dict]] | None = None,
) -> dict:
    """Build the flash_reports.payload from in-memory data (no DB query).

    If `birthdays_override` is provided (from the comprehensive OneDrive
    birthdays file), it replaces the Opera-reservations-derived
    birthdays_today list from compute_flash.

    If `zoho_data` is provided (from zoho_fetch.fetch_zoho_for_report),
    Phase 24 zoho payload sections are computed and added.
    """
    promoted_rooms = promoted_rooms or set()
    flash = compute_flash(records, report_date, promoted_rooms=promoted_rooms)
    zoho_payload = merge_zoho_into_flash(records, zoho_data or {}, report_date)

    def _enrich(guests: list[dict]) -> list[dict]:
        out = []
        for g in guests:
            rnid = g.get("resv_name_id")
            g2 = dict(g)
            g2["extraction"] = extractions_by_rnid.get(rnid)
            g2["alister"] = findings_by_rnid.get(rnid, [])
            out.append(g2)
        return out

    # A-lister panel: only surface confirmed or needs_review findings
    # (review_status is computed in alister.py from confidence + disprove pass).
    rec_by_rnid = {r.get("resv_name_id"): r for r in records if r.get("resv_name_id")}
    al_panel_rows: list[dict] = []
    for rnid, finds in findings_by_rnid.items():
        for f in finds:
            status = f.get("review_status") or "needs_review"
            if status not in ("confirmed", "needs_review"):
                continue
            # Belt-and-braces: still require ≥85 confidence + is_notable
            if not f.get("is_notable") or (f.get("confidence") or 0) < 85:
                continue
            rec = rec_by_rnid.get(rnid) or {}
            # Phase 23.1: defensive in-house filter. The reservation must
            # actually overlap the report_date — protects against findings
            # for guests whose stay ended before today's flash, or whose
            # reservation was cancelled/edited after research.
            if not _in_house_on(rec, report_date):
                continue
            guest = f"{(rec.get('guest_first_name') or '').strip()} {(rec.get('guest_name') or '').strip()}".strip()
            al_panel_rows.append({
                **f,
                "room": rec.get("room"),
                "guest_on_booking": guest,
            })
    # Sort: confirmed first, then needs_review; within tier, by confidence desc.
    al_panel_rows.sort(
        key=lambda a: (
            0 if a.get("review_status") == "confirmed" else 1,
            -(a.get("confidence") or 0),
        )
    )

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
        "booking_com_arrivals": _enrich(flash.booking_com_arrivals),
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
            "zoho_allergies": len(zoho_payload["zoho_allergies"]),
            "zoho_medical": len(zoho_payload["zoho_medical_notes"]),
            "zoho_pending_complaints": len(zoho_payload["zoho_pending_complaints"]),
            "zoho_activities": len(zoho_payload["zoho_todays_activities"]),
        },
        # Phase 24 — zoho_notes-sourced payload sections
        "zoho_allergies":          zoho_payload["zoho_allergies"],
        "zoho_medical_notes":      zoho_payload["zoho_medical_notes"],
        "zoho_pending_complaints": zoho_payload["zoho_pending_complaints"],
        "zoho_todays_activities":  zoho_payload["zoho_todays_activities"],
        "zoho_hsk_summary":        zoho_payload["zoho_hsk_summary"],
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
    # Phase 48 — also re-extract in-house guests whose comment hash changed
    # since the last extraction. Catches mid-stay edits (upsells, late
    # requests) that the original first-ingest-only extraction missed.
    extractions_by_rnid: dict[int, dict] = {}
    extracted_comment_hashes: dict[int, str] = {}
    if run_extract:
        from extract import (
            extract_batch,
            select_extraction_candidates,
            hash_comment,
        )

        # Fetch existing comment hashes for in-house reservations from
        # the previous upload's comment_extractions rows. Best-effort —
        # if the DB query fails, we fall back to arrival-window-only
        # selection (existing behaviour).
        existing_hash_by_rnid: dict[int, str | None] = {}
        try:
            from supa import client as _supa_client
            _sb = _supa_client()
            in_house_rnids = [
                r.get("resv_name_id") for r in records
                if r.get("resv_name_id") is not None
            ]
            if in_house_rnids:
                # Page through in chunks of 500 — Supabase PostgREST has
                # a query length limit on .in_().
                for i in range(0, len(in_house_rnids), 500):
                    chunk = in_house_rnids[i : i + 500]
                    rows = (
                        _sb.table("comment_extractions")
                        .select("resv_name_id, comment_hash")
                        .in_("resv_name_id", chunk)
                        .execute()
                        .data
                        or []
                    )
                    for row in rows:
                        rnid = row.get("resv_name_id")
                        if rnid is not None:
                            existing_hash_by_rnid[rnid] = row.get("comment_hash")
        except Exception as _e:
            print(
                f"       Phase 48 hash prefetch failed (continuing with "
                f"arrival-window only): {type(_e).__name__}: {_e}"
            )
            existing_hash_by_rnid = {}

        in_scope = select_extraction_candidates(
            records, report_date, extract_window_days,
            existing_hash_by_rnid=existing_hash_by_rnid,
        )
        n_arrivals = sum(
            1 for r in in_scope
            if isinstance(r.get("arrival"), datetime)
            and report_date <= r["arrival"].date() <= report_date + timedelta(days=extract_window_days)
        )
        n_in_house = len(in_scope) - n_arrivals
        print(
            f"[2/5] Extracting from {len(in_scope)} in-scope reservations "
            f"({n_arrivals} arrivals + {n_in_house} in-house with changed comments)..."
        )
        extractions, errors, es = asyncio.run(extract_batch(in_scope))
        extractions_by_rnid = {rid: ext.model_dump() for rid, ext in extractions.items()}

        # Phase 48 — capture the comment hashes used for this extraction.
        # Persisted to comment_extractions.comment_hash AFTER the bridge
        # POST so the next cron run can detect changes.
        rnid_to_record = {r.get("resv_name_id"): r for r in in_scope}
        for rnid in extractions_by_rnid:
            rec = rnid_to_record.get(rnid)
            if rec is not None:
                extracted_comment_hashes[rnid] = hash_comment(rec.get("comments"))

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

    # Step 4b — Phase 24: zoho_notes pulled by Lovable's ingest-zoho-notes
    # edge function. We just SELECT and bucket, no ingestion here.
    try:
        zoho_data = fetch_zoho_for_report(report_date)
        print(f"[4b/5] Zoho buckets: {zoho_size_summary(zoho_data)}")
    except Exception as e:
        print(f"[4b/5] zoho fetch failed (continuing without it): {e}")
        zoho_data = {}

    # Phase 50.1 — fetch pool heating data by querying the
    # explore_arrival_detail view directly and applying Phase 49 room-
    # category rules in Python. We previously called the
    # explore_pool_heating RPC, but Lovable's PostgREST schema cache
    # stays stale after the function gets recreated (CREATE OR REPLACE
    # via SQL editor doesn't always trigger a reload). Going through
    # the view bypasses the cache issue and is also more debuggable.
    pool_heating_data: list[dict] = []
    try:
        from collections import defaultdict
        from supa import client as _supa_client
        _sb = _supa_client()
        _end_date = report_date + timedelta(days=14)
        _resp = (
            _sb.from_("explore_arrival_detail")
            .select(
                "room, guest_first_name, guest_name, room_category_label, "
                "arrival_date, departure_date, nights, pool_fence, ce_pool_heating"
            )
            .lte("arrival_date", _end_date.isoformat())
            .gte("departure_date", report_date.isoformat())
            .execute()
        )
        _raw_rows = _resp.data or []

        def _effective_pool_heating(category: str | None, ce_flag) -> bool:
            """Phase 49 — category-based heating rules."""
            cat = (category or "").upper()
            if cat.startswith("V"):
                return True   # Villas always heated, action required
            if cat.startswith("C"):
                return False  # Collection: heated by package, no action
            if cat in ("DLXP", "DJSTEP"):
                return False  # Pool exists but never heatable
            if cat in ("JSTEP", "STEP"):
                return bool(ce_flag)  # On request via comment
            return False  # No private pool

        # Filter to dashboard-eligible rows (heating action OR fence request)
        _filtered: list[dict] = []
        for r in _raw_rows:
            eff = _effective_pool_heating(
                r.get("room_category_label"), r.get("ce_pool_heating")
            )
            if eff or r.get("pool_fence"):
                _filtered.append({**r, "_effective_pool_heating": eff})

        # Group by room (Phase 31.1 dedup — combine same-room reservations)
        _by_room: dict[str, list[dict]] = defaultdict(list)
        for r in _filtered:
            room = r.get("room")
            if room:
                _by_room[room].append(r)

        for room, rows in _by_room.items():
            full_names = sorted(set(
                (
                    (r.get("guest_first_name") or "").strip()
                    + " "
                    + (r.get("guest_name") or "").strip()
                ).strip() or (r.get("guest_name") or "")
                for r in rows
            ))
            guests = sorted({
                (r.get("guest_name") or "").strip()
                for r in rows
                if r.get("guest_name")
            })
            pool_heating_data.append({
                "room": room,
                "guest_name": guests[0] if guests else None,
                "guests": guests,
                "full_names": full_names,
                "occupants": len(full_names),
                "arrival": min(r.get("arrival_date") for r in rows),
                "departure": max(r.get("departure_date") for r in rows),
                "nights": max((r.get("nights") or 0) for r in rows),
                "room_category_label": next(
                    (r.get("room_category_label") for r in rows), None
                ),
                "pool_heating": any(r["_effective_pool_heating"] for r in rows),
                "pool_fence": any(r.get("pool_fence") for r in rows),
            })

        pool_heating_data.sort(
            key=lambda x: ((x.get("arrival") or ""), (x.get("room") or ""))
        )
        print(
            f"[4c/5] Pool heating: {len(pool_heating_data)} rooms over "
            f"{report_date} → {_end_date} "
            f"({len(_raw_rows)} raw rows, {len(_filtered)} eligible)"
        )
    except Exception as _e:
        print(
            f"[4c/5] Pool heating fetch failed (continuing with empty list): "
            f"{type(_e).__name__}: {_e}"
        )

    # Step 5 — assemble + build envelope + POST
    flash_report_payload = assemble_payload_in_memory(
        records, extractions_by_rnid, findings_by_rnid,
        report_date, weather,
        birthdays_override=birthdays_override,
        zoho_data=zoho_data,
        pool_heating=pool_heating_data,
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

    # Phase 48 — persist the comment hashes for the extractions that just
    # landed. Done AFTER the bridge POST because the bridge is responsible
    # for inserting the comment_extractions rows; we just stamp the hash
    # column on each row by reservation_id. Best-effort — failure here
    # doesn't roll back the pipeline; the worst case is the next cron
    # re-extracts in-house guests (a no-op for unchanged comments, plus
    # a small LLM cost).
    if extracted_comment_hashes and resp and resp.get("ok"):
        upload_id = resp.get("upload_id") or (resp.get("verification") or {}).get("upload_id")
        if upload_id:
            try:
                from supa import client as _supa_client
                _sb = _supa_client()
                rows = (
                    _sb.table("reservations")
                    .select("id, resv_name_id")
                    .eq("upload_id", upload_id)
                    .in_("resv_name_id", list(extracted_comment_hashes.keys()))
                    .execute()
                    .data
                    or []
                )
                rid_by_rnid = {row["resv_name_id"]: row["id"] for row in rows}
                stamped = 0
                for rnid, comment_hash in extracted_comment_hashes.items():
                    rid = rid_by_rnid.get(rnid)
                    if not rid:
                        continue
                    try:
                        _sb.table("comment_extractions").update(
                            {"comment_hash": comment_hash}
                        ).eq("reservation_id", rid).execute()
                        stamped += 1
                    except Exception:
                        pass
                print(
                    f"       Phase 48: stamped comment_hash on "
                    f"{stamped}/{len(extracted_comment_hashes)} extraction rows"
                )
            except Exception as _e:
                print(
                    f"       Phase 48 hash persist failed (next run will re-extract "
                    f"in-house guests): {type(_e).__name__}: {_e}"
                )

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
