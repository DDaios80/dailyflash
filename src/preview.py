"""CLI preview of the Daily Flash for a given .xlsx + date.

Usage:
    python src/preview.py samples/Daily\\ Flash\\ 20.04.2026.xlsx --date 2026-04-20
    python src/preview.py samples/Daily\\ Flash\\ 20.04.2026.xlsx --date 2026-04-20 --extract
"""
from __future__ import annotations

import argparse
import asyncio
import json
from dataclasses import asdict
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

from ingest import parse_file, summarize
from compute import compute_flash


def _parse_date(s: str) -> date:
    return datetime.strptime(s, "%Y-%m-%d").date()


def _section(title: str) -> None:
    print(f"\n\033[1;33m── {title} ──\033[0m")


def _guest_line(g: dict, extraction: Optional[dict] = None) -> str:
    room = g.get("room") or "-"
    name = g.get("name") or ""
    reason = g.get("reason") or ""
    flag_alg = " ⚠️ ALLERGY" if (g.get("allergy_flag") or (extraction and extraction.get("allergies_present"))) else ""
    flag_hon = " 💍 HON" if (g.get("honeymoon") or (extraction and extraction.get("honeymoon"))) else ""
    acc = f"  (+{g['accompanying']})" if g.get("accompanying") else ""
    badge_extras = []
    if extraction:
        if extraction.get("pool_fence"):
            badge_extras.append("🧒 POOL FENCE")
        if extraction.get("free_upgrade"):
            badge_extras.append("⬆️ UPGRADE")
        if extraction.get("late_checkout"):
            badge_extras.append("🕑 LCO")
        if extraction.get("vip_flag"):
            badge_extras.append("⭐ VIP-note")
    extras = (" " + " ".join(badge_extras)) if badge_extras else ""
    return f"  {room:>5}  {name:<40}  {reason}{flag_alg}{flag_hon}{extras}{acc}"


def _guest_detail_block(extraction: Optional[dict]) -> str:
    """Extra indented detail lines surfacing extraction highlights per guest."""
    if not extraction:
        return ""
    lines = []
    if extraction.get("allergies_present") and extraction.get("allergies_text"):
        lines.append(f"         ⚠️  {extraction['allergies_text']}")
    if extraction.get("amenities"):
        amenities = ", ".join(extraction["amenities"])
        lines.append(f"         🎁  {amenities}")
    if extraction.get("payment_notes"):
        lines.append(f"         💳  {extraction['payment_notes']}")
    return "\n".join(lines) + "\n" if lines else ""


