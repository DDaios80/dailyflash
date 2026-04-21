"""Seed the admin-entered data (daily_briefing + pool_heating) for 2026-04-20
from the original one-page Daily Flash sample PDF. One-off — makes the Lovable
preview look complete.

Run:
    python src/seed_20_04.py
Then:
    python src/daily.py samples/Daily\\ Flash\\ 20.04.2026.xlsx --date 2026-04-20
to rebuild the flash_reports payload with this data included.
"""
from __future__ import annotations

from datetime import date
from supa import client


REPORT_DATE = date(2026, 4, 20)

DAILY_BRIEFING = {
    "report_date": REPORT_DATE.isoformat(),
    "mod_name": "Mr. Kyrillos Michailides",
    "mod_phone": "+30 6951 652845 / 3845",
    "hotel_events": "10:00  Inspection by Ms. Analena Trampler",
    "site_inspections": "DER TOUR — Christianna Skouloudaki +9",
    "group_events": None,
    "show_rooms": "DLX 323 · CJSTE 103 · CSTEP 108 · CPRESP 112 · V1 205 · V2 201 · MANSION",
    "notes": None,
}

# From the sample PDF "Pool Heating & Fence" panel.
POOL_HEATING = [
    {"report_date": REPORT_DATE.isoformat(), "room": "112", "service": "HP",    "end_date": "2026-04-24", "source": "manual"},
    {"report_date": REPORT_DATE.isoformat(), "room": "118", "service": "HP",    "end_date": "2026-04-26", "source": "manual"},
    {"report_date": REPORT_DATE.isoformat(), "room": "148", "service": "HP",    "end_date": "2026-04-23", "source": "manual"},
    {"report_date": REPORT_DATE.isoformat(), "room": "201", "service": "HP",    "end_date": "2026-04-27", "source": "manual"},
    {"report_date": REPORT_DATE.isoformat(), "room": "202", "service": "HP",    "end_date": "2026-04-25", "source": "manual"},
    {"report_date": REPORT_DATE.isoformat(), "room": "205", "service": "HP",    "end_date": "2026-04-27", "source": "manual"},
    {"report_date": REPORT_DATE.isoformat(), "room": "206", "service": "HP",    "end_date": "2026-04-22", "source": "manual"},
    {"report_date": REPORT_DATE.isoformat(), "room": "207", "service": "HP",    "end_date": "2026-04-25", "source": "manual"},
    {"report_date": REPORT_DATE.isoformat(), "room": "210", "service": "HP/PF", "end_date": "2026-05-04", "source": "manual"},
    {"report_date": REPORT_DATE.isoformat(), "room": "346", "service": "PF",    "end_date": "2026-05-01", "source": "manual"},
    {"report_date": REPORT_DATE.isoformat(), "room": "347", "service": "PF",    "end_date": "2026-04-26", "source": "manual"},
]


def main() -> None:
    sb = client()

    sb.table("daily_briefing").upsert(DAILY_BRIEFING, on_conflict="report_date").execute()
    print(f"daily_briefing upserted for {REPORT_DATE}")

    # Replace any existing rows for this date to avoid duplicates
    sb.table("pool_heating").delete().eq("report_date", REPORT_DATE.isoformat()).execute()
    sb.table("pool_heating").insert(POOL_HEATING).execute()
    print(f"pool_heating: {len(POOL_HEATING)} rows for {REPORT_DATE}")


if __name__ == "__main__":
    main()
