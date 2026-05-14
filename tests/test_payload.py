"""Smoke test: latest flash_reports row has correct payload structure.

What this catches:
- A future migration accidentally drops a payload field (e.g., pool_heating_grid)
- The Python pipeline writes a malformed envelope
- A schema regression that breaks the dashboard / PDF / email rendering

What it does NOT catch:
- The full pipeline being run end-to-end (no fresh xlsx → envelope check)
- Logical errors in the data (e.g., room counts wrong)
- Issues that only manifest under specific data conditions

This is a SMOKE test, not a regression test. Designed to fail loudly when
something structural breaks; not to validate every business rule.

Reads against the live Supabase. Requires SUPABASE_URL +
SUPABASE_SERVICE_ROLE_KEY env vars (loaded from .env via src/supa.py).
"""
from __future__ import annotations

from datetime import date, timedelta, timezone, datetime

import pytest


# Expected top-level payload keys with optional sanity-check counts.
# (key_name, expected_count_or_None): if count is not None, assert len(payload[key]) == count.
# If count is None, just assert the key exists and is non-empty when applicable.
EXPECTED_PAYLOAD_KEYS = [
    # Phase 60 — pool heating
    ("pool_heating_grid", 47),
    ("pool_heating_calendar", None),  # dict, not list
    # Phase 60.1 — fence overflow
    ("pool_fence_other_rooms", None),
    # Phase 61 — cleaning
    ("pool_cleaning", None),
    # Phase 61.1 — cleaning grid
    ("pool_cleaning_grid", 137),
    ("pool_cleaning_calendar", None),
    # Phase 61.2 — fence grid
    ("pool_fence_grid", 137),
    ("pool_fence_calendar", None),
    # Phase 62 — cribs
    ("cribs", None),
    ("cribs_grid", None),  # dynamic count, varies daily
    ("cribs_calendar", None),
    # Phase 60 follow-up — legacy pool_heating soft-deprecated to empty []
    ("pool_heating", 0),
    # Other expected envelope fields
    ("special_attention_arrivals", None),
    ("birthdays_in_house", None),
    ("alister_findings", None),
    ("daily_briefing", None),
]


@pytest.fixture(scope="module")
def latest_flash_report(supabase_client):
    """Fetch the most recent flash_reports row from the last 7 days.

    Date filter is intentional: we only want to assert on recent cron runs,
    not stale rows from earlier in development. Also avoids any potential
    issues with supabase-py's order() semantics — the date filter does the
    real selection work, order+limit is just for tie-breaking.
    """
    cutoff = (date.today() - timedelta(days=7)).isoformat()
    result = (
        supabase_client.table("flash_reports")
        .select("report_date,computed_at,payload")
        .gte("report_date", cutoff)
        .order("computed_at", desc=True)
        .limit(1)
        .execute()
    )
    if not result.data:
        pytest.skip(
            f"No flash_reports rows in the last 7 days (cutoff: {cutoff}). "
            f"Cron may have stopped running, or you're running tests against "
            f"a fresh / non-production database."
        )
    return result.data[0]


def test_latest_report_recent(latest_flash_report):
    """Latest flash_report was computed within the last 48 hours.

    Catches: cron not running. The Phase 60.3 freshness check warns at the
    cron-run level; this asserts at the test level.
    """
    computed_at_str = latest_flash_report["computed_at"]
    # Parse the ISO timestamp (may have either +00:00 or Z suffix)
    computed_at = datetime.fromisoformat(computed_at_str.replace("Z", "+00:00"))
    now = datetime.now(timezone.utc)
    age_hours = (now - computed_at).total_seconds() / 3600

    assert age_hours < 48, (
        f"Latest flash_report computed_at = {computed_at_str} "
        f"({age_hours:.1f} hours ago). Cron may have stopped running. "
        f"Check Railway dashboard + heartbeat status."
    )


