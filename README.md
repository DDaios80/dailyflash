# Daios Cove Daily Flash

Agentic daily briefing for Daios Cove management. Ingests the daily Opera PMS reservation export (the "elements" `.xlsx` file) and produces a one-page flash: occupancy, special-attention arrivals, complimentary / PEP bookings, birthdays, allergies, weather, pool heating, MOD.

## Status

**Phase 1** — Data foundation (in progress). Deterministic pipeline from Excel → structured JSON matching the flash layout. No LLM, no A-lister, no UI yet.

## Layout

```
daily-flash/
  db/schema.sql        # Supabase schema (Phase 1 tables + Phase 2/3 placeholders)
  src/ingest.py        # Parse .xlsx → normalized reservations
  src/compute.py       # Deterministic business logic (occupancy, special attention, etc.)
  src/preview.py       # CLI: given a .xlsx + report date, print the flash
  samples/             # Real daily exports for testing
  requirements.txt
  .env.example
```

## Quick start

```bash
cd daily-flash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python src/preview.py samples/Daily\ Flash\ 20.04.2026.xlsx --date 2026-04-20
```

## Later phases

- Phase 2: Claude LLM pass over `COMMENTS` for allergies, pool fence, repeater amenities.
- Phase 3: Firecrawl A-lister research across guest + accompanying names.
- Phase 4: Next.js dashboard + downloadable PDF + admin screens (MOD/events/pool/weather).
- Phase 5: Opera Cloud API cutover (Nov 2026), replacing `.xlsx` upload.