def render(flash, extractions_by_resv_id: Optional[dict] = None) -> None:
    extractions_by_resv_id = extractions_by_resv_id or {}
    print(f"\n\033[1;36mDAIOS COVE — DAILY FLASH {flash.report_date.isoformat()}\033[0m")

    _section("OCCUPANCY")
    print(f"  {'Day':<12} {'Occ.Rooms':>10} {'Guests INH':>12} {'Arrivals':>10} {'Departures':>12} {'Occ%':>8}")
    for occ in flash.occupancy:
        print(
            f"  {occ['label']:<12} "
            f"{occ['occ_rooms']:>10} {occ['guests_inh']:>12} "
            f"{occ['arrivals']:>10} {occ['departures']:>12} "
            f"{occ['occupancy_pct']:>7.2f}%"
        )

    def _render_guest_list(title: str, guests: list) -> None:
        _section(f"{title} ({len(guests)})")
        for g in guests:
            ext = extractions_by_resv_id.get(g.get("resv_name_id")) if g.get("resv_name_id") else None
            print(_guest_line(g, ext))
            detail = _guest_detail_block(ext)
            if detail:
                print(detail, end="")

    _render_guest_list("SPECIAL ATTENTION ARRIVALS", flash.special_attention_arrivals)
    _render_guest_list("SPECIAL ATTENTION DEPARTURES", flash.special_attention_departures)
    _render_guest_list("COMPLIMENTARY — PARTNER ARRIVALS", flash.complimentary_partner_arrivals)
    _render_guest_list("PEP ARRIVALS", flash.pep_arrivals)
    _render_guest_list("BIRTHDAYS / ANNIVERSARIES IN HOUSE (from BIRTH_DATE)", flash.birthdays_today)

    # Pull additional birthday/anniversary signals from extractions that the
    # structured BIRTH_DATE field misses.
    if extractions_by_resv_id:
        extra_hon = [
            rid for rid, ext in extractions_by_resv_id.items()
            if ext.get("honeymoon")
        ]
        if extra_hon:
            _section(f"HONEYMOON / ANNIVERSARY (from COMMENTS) ({len(extra_hon)})")
            for rid in extra_hon:
                ext = extractions_by_resv_id[rid]
                print(f"  resv={rid}  {ext.get('ops_summary')}")

        pf = [(rid, ext) for rid, ext in extractions_by_resv_id.items() if ext.get("pool_fence")]
        if pf:
            _section(f"POOL FENCE / CHILD SAFETY ({len(pf)})")
            for rid, ext in pf:
                print(f"  resv={rid}  {ext.get('ops_summary')}")

        up = [(rid, ext) for rid, ext in extractions_by_resv_id.items() if ext.get("free_upgrade")]
        if up:
            _section(f"FREE UPGRADES TO COMMUNICATE ({len(up)})")
            for rid, ext in up:
                print(f"  resv={rid}  {ext.get('ops_summary')}")

    # Allergy section: union of keyword prefilter + LLM extraction
    allergy_rids = {g.get("resv_name_id") for g in flash.allergies_in_house if g.get("resv_name_id")}
    if extractions_by_resv_id:
        allergy_rids |= {rid for rid, ext in extractions_by_resv_id.items() if ext.get("allergies_present")}
    _section(f"ALLERGIES / DIETARY ({len(allergy_rids)})")
    # Prefer extraction data for richer rendering; fall back to keyword prefilter rows.
    printed = set()
    for g in flash.allergies_in_house:
        rid = g.get("resv_name_id")
        ext = extractions_by_resv_id.get(rid) if rid else None
        print(_guest_line(g, ext))
        if ext and ext.get("allergies_text"):
            print(f"         ⚠️  {ext['allergies_text']}")
        printed.add(rid)
    for rid in allergy_rids - printed:
        ext = extractions_by_resv_id[rid]
        text = ext.get("allergies_text") or ext.get("ops_summary") or ""
        print(f"  resv={rid}  ⚠️  {text}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("xlsx", help="Path to the daily Opera .xlsx export")
    ap.add_argument("--date", required=True, type=_parse_date, help="Report date (YYYY-MM-DD)")
    ap.add_argument("--extract", action="store_true",
                    help="Also run LLM extraction over COMMENTS (requires ANTHROPIC_API_KEY)")
    ap.add_argument("--window-days", type=int, default=7,
                    help="Days ahead to include when running --extract")
    ap.add_argument("--json", action="store_true", help="Also emit JSON")
    args = ap.parse_args()

    records = parse_file(args.xlsx)
    summary = summarize(records)
    print(f"Parsed {summary['total_real_reservations']} real reservations from {Path(args.xlsx).name}")
    print(f"  by status: {summary['by_status']}")

    extractions_by_resv_id: dict = {}
    if args.extract:
        # Imported lazily so Phase 1-only users don't need anthropic installed.
        from extract import extract_batch, _should_extract  # type: ignore
        end = args.date + timedelta(days=args.window_days)
        in_scope = [r for r in records if _should_extract(r, args.date, end)]
        print(f"Running LLM extraction on {len(in_scope)} reservations "
              f"(arrivals {args.date}..{end}) — this will take ~60s...")
        extractions, errors, es = asyncio.run(extract_batch(in_scope))
        print(f"  extraction: {es['succeeded']}/{es['total']} ok, {es['failed']} failed")
        extractions_by_resv_id = {rid: ext.model_dump() for rid, ext in extractions.items()}

    flash = compute_flash(records, args.date)
    render(flash, extractions_by_resv_id)

    if args.json:
        print("\n\033[1;33m── JSON ──\033[0m")
        out = asdict(flash)
        out["extractions"] = extractions_by_resv_id
        print(json.dumps(out, default=str, indent=2))


if __name__ == "__main__":
    main()
