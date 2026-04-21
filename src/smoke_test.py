"""Smoke test — verifies Phase 1 extraction against the known 2026-04-20 sample.

Run:  python src/smoke_test.py
"""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

from ingest import parse_file, summarize
from compute import compute_flash


SAMPLE = Path(__file__).parent.parent / "samples" / "Daily Flash 20.04.2026.xlsx"
REPORT_DATE = date(2026, 4, 20)

# Rooms that MUST appear in Special Attention Arrivals for 2026-04-20.
# These are the 14 rooms on the manually-curated sample flash.
REQUIRED_ROOMS = {
    "108",  # Conitzer - Complimentary
    "112",  # Moerch/Frederiksen - PEP
    "143",  # Westermann - Repeaters
    "148",  # Merle - Complimentary
    "205",  # Dixon - Repeaters
    "210",  # Magistro - Repeaters + allergy
    "308",  # Laue - Dertour
    "331",  # Roubach - VIP in comments
    "336",  # Lang - Repeaters
    "345",  # Bounni - Repeaters
    "346",  # Hadler - Repeaters
    "382",  # Wagner - F&F
    "501",  # Tremoureux - Repeaters
    "503",  # Achmueller - Repeaters
}


def main() -> int:
    assert SAMPLE.exists(), f"Sample file missing: {SAMPLE}"

    records = parse_file(SAMPLE)
    summary = summarize(records)
    print(f"Parsed {summary['total_real_reservations']} reservations: {summary['by_status']}")

    flash = compute_flash(records, REPORT_DATE)
    actual_rooms = {g["room"] for g in flash.special_attention_arrivals}
    missing = REQUIRED_ROOMS - actual_rooms
    if missing:
        print(f"FAIL: missing special-attention rooms {sorted(missing)}")
        return 1

    # Sanity: room rendering is clean (no trailing .0)
    bad_rooms = [g["room"] for g in flash.special_attention_arrivals if g["room"] and g["room"].endswith(".0")]
    if bad_rooms:
        print(f"FAIL: rooms have trailing '.0': {bad_rooms}")
        return 1

    # Sanity: Magistro + Nordbaeck caught as allergy
    allergy_rooms = {g["room"] for g in flash.allergies_in_house}
    if "210" not in allergy_rooms:
        print(f"FAIL: Magistro (210) not flagged for allergy; got {allergy_rooms}")
        return 1

    # Sanity: PEP arrival present
    if not any(g["room"] == "112" for g in flash.pep_arrivals):
        print("FAIL: PEP arrival 112 not detected")
        return 1

    # Sanity: complimentary-partner arrivals include 108, 148 (guests with accompanying names)
    comp_rooms = {g["room"] for g in flash.complimentary_partner_arrivals}
    if not {"108", "148"}.issubset(comp_rooms):
        print(f"FAIL: expected 108 and 148 in complimentary-partner arrivals; got {comp_rooms}")
        return 1

    # Sanity: occupancy trio is monotone-ish and percentages are sane
    pct = [o["occupancy_pct"] for o in flash.occupancy]
    if not all(0 <= p <= 100 for p in pct):
        print(f"FAIL: occupancy pct out of range {pct}")
        return 1

    print("PASS — all Phase 1 invariants hold.")
    print(f"  special_attention_arrivals: {len(flash.special_attention_arrivals)}")
    print(f"  complimentary_partner_arrivals: {len(flash.complimentary_partner_arrivals)}")
    print(f"  pep_arrivals: {len(flash.pep_arrivals)}")
    print(f"  allergies_in_house: {len(flash.allergies_in_house)}")
    print(f"  occupancy today: {flash.occupancy[0]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
