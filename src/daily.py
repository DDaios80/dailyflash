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
from compute import compute_flash, _in_house_on, merge_zoho_into_flash, is_suppressed_arrival
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
    pool_heating_grid: list[dict] | None = None,
    pool_heating_calendar: dict | None = None,
    pool_fence_other_rooms: list[dict] | None = None,
    pool_cleaning: dict | None = None,
    pool_cleaning_grid: list[dict] | None = None,
    pool_cleaning_calendar: dict | None = None,
    pool_fence_grid: list[dict] | None = None,
    pool_fence_calendar: dict | None = None,
    cribs: dict | None = None,
    cribs_grid: list[dict] | None = None,
    cribs_calendar: dict | None = None,
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
        # Phase 60 follow-up (2026-05-14): legacy `pool_heating` field is
        # soft-deprecated. Always empty going forward. All consumers should
        # use `pool_heating_grid` filtered by `is_heated_today` instead.
        # Kept as an empty list so any forgotten reader gets empty data
        # rather than KeyError. Full removal targeted for ~2026-05-21 after
        # a week of production soak with no complaints.
        "pool_heating": [],
        # Phase 60 — new structures for the redesigned pool-heating UI.
        # `pool_heating_grid` = master list of all 47 heatable rooms with
        # per-room "is heated today" state (red button when true).
        # `pool_heating_calendar` = heated stays in the 14-day window for the
        # Gantt-style calendar above the grid.
        "pool_heating_grid": pool_heating_grid or [],
        "pool_heating_calendar": pool_heating_calendar or {"window": {}, "stays": []},
        # Phase 60.1 — fence requests in non-grid rooms (DLXP, DJSTEP, Collection).
        "pool_fence_other_rooms": pool_fence_other_rooms or [],
        # Phase 61 — pool cleaning forecast for the maintenance coordinator.
        "pool_cleaning": pool_cleaning or {"window": {}, "today": {}, "forecast": [], "all_days": []},
        # Phase 61.1 — same shape as pool_heating_grid / pool_heating_calendar
        # so the explore-page Pool Cleaning panel can mirror the Pool Heating
        # layout (14-day Gantt + 137-button grid grouped by type code).
        "pool_cleaning_grid": pool_cleaning_grid or [],
        "pool_cleaning_calendar": pool_cleaning_calendar or {"window": {}, "stays": []},
        # Phase 61.2 — same layout for fences. Unifies the Phase 60.1 fence
        # data (47-room grid is_fence_today + non-grid pool_fence_other_rooms)
        # into a single 137-button grid and a fence-only calendar.
        "pool_fence_grid": pool_fence_grid or [],
        "pool_fence_calendar": pool_fence_calendar or {"window": {}, "stays": []},
        # Phase 62 — Cribs grid + calendar + summary, clone of the pool
        # heating/cleaning layout for housekeeping crib operations
        # (delivery, pickup, in-room management).
        "cribs": cribs or {"window": {}, "today": {}, "forecast": [], "all_days": []},
        "cribs_grid": cribs_grid or [],
        "cribs_calendar": cribs_calendar or {"window": {}, "stays": []},
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

        # 2026-07-31 — extraction cache (replaces Phase 60.5 force-all and
        # the never-functional Phase 48 DB stamping, which read/wrote the
        # WRONG database: the supa client points at the old April project,
        # not the prod DB the ingest edge fn writes to). The cache lives in
        # app_settings (key extraction_cache_v1) in the store the pipeline
        # already reliably uses for its heartbeat/marker/token:
        #   { "<resv_name_id>": {"h": <comment sha256>, "e": {extraction}} }
        # Guests whose comment hash is unchanged skip the LLM entirely and
        # reuse the cached extraction in the envelope — arrivals included,
        # since an identical comment yields an identical extraction.
        # Cache-load failure degrades to the previous behaviour (re-extract
        # every in-house guest + all arrivals).
        _cache: dict[int, dict] = {}
        try:
            import json as _json
            from supa import client as _supa_client_cache
            _row = (
                _supa_client_cache()
                .table("app_settings")
                .select("value")
                .eq("key", "extraction_cache_v1")
                .maybe_single()
                .execute()
            )
            _raw = ((getattr(_row, "data", None) or {}).get("value") or "").strip()
            if _raw:
                _cache = {int(k): v for k, v in _json.loads(_raw).items()
                          if isinstance(v, dict) and v.get("h") and isinstance(v.get("e"), dict)}
            print(f"       extraction cache: {len(_cache)} entries loaded")
        except Exception as _e:
            print(f"       extraction cache load failed (re-extracting all): "
                  f"{type(_e).__name__}: {_e}")
            _cache = {}

        existing_hash_by_rnid: dict[int, str | None] = {}
        try:
            for _r in records:
                _arr = _r.get("arrival")
                _dep = _r.get("departure")
                if isinstance(_arr, datetime):
                    _arr = _arr.date()
                if isinstance(_dep, datetime):
                    _dep = _dep.date()
                if not isinstance(_arr, date) or not isinstance(_dep, date):
                    continue
                if not (_arr <= report_date <= _dep):
                    continue
                _rnid = _r.get("resv_name_id")
                if _rnid is not None:
                    # Known hash gates on change; None forces re-extraction.
                    existing_hash_by_rnid[_rnid] = (_cache.get(_rnid) or {}).get("h")
        except Exception as _e:
            print(
                f"       in-house map failed (continuing with "
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

        # Split candidates: reuse the cached extraction when the comment is
        # byte-identical (normalised) to what was last extracted.
        _to_extract: list[dict] = []
        _reused: dict[int, dict] = {}
        for _r in in_scope:
            _rnid = _r.get("resv_name_id")
            _entry = _cache.get(_rnid) if _rnid is not None else None
            if _entry and _entry.get("h") == hash_comment(_r.get("comments")):
                _reused[_rnid] = _entry["e"]
            else:
                _to_extract.append(_r)
        print(
            f"[2/5] Extracting from {len(_to_extract)} of {len(in_scope)} in-scope "
            f"reservations ({n_arrivals} arrivals + {n_in_house} in-house with "
            f"changed comments; {len(_reused)} unchanged, reused from cache)..."
        )
        extractions, errors, es = asyncio.run(extract_batch(_to_extract))
        extractions_by_rnid = {rid: ext.model_dump() for rid, ext in extractions.items()}
        extractions_by_rnid.update(_reused)

        print(f"       {es['succeeded']}/{es['total']} ok, {es['failed']} failed"
              f" (+{len(_reused)} from cache)")
    else:
        print(f"[2/5] --no-extract (skipped)")

    # Phase 52 — preserve comment_extractions across upload replacement.
    # When run_extract is False (--quick mode), no new extractions are
    # generated. The bridge's _replace_existing_upload then cascade-
    # deletes the prior reservations and their comment_extractions,
    # leaving the new reservations bare. Result: in-house notes
    # disappear from the dashboard until the next full nightly cron.
    #
    # Fix: fetch existing comment_extractions from DB before bridge
    # call, populate extractions_by_rnid with them. Bridge then re-
    # inserts them attached to the new reservation_ids via the
    # resv_name_id mapping. No data loss across daily uploads.
    #
    # Best-effort. If the DB fetch fails (RLS, network), we fall
    # through to the old behaviour — no preservation, but no breakage.
    if not extractions_by_rnid:
        # 2026-07-31 — preserve from the extraction cache first: it holds the
        # results of OUR last successful extraction. The DB path below reads
        # the supa-client project, which is NOT where ingests land (it served
        # April-era rows on 2026-07-31) — kept only as a last resort.
        try:
            import json as _json_p52
            from supa import client as _supa_client_p52
            _row = (
                _supa_client_p52()
                .table("app_settings")
                .select("value")
                .eq("key", "extraction_cache_v1")
                .maybe_single()
                .execute()
            )
            _raw = ((getattr(_row, "data", None) or {}).get("value") or "").strip()
            if _raw:
                _c = _json_p52.loads(_raw)
                _rnids_now = {r.get("resv_name_id") for r in records}
                _from_cache = {
                    int(k): v["e"] for k, v in _c.items()
                    if isinstance(v, dict) and isinstance(v.get("e"), dict)
                    and int(k) in _rnids_now
                }
                if _from_cache:
                    extractions_by_rnid = _from_cache
                    print(
                        f"       Phase 52: preserved {len(_from_cache)} "
                        f"comment_extractions from the extraction cache "
                        f"(skipped fresh extraction in --quick mode)"
                    )
        except Exception as _e:
            print(f"       Phase 52 cache preservation failed (trying DB): "
                  f"{type(_e).__name__}: {_e}")

    if not extractions_by_rnid:
        try:
            from supa import client as _supa_client
            _sb = _supa_client()
            _all_rnids = [
                r.get("resv_name_id") for r in records
                if r.get("resv_name_id") is not None
            ]
            _preserved: dict[int, dict] = {}
            for _i in range(0, len(_all_rnids), 500):
                _chunk = _all_rnids[_i : _i + 500]
                # Step 1 — get reservation_id ↔ resv_name_id mapping
                _rmap_rows = (
                    _sb.from_("reservations")
                    .select("id, resv_name_id")
                    .in_("resv_name_id", _chunk)
                    .execute()
                    .data
                    or []
                )
                _rid_to_rnid: dict[str, int] = {
                    row["id"]: row["resv_name_id"]
                    for row in _rmap_rows
                    if row.get("id") and row.get("resv_name_id") is not None
                }
                if not _rid_to_rnid:
                    continue
                # Step 2 — fetch existing extractions for those reservation_ids
                _ce_rows = (
                    _sb.from_("comment_extractions")
                    .select(
                        "reservation_id, allergies_present, allergies_text, "
                        "pool_fence, pool_heating, free_transfer, free_upgrade, "
                        "lco, honeymoon, amenities, ops_notes"
                    )
                    .in_("reservation_id", list(_rid_to_rnid.keys()))
                    .execute()
                    .data
                    or []
                )
                # Step 3 — map back to resv_name_id, build extraction dict
                for _ce in _ce_rows:
                    _rid = _ce.get("reservation_id")
                    _rnid = _rid_to_rnid.get(_rid)
                    if _rnid is None:
                        continue
                    _preserved[_rnid] = {
                        "allergies_present": _ce.get("allergies_present"),
                        "allergies_text": _ce.get("allergies_text"),
                        "pool_fence": _ce.get("pool_fence"),
                        "pool_heating": _ce.get("pool_heating"),
                        "free_transfer": _ce.get("free_transfer"),
                        "free_upgrade": _ce.get("free_upgrade"),
                        # Bridge maps "lco" through; some legacy paths
                        # accept either "lco" or "late_checkout".
                        "lco": _ce.get("lco"),
                        "late_checkout": _ce.get("lco"),
                        "honeymoon": _ce.get("honeymoon"),
                        "amenities": _ce.get("amenities") or [],
                        "ops_notes": _ce.get("ops_notes"),
                    }
            if _preserved:
                extractions_by_rnid = _preserved
                print(
                    f"       Phase 52: preserved {len(_preserved)} "
                    f"comment_extractions from prior upload "
                    f"(skipped fresh extraction in --quick mode)"
                )
        except Exception as _e:
            print(
                f"       Phase 52 preservation failed (continuing without): "
                f"{type(_e).__name__}: {_e}"
            )

    # Phase 48 — capture the comment hashes for every entry in
    # extractions_by_rnid (whether fresh from this run or preserved from
    # Phase 52). Persisted to comment_extractions.comment_hash AFTER
    # the bridge POST so the next cron's diff check works correctly.
    if extractions_by_rnid:
        from extract import hash_comment as _hash_comment_fn
        _record_by_rnid = {r.get("resv_name_id"): r for r in records}
        for _rnid in extractions_by_rnid:
            _rec = _record_by_rnid.get(_rnid)
            if _rec is not None:
                extracted_comment_hashes[_rnid] = _hash_comment_fn(
                    _rec.get("comments")
                )

    # Step 3 — A-lister
    findings_by_rnid: dict[int, list[dict]] = {}
    researched_subjects_list: list[dict] = []
    if run_alister:
        today_arrivals = [
            r for r in records
            if isinstance(r.get("arrival"), datetime) and r["arrival"].date() == report_date
            and not is_suppressed_arrival(r)
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

    # Phase 50.2 — compute pool heating data from in-memory records.
    # Both the RPC and view-query paths failed due to PostgREST schema
    # cache issues on Lovable. Records are the parsed xlsx data we
    # already have in memory, so we apply heating rules directly
    # without touching the DB. No PostgREST cache dependency.
    #
    # Phase 60 — new ``effective_pool_heating_v60`` rule: heating service
    # follows what was BOOKED, not the actual assigned room. Upgrades from
    # non-heatable categories don't get heating service. Plus we now emit
    # two new structures (``pool_heating_grid`` for the 47-button master
    # grid, ``pool_heating_calendar`` for the Gantt) alongside the legacy
    # ``pool_heating`` list — the legacy list is kept for one cycle while
    # the dashboard migrates to the new structures.
    #
    # pool_fence comes from current-run extractions_by_rnid (populated
    # when run_extract=True). On --quick runs, fence will be empty
    # until the next full cron repopulates comment_extractions.
    pool_heating_data: list[dict] = []
    pool_heating_grid: list[dict] = []
    pool_heating_calendar: dict = {"window": {}, "stays": []}
    pool_fence_other_rooms: list[dict] = []  # Phase 60.1 — fence in non-grid rooms
    try:
        from collections import defaultdict
        from heatable_rooms import (
            HEATABLE_ROOMS,
            HEATABLE_ROOM_NUMBERS,
            effective_pool_heating_v60,
        )

        # Phase 60.7 — fetch manual overrides from pool_heating_overrides
        # (created by Lovable in the override-UI work). Each row carries:
        #   reservation_id (uuid → reservations.id)
        #   override_type: 'heating' or 'fence'
        #   forced_value: bool
        # We embed reservations.resv_name_id so we can match against the
        # records dict (which has resv_name_id from the xlsx, not the
        # Supabase UUID). Currently PostgREST schema cache may not see this
        # table (PGRST205) — that's the same persistent issue as zoho_notes.
        # Falls back to no-op if fetch fails: the grid still renders, just
        # without override merge. Once PostgREST restarts, overrides flow.
        overrides_by_rnid: dict[int, dict[str, bool]] = {}
        try:
            from supa import client as _supa_override_client
            _osb = _supa_override_client()
            _override_rows = (
                _osb.table("pool_heating_overrides")
                .select(
                    "override_type, forced_value, reservations!inner(resv_name_id)"
                )
                .execute()
                .data
                or []
            )
            for _ov in _override_rows:
                _rnid = (_ov.get("reservations") or {}).get("resv_name_id")
                _otype = _ov.get("override_type")
                _val = _ov.get("forced_value")
                if _rnid is None or _otype not in ("heating", "fence") or _val is None:
                    continue
                overrides_by_rnid.setdefault(_rnid, {})[_otype] = bool(_val)
            print(
                f"       Phase 60.7: loaded {len(overrides_by_rnid)} reservation overrides "
                f"from pool_heating_overrides"
            )
        except Exception as _ov_e:
            print(
                f"       Phase 60.7 override fetch failed (continuing without "
                f"overrides — grid uses LLM-derived state only): "
                f"{type(_ov_e).__name__}: {_ov_e}"
            )
            overrides_by_rnid = {}

        # Phase 60 calendar window: yesterday + today (report_date) + 12 days forward.
        _window_start = report_date - timedelta(days=1)
        _window_end = report_date + timedelta(days=12)
        # Legacy compat: previous code used a +14d window for the legacy
        # ``pool_heating`` list. Keep the same window for the legacy list.
        _legacy_end = report_date + timedelta(days=14)

        _eligible: list[dict] = []  # legacy list rows (effective heated OR pool_fence)
        _window_stays: list[dict] = []  # Phase 60.1 — stays with heating OR fence in heatable rooms
        _other_fence_stays: list[dict] = []  # Phase 60.1 — fence in non-grid rooms
        _raw_count = 0
        for _r in records:
            _arrival = _r.get("arrival")
            _departure = _r.get("departure")
            if isinstance(_arrival, datetime):
                _arrival = _arrival.date()
            if isinstance(_departure, datetime):
                _departure = _departure.date()
            if not isinstance(_arrival, date) or not isinstance(_departure, date):
                continue
            if not (_arrival <= _legacy_end and _departure >= _window_start):
                continue
            _raw_count += 1

            _rnid = _r.get("resv_name_id")
            _ext = extractions_by_rnid.get(_rnid, {}) if _rnid is not None else {}
            _ce_heating = bool(_ext.get("pool_heating", False))
            _pool_fence = bool(_ext.get("pool_fence", False))

            _booked_cat = _r.get("booked_room_category_label")
            _actual_cat = _r.get("room_category_label")
            _eff = effective_pool_heating_v60(_booked_cat, _actual_cat, _ce_heating)

            _room = _r.get("room")
            _full_name = (
                (_r.get("guest_first_name") or "").strip()
                + " "
                + (_r.get("guest_name") or "").strip()
            ).strip() or (_r.get("guest_name") or "")

            # Phase 60.7 — apply per-reservation manual overrides on top of
            # the LLM-derived state. Override is the source of truth when
            # set; ``auto_*`` keeps the LLM-derived value for the UI to
            # show "this is overridden" markers.
            _ov = overrides_by_rnid.get(_rnid, {}) if _rnid is not None else {}
            _ov_heating = _ov.get("heating")  # None | bool
            _ov_fence = _ov.get("fence")
            _effective_heated = _ov_heating if _ov_heating is not None else bool(_eff)
            _effective_fence = _ov_fence if _ov_fence is not None else bool(_pool_fence)

            # Phase 60.1 — collect stays with heating OR fence (effective,
            # post-override) in the 14-day window. Bucket by whether the
            # room is on the heatable grid.
            if (
                (_effective_heated or _effective_fence)
                and _arrival <= _window_end
                and _departure >= _window_start
            ):
                _stay = {
                    "room": _room,
                    "guest_name": _r.get("guest_name"),
                    "guest_full_name": _full_name,
                    "arrival": _arrival.isoformat(),
                    "departure": _departure.isoformat(),
                    "nights": (_departure - _arrival).days,
                    "booked_room_category_label": _booked_cat,
                    "room_category_label": _actual_cat,
                    "heated": _effective_heated,
                    "fence": _effective_fence,
                    # Phase 60.7 override transparency:
                    "auto_heated": bool(_eff),
                    "auto_fence": bool(_pool_fence),
                    "override_heating": _ov_heating,  # None | bool
                    "override_fence": _ov_fence,
                }
                if _room in HEATABLE_ROOM_NUMBERS:
                    _window_stays.append(_stay)
                elif _effective_fence:
                    # Non-grid room with a fence request: surface separately.
                    # (Heating in non-grid rooms is impossible by rule, so we
                    # only need the fence path here.)
                    _other_fence_stays.append(_stay)

            # Legacy ``pool_heating`` list: same shape as before for backward compat.
            if _eff or _pool_fence:
                if _arrival <= _legacy_end and _departure >= report_date:
                    _eligible.append({
                        "room": _room,
                        "guest_first_name": _r.get("guest_first_name"),
                        "guest_name": _r.get("guest_name"),
                        "room_category_label": _actual_cat,
                        "arrival_date": _arrival,
                        "departure_date": _departure,
                        "nights": (_departure - _arrival).days,
                        "_effective_pool_heating": _eff,
                        "pool_fence": _pool_fence,
                    })

        # Group legacy list by room (Phase 31.1 dedup pattern).
        _by_room: dict[str, list[dict]] = defaultdict(list)
        for _r in _eligible:
            _room = _r.get("room")
            if _room:
                _by_room[_room].append(_r)

        for _room, _rows in _by_room.items():
            _full_names = sorted({
                (
                    (_x.get("guest_first_name") or "").strip()
                    + " "
                    + (_x.get("guest_name") or "").strip()
                ).strip() or (_x.get("guest_name") or "")
                for _x in _rows
            })
            _guests = sorted({
                (_x.get("guest_name") or "").strip()
                for _x in _rows
                if _x.get("guest_name")
            })
            pool_heating_data.append({
                "room": _room,
                "guest_name": _guests[0] if _guests else None,
                "guests": _guests,
                "full_names": _full_names,
                "occupants": len(_full_names),
                "arrival": min(_x["arrival_date"] for _x in _rows).isoformat(),
                "departure": max(_x["departure_date"] for _x in _rows).isoformat(),
                "nights": max((_x.get("nights") or 0) for _x in _rows),
                "room_category_label": next(
                    (_x.get("room_category_label") for _x in _rows), None
                ),
                "pool_heating": any(_x["_effective_pool_heating"] for _x in _rows),
                "pool_fence": any(_x.get("pool_fence") for _x in _rows),
            })

        pool_heating_data.sort(
            key=lambda x: ((x.get("arrival") or ""), (x.get("room") or ""))
        )

        # Phase 60 — build the master 47-room grid with two indicators per
        # button: is_heated_today (red bg) and is_fence_today (icon overlay).
        # "today" = report_date is in [arrival, departure).
        # Phase 60.7 — also surface per-room override flags so the UI can
        # show a visual marker on overridden buttons (e.g. small dot).
        _today_iso = report_date.isoformat()
        _heated_rooms_today: set[str] = set()
        _fence_rooms_today: set[str] = set()
        # room → today's in-house stay (for override transparency)
        _today_stay_by_room: dict[str, dict] = {}
        for _s in _window_stays:
            if _s["arrival"] <= _today_iso < _s["departure"]:
                if _s["heated"]:
                    _heated_rooms_today.add(_s["room"])
                if _s["fence"]:
                    _fence_rooms_today.add(_s["room"])
                _today_stay_by_room[_s["room"]] = _s

        for _hr in HEATABLE_ROOMS:
            _today_stay = _today_stay_by_room.get(_hr["room"])
            pool_heating_grid.append({
                "room": _hr["room"],
                "type_code": _hr["type_code"],
                "description": _hr["description"],
                "is_heated_today": _hr["room"] in _heated_rooms_today,
                "is_fence_today": _hr["room"] in _fence_rooms_today,
                # Phase 60.7 — override transparency. None when no in-house
                # stay or no override; bool when an override is active.
                "override_heating": (_today_stay or {}).get("override_heating"),
                "override_fence":   (_today_stay or {}).get("override_fence"),
                "auto_heated":      (_today_stay or {}).get("auto_heated"),
                "auto_fence":       (_today_stay or {}).get("auto_fence"),
            })

        # Phase 60 — calendar payload (Gantt source data).
        # Stays carry both ``heated`` and ``fence`` booleans (at least one is
        # true). Sorted by arrival date then room for deterministic rendering.
        _window_stays.sort(key=lambda s: (s["arrival"], s["room"]))
        pool_heating_calendar = {
            "window": {
                "start": _window_start.isoformat(),
                "end": _window_end.isoformat(),
                "anchor": report_date.isoformat(),
                "days": (_window_end - _window_start).days + 1,
            },
            "stays": _window_stays,
        }

        # Phase 60.1 — fence requests in non-grid rooms (DLXP, DJSTEP,
        # Collection categories). Surfaced separately so the housekeeping
        # team doesn't lose visibility on fence work that won't appear in
        # the heatable-rooms grid.
        _other_fence_stays.sort(key=lambda s: (s["arrival"], s["room"]))
        pool_fence_other_rooms = [
            {**_s, "in_house_today": _s["arrival"] <= _today_iso < _s["departure"]}
            for _s in _other_fence_stays
        ]

        _override_in_use = sum(
            1 for _g in pool_heating_grid
            if _g.get("override_heating") is not None or _g.get("override_fence") is not None
        )
        print(
            f"[4c/5] Pool heating: legacy={len(pool_heating_data)} rooms, "
            f"grid={len(pool_heating_grid)} buttons "
            f"({len(_heated_rooms_today)} red, {len(_fence_rooms_today)} fence today, "
            f"{_override_in_use} overridden), "
            f"calendar={len(_window_stays)} stays, "
            f"other-fence={len(pool_fence_other_rooms)} non-grid stays over "
            f"{_window_start} → {_window_end} ({_raw_count} active in window)"
        )
    except Exception as _e:
        print(
            f"[4c/5] Pool heating compute failed (continuing with empty data): "
            f"{type(_e).__name__}: {_e}"
        )

    # Phase 61 — pool cleaning forecast for the maintenance coordinator.
    # Counts occupied private-pool rooms per day (137-room inventory).
    # Distinct from heating: heating only applies to 47 rooms; cleaning to
    # all 137. DLXP and DJSTEP are aliased to DLXP (same physical inventory,
    # different rate plan: HB vs Residents' Club). C2BSP combined-suite
    # bookings count as 1 cleaning per booking (we count distinct occupied
    # room numbers; Opera typically books C2BSP under one of the two
    # component room numbers).
    pool_cleaning: dict = {"window": {}, "today": {}, "forecast": [], "all_days": []}
    try:
        from collections import defaultdict
        from pool_rooms import POOL_ROOM_NUMBERS, display_category_for_cleaning

        _cleaning_days = 7
        _cleaning_window_end = report_date + timedelta(days=_cleaning_days - 1)

        # Pre-coerce dates on records (some are datetimes, some dates).
        _record_dates = []
        for _r in records:
            _arr = _r.get("arrival")
            _dep = _r.get("departure")
            if isinstance(_arr, datetime):
                _arr = _arr.date()
            if isinstance(_dep, datetime):
                _dep = _dep.date()
            if not isinstance(_arr, date) or not isinstance(_dep, date):
                continue
            _room = _r.get("room")
            if not _room or _room not in POOL_ROOM_NUMBERS:
                continue
            # For the breakdown: prefer the BOOKED category if it bears a pool
            # (preserves DLXP vs DJSTEP rate-plan distinction the coordinator
            # cares about). Else fall back to actual (collapses DLX→DLXP and
            # similar upgrades into the operationally relevant pool category).
            _display_cat = display_category_for_cleaning(
                _r.get("booked_room_category_label"),
                _r.get("room_category_label"),
                _room,
            ) or "UNKNOWN"
            _record_dates.append((_arr, _dep, _room, _display_cat))

        _all_days: list[dict] = []
        for _offset in range(_cleaning_days):
            _d = report_date + timedelta(days=_offset)
            _occupied_rooms: set[str] = set()
            _by_category: dict[str, int] = defaultdict(int)
            for _arr, _dep, _room, _cat in _record_dates:
                if not (_arr <= _d < _dep):
                    continue
                if _room in _occupied_rooms:
                    continue  # dedupe (multiple reservations same day same room)
                _occupied_rooms.add(_room)
                _by_category[_cat] += 1
            _all_days.append({
                "date": _d.isoformat(),
                "count": len(_occupied_rooms),
                "by_category": dict(sorted(_by_category.items())),
            })

        pool_cleaning = {
            "window": {
                "start": report_date.isoformat(),
                "end": _cleaning_window_end.isoformat(),
                "days": _cleaning_days,
            },
            "today": _all_days[0],
            "forecast": _all_days[1:],
            "all_days": _all_days,
        }

        # Phase 61.1 — mirror the Phase 60 heating layout for cleaning:
        # `pool_cleaning_grid` (137 buttons grouped by type) +
        # `pool_cleaning_calendar` (14-day Gantt of pool-room stays).
        # Window matches the heating calendar exactly so the two panels
        # render side by side with the same time axis.
        from pool_rooms import POOL_ROOMS, primary_category
        _pc_window_start = report_date - timedelta(days=1)
        _pc_window_end = report_date + timedelta(days=12)
        _pc_today_iso = report_date.isoformat()

        # Collect every in-pool-room stay overlapping the 14-day window.
        _pc_stays: list[dict] = []
        for _r in records:
            _arr = _r.get("arrival")
            _dep = _r.get("departure")
            if isinstance(_arr, datetime):
                _arr = _arr.date()
            if isinstance(_dep, datetime):
                _dep = _dep.date()
            if not isinstance(_arr, date) or not isinstance(_dep, date):
                continue
            _room = _r.get("room")
            if not _room or _room not in POOL_ROOM_NUMBERS:
                continue
            if not (_arr <= _pc_window_end and _dep >= _pc_window_start):
                continue
            _pc_full_name = (
                (_r.get("guest_first_name") or "").strip()
                + " "
                + (_r.get("guest_name") or "").strip()
            ).strip() or (_r.get("guest_name") or "")
            _pc_stays.append({
                "room": _room,
                "guest_name": _r.get("guest_name"),
                "guest_full_name": _pc_full_name,
                "arrival": _arr.isoformat(),
                "departure": _dep.isoformat(),
                "nights": (_dep - _arr).days,
                "type_code": primary_category(_room),
                "booked_room_category_label": _r.get("booked_room_category_label"),
                "room_category_label": _r.get("room_category_label"),
            })

        # Today's occupied set drives is_to_clean_today on the grid.
        _pc_occupied_today: set[str] = set()
        for _s in _pc_stays:
            if _s["arrival"] <= _pc_today_iso < _s["departure"]:
                _pc_occupied_today.add(_s["room"])

        # 137-button grid in registry order (same shape as pool_heating_grid).
        pool_cleaning_grid: list[dict] = []
        for _pr in POOL_ROOMS:
            pool_cleaning_grid.append({
                "room": _pr["room"],
                "type_code": _pr["type_code"],
                "description": _pr["description"],
                "heatable": _pr["heatable"],
                "is_to_clean_today": _pr["room"] in _pc_occupied_today,
            })

        # Calendar payload (Gantt source) — every in-pool-room stay in window.
        _pc_stays.sort(key=lambda s: (s["arrival"], s["room"]))
        pool_cleaning_calendar: dict = {
            "window": {
                "start": _pc_window_start.isoformat(),
                "end": _pc_window_end.isoformat(),
                "anchor": report_date.isoformat(),
                "days": (_pc_window_end - _pc_window_start).days + 1,
            },
            "stays": _pc_stays,
        }

        print(
            f"[4d/5] Pool cleaning: today={pool_cleaning['today']['count']} pools to clean, "
            f"7-day total={sum(d['count'] for d in _all_days)} cleanings | "
            f"grid={len(pool_cleaning_grid)} buttons "
            f"({len(_pc_occupied_today)} occupied today), "
            f"calendar={len(_pc_stays)} stays over {_pc_window_start} → {_pc_window_end}"
        )
    except Exception as _e:
        print(
            f"[4d/5] Pool cleaning compute failed (continuing with empty data): "
            f"{type(_e).__name__}: {_e}"
        )
        # Ensure the new fields exist even when compute fails so downstream
        # rendering doesn't crash on missing keys.
        try:
            pool_cleaning_grid  # type: ignore[name-defined]
        except NameError:
            pool_cleaning_grid = []
        try:
            pool_cleaning_calendar  # type: ignore[name-defined]
        except NameError:
            pool_cleaning_calendar = {"window": {}, "stays": []}

    # Phase 61.2 — pool fence grid + calendar (mirror Phase 60 heating layout
    # for fences). Same 137-button universe as cleaning since fence requests
    # can come from any private-pool room — heatable or not. Reuses the
    # already-computed pool_heating_grid[].is_fence_today (heatable rooms)
    # and pool_fence_other_rooms (non-heatable) to merge into a single grid.
    pool_fence_grid: list[dict] = []
    pool_fence_calendar: dict = {"window": {}, "stays": []}
    try:
        from pool_rooms import POOL_ROOMS as _PR_ALL, primary_category as _pc_primary

        # Set of rooms with an active fence request today (in-house tonight).
        _fence_today_rooms: set[str] = set()
        for _g in pool_heating_grid:
            if _g.get("is_fence_today"):
                _fence_today_rooms.add(_g["room"])
        for _o in pool_fence_other_rooms:
            if _o.get("in_house_today"):
                _fence_today_rooms.add(_o["room"])

        # 137-button grid in registry order — identical shape to
        # pool_cleaning_grid except the boolean is_fence_today.
        for _pr in _PR_ALL:
            pool_fence_grid.append({
                "room": _pr["room"],
                "type_code": _pr["type_code"],
                "description": _pr["description"],
                "heatable": _pr["heatable"],
                "is_fence_today": _pr["room"] in _fence_today_rooms,
            })

        # Calendar — fence stays only across both sources, deduped by
        # (room, arrival, departure). Same window as heating/cleaning.
        _fence_stays_dedup: dict[tuple, dict] = {}
        for _s in pool_heating_calendar.get("stays", []):
            if not _s.get("fence"):
                continue
            _key = (_s.get("room"), _s.get("arrival"), _s.get("departure"))
            _fs = dict(_s)
            if "type_code" not in _fs:
                _fs["type_code"] = _pc_primary(_fs.get("room"))
            _fence_stays_dedup[_key] = _fs
        for _o in pool_fence_other_rooms:
            _key = (_o.get("room"), _o.get("arrival"), _o.get("departure"))
            if _key in _fence_stays_dedup:
                continue
            _fs = dict(_o)
            if "type_code" not in _fs:
                _fs["type_code"] = _pc_primary(_fs.get("room"))
            # Normalize the shape — non-grid stays don't have heated/fence
            # booleans by default, but the calendar consumer may expect them.
            _fs.setdefault("fence", True)
            _fs.setdefault("heated", False)
            _fence_stays_dedup[_key] = _fs

        _fence_stays_list = sorted(
            _fence_stays_dedup.values(),
            key=lambda s: (s.get("arrival") or "", s.get("room") or ""),
        )

        pool_fence_calendar = {
            "window": dict(pool_heating_calendar.get("window") or {}),
            "stays": _fence_stays_list,
        }

        print(
            f"[4e/5] Pool fence: grid={len(pool_fence_grid)} buttons "
            f"({len(_fence_today_rooms)} fence today), "
            f"calendar={len(_fence_stays_list)} fence stays in window"
        )
    except Exception as _e:
        print(
            f"[4e/5] Pool fence grid compute failed (continuing with empty): "
            f"{type(_e).__name__}: {_e}"
        )

    # Phase 62 — Cribs grid + calendar (clone of Phase 60/61 layout) for
    # housekeeping crib delivery / pickup planning. Unlike pool heating
    # (fixed 47-room registry) or pool cleaning (fixed 137-room registry),
    # cribs are mobile equipment — any room can request one. So the grid
    # is dynamic: only rooms with cribs in the 14-day window appear.
    cribs: dict = {"window": {}, "today": {}, "forecast": [], "all_days": []}
    cribs_grid: list[dict] = []
    cribs_calendar: dict = {"window": {}, "stays": []}
    try:
        from collections import defaultdict as _cribs_dd

        _cb_window_start = report_date - timedelta(days=1)
        _cb_window_end = report_date + timedelta(days=12)
        _cb_today_iso = report_date.isoformat()

        # Collect every stay in the window with cribs > 0.
        _cb_stays: list[dict] = []
        for _r in records:
            _arr = _r.get("arrival")
            _dep = _r.get("departure")
            if isinstance(_arr, datetime):
                _arr = _arr.date()
            if isinstance(_dep, datetime):
                _dep = _dep.date()
            if not isinstance(_arr, date) or not isinstance(_dep, date):
                continue
            _crib_count = _r.get("cribs") or 0
            try:
                _crib_count = int(_crib_count)
            except (ValueError, TypeError):
                _crib_count = 0
            if _crib_count <= 0:
                continue
            if not (_arr <= _cb_window_end and _dep >= _cb_window_start):
                continue
            _room = _r.get("room")
            _full_name = (
                (_r.get("guest_first_name") or "").strip()
                + " "
                + (_r.get("guest_name") or "").strip()
            ).strip() or (_r.get("guest_name") or "")
            _children = _r.get("children") or 0
            try:
                _children = int(_children)
            except (ValueError, TypeError):
                _children = 0
            _cb_stays.append({
                "room": _room,
                "guest_name": _r.get("guest_name"),
                "guest_full_name": _full_name,
                "arrival": _arr.isoformat(),
                "departure": _dep.isoformat(),
                "nights": (_dep - _arr).days,
                "cribs": _crib_count,
                "children": _children,
                "room_category_label": _r.get("room_category_label"),
                "booked_room_category_label": _r.get("booked_room_category_label"),
            })

        # Today's crib state: rooms with at least one crib in-house tonight,
        # plus the count of cribs per room (multi-crib bookings supported).
        _cb_today_by_room: dict[str, dict] = {}
        _cb_today_total = 0
        for _s in _cb_stays:
            if not (_s["arrival"] <= _cb_today_iso < _s["departure"]):
                continue
            _room = _s["room"]
            if not _room:
                continue
            if _room not in _cb_today_by_room:
                _cb_today_by_room[_room] = {
                    "room": _room,
                    "cribs": 0,
                    "children": 0,
                    "guests": [],
                    "guest_full_name": _s["guest_full_name"],
                }
            _cb_today_by_room[_room]["cribs"] += _s["cribs"]
            _cb_today_by_room[_room]["children"] += _s["children"]
            _cb_today_by_room[_room]["guests"].append(_s["guest_full_name"])
            _cb_today_total += _s["cribs"]

        # 7-day forecast (matches pool_cleaning shape).
        _cb_forecast_days = 7
        _cb_all_days: list[dict] = []
        for _offset in range(_cb_forecast_days):
            _d = report_date + timedelta(days=_offset)
            _d_iso = _d.isoformat()
            _day_rooms: set[str] = set()
            _day_total = 0
            for _s in _cb_stays:
                if _s["arrival"] <= _d_iso < _s["departure"] and _s["room"]:
                    _day_rooms.add(_s["room"])
                    _day_total += _s["cribs"]
            _cb_all_days.append({
                "date": _d_iso,
                "cribs": _day_total,
                "rooms": len(_day_rooms),
            })

        # Dynamic grid: every room with at least one crib stay in the
        # window. Each entry carries today's crib count (0 if not in-house
        # tonight) and the total stays touching the window.
        _rooms_in_window: dict[str, dict] = {}
        for _s in _cb_stays:
            _room = _s["room"]
            if not _room:
                continue
            if _room not in _rooms_in_window:
                _rooms_in_window[_room] = {
                    "room": _room,
                    "stays_in_window": 0,
                    "max_cribs_in_window": 0,
                }
            _rooms_in_window[_room]["stays_in_window"] += 1
            _rooms_in_window[_room]["max_cribs_in_window"] = max(
                _rooms_in_window[_room]["max_cribs_in_window"], _s["cribs"]
            )

        for _room, _info in sorted(_rooms_in_window.items()):
            _today_entry = _cb_today_by_room.get(_room) or {}
            cribs_grid.append({
                "room": _room,
                "is_crib_today": bool(_today_entry),
                "cribs_today": _today_entry.get("cribs", 0),
                "children_today": _today_entry.get("children", 0),
                "stays_in_window": _info["stays_in_window"],
                "max_cribs_in_window": _info["max_cribs_in_window"],
            })

        # Calendar (Gantt source) — every crib stay in the window, sorted
        # by arrival then room.
        _cb_stays.sort(key=lambda s: (s["arrival"], s["room"] or ""))
        cribs_calendar = {
            "window": {
                "start": _cb_window_start.isoformat(),
                "end": _cb_window_end.isoformat(),
                "anchor": report_date.isoformat(),
                "days": (_cb_window_end - _cb_window_start).days + 1,
            },
            "stays": _cb_stays,
        }

        cribs = {
            "window": {
                "start": report_date.isoformat(),
                "end": (report_date + timedelta(days=_cb_forecast_days - 1)).isoformat(),
                "days": _cb_forecast_days,
            },
            "today": {
                "date": _cb_today_iso,
                "count": _cb_today_total,
                "rooms": sorted(
                    _cb_today_by_room.values(),
                    key=lambda r: r["room"] or "",
                ),
            },
            "forecast": _cb_all_days[1:],
            "all_days": _cb_all_days,
        }

        print(
            f"[4f/5] Cribs: today={_cb_today_total} cribs in {len(_cb_today_by_room)} rooms, "
            f"grid={len(cribs_grid)} rooms with cribs in window, "
            f"calendar={len(_cb_stays)} stays over "
            f"{_cb_window_start} → {_cb_window_end}"
        )
    except Exception as _e:
        print(
            f"[4f/5] Cribs compute failed (continuing with empty data): "
            f"{type(_e).__name__}: {_e}"
        )

    # Step 5 — assemble + build envelope + POST
    flash_report_payload = assemble_payload_in_memory(
        records, extractions_by_rnid, findings_by_rnid,
        report_date, weather,
        birthdays_override=birthdays_override,
        zoho_data=zoho_data,
        pool_heating=pool_heating_data,
        pool_heating_grid=pool_heating_grid,
        pool_heating_calendar=pool_heating_calendar,
        pool_fence_other_rooms=pool_fence_other_rooms,
        pool_cleaning=pool_cleaning,
        pool_cleaning_grid=pool_cleaning_grid,
        pool_cleaning_calendar=pool_cleaning_calendar,
        pool_fence_grid=pool_fence_grid,
        pool_fence_calendar=pool_fence_calendar,
        cribs=cribs,
        cribs_grid=cribs_grid,
        cribs_calendar=cribs_calendar,
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
    # 2026-07-31 — persist the extraction cache (replaces the old comment_hash
    # stamping, which updated the supa-client project where the ingested rows
    # never lived, so it always stamped 0). One JSON blob in app_settings:
    # every entry in extractions_by_rnid with its current comment hash. Next
    # run reuses these for unchanged comments instead of calling the LLM.
    if extracted_comment_hashes and resp and resp.get("ok"):
        if True:
            try:
                import json as _json_cw
                from supa import client as _supa_client
                _sb = _supa_client()
                _cache_out = {
                    str(_rnid): {"h": _h, "e": extractions_by_rnid[_rnid]}
                    for _rnid, _h in extracted_comment_hashes.items()
                    if _rnid in extractions_by_rnid
                }
                _sb.table("app_settings").upsert(
                    {"key": "extraction_cache_v1",
                     "value": _json_cw.dumps(_cache_out, default=str)},
                    on_conflict="key",
                ).execute()
                stamped = len(_cache_out)
                print(
                    f"       Phase 48: extraction cache written "
                    f"({stamped} entries)"
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