def test_report_date_today_or_tomorrow(latest_flash_report):
    """Latest flash_report's report_date is today or tomorrow Athens time.

    The pipeline runs at 22:00 Athens to generate tomorrow's flash, so the
    most recent row should have report_date = tomorrow's date.
    """
    report_date = date.fromisoformat(latest_flash_report["report_date"])
    today = date.today()
    # Acceptable: today, tomorrow, day-after-tomorrow (edge cases around
    # cron timing + timezone offsets).
    acceptable = {today - timedelta(days=1), today, today + timedelta(days=1)}
    assert report_date in acceptable, (
        f"Latest flash_report report_date = {report_date} but expected "
        f"one of {sorted(acceptable)}. Cron timing or date logic regression."
    )


@pytest.mark.parametrize(
    "key,expected_count",
    EXPECTED_PAYLOAD_KEYS,
    ids=[k for k, _ in EXPECTED_PAYLOAD_KEYS],
)
def test_payload_key_exists(latest_flash_report, key, expected_count):
    """Each expected payload key exists in the latest flash_report envelope.

    If a key is missing, a future migration likely dropped it. The Phase 67.1
    column-name bug class would have been caught here if it had affected the
    payload structure.
    """
    payload = latest_flash_report["payload"]
    assert key in payload, (
        f"Payload missing expected key '{key}'. "
        f"Available keys: {sorted(payload.keys())}"
    )

    if expected_count is not None:
        actual = payload[key]
        assert isinstance(actual, list), (
            f"Payload key '{key}' is {type(actual).__name__}, expected list."
        )
        assert len(actual) == expected_count, (
            f"Payload key '{key}' has {len(actual)} items, expected {expected_count}. "
            f"Either the room registry changed (legitimate, update test) or "
            f"the pipeline regressed (investigate src/daily.py)."
        )


def test_pool_heating_soft_deprecated(latest_flash_report):
    """Legacy pool_heating field is empty [] per Phase 60 follow-up soak.

    Until ~2026-05-21, this field should always be empty list. After that
    date, the field will be fully removed and this test should be deleted
    (or updated to assert key absence).
    """
    payload = latest_flash_report["payload"]
    pool_heating_legacy = payload.get("pool_heating", "MISSING")
    assert pool_heating_legacy == [], (
        f"Legacy pool_heating field expected to be empty list (soft-deprecated). "
        f"Got: {pool_heating_legacy!r}. "
        f"Either soak period ended and field was removed (update test), "
        f"or pipeline regressed (check src/daily.py line ~209)."
    )


def test_pool_heating_grid_room_count_invariant(latest_flash_report):
    """Phase 60 pool heating grid has exactly 47 heatable rooms.

    The 47-room registry (heatable_rooms.HEATABLE_ROOMS) is fixed and tied
    to physical resort inventory. If this count changes, either:
      - A room was added/removed (legitimate, update src/heatable_rooms.py)
      - The grid build logic regressed (investigate)
    """
    payload = latest_flash_report["payload"]
    grid = payload.get("pool_heating_grid", [])
    assert len(grid) == 47, (
        f"pool_heating_grid has {len(grid)} entries, expected 47. "
        f"Check src/heatable_rooms.py registry."
    )

    # Each entry should have the standard shape
    if grid:
        first = grid[0]
        required_keys = {"room", "type_code", "description", "is_heated_today"}
        actual_keys = set(first.keys())
        missing = required_keys - actual_keys
        assert not missing, (
            f"pool_heating_grid entry missing keys: {missing}. "
            f"Got keys: {sorted(actual_keys)}"
        )


def test_pool_cleaning_grid_room_count_invariant(latest_flash_report):
    """Phase 61.1 pool cleaning grid has exactly 137 private-pool rooms."""
    payload = latest_flash_report["payload"]
    grid = payload.get("pool_cleaning_grid", [])
    assert len(grid) == 137, (
        f"pool_cleaning_grid has {len(grid)} entries, expected 137. "
        f"Check src/pool_rooms.py registry."
    )


def test_pool_fence_grid_room_count_invariant(latest_flash_report):
    """Phase 61.2 pool fence grid has exactly 137 private-pool rooms."""
    payload = latest_flash_report["payload"]
    grid = payload.get("pool_fence_grid", [])
    assert len(grid) == 137, (
        f"pool_fence_grid has {len(grid)} entries, expected 137. "
        f"Check src/pool_rooms.py + daily.py Phase 61.2 logic."
    )
