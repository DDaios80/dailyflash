"""Phase 60 — Registry of all rooms that have private heatable pools.

Source of truth: ``Pool Heating_Suites_Villas.xlsx`` (rooms division), 5/9/2026.
47 rooms across 7 categories. This is the master list shown in the dashboard
pool-heating grid (every room as a button, regardless of occupancy).

NOT in this list (intentionally):
- DLXP (Deluxe Sea View Room w/ Private Pool) — has private pool but never heatable
- DJSTEP (Deluxe Junior Suite w/ Private Pool) — has private pool but never heatable
- Collection — heated by package default, no daily action required, not surfaced

Update procedure: if rooms division adds new heatable rooms, edit the xlsx in
the rooms division shared folder, then update HEATABLE_ROOMS below to match.
Keep the count line in sync.
"""
from __future__ import annotations


# Each entry: type_code, room number (str), short description.
# Categories ordered as in the source xlsx.
HEATABLE_ROOMS: list[dict] = [
    # JSTEP — Premium Junior Suite 42sqm with Private Pool (5 rooms)
    {"room": "329", "type_code": "JSTEP", "description": "Premium Junior Suite 42sqm with Private Pool"},
    {"room": "525", "type_code": "JSTEP", "description": "Premium Junior Suite 42sqm with Private Pool"},
    {"room": "538", "type_code": "JSTEP", "description": "Premium Junior Suite 42sqm with Private Pool"},
    {"room": "605", "type_code": "JSTEP", "description": "Premium Junior Suite 42sqm with Private Pool"},
    {"room": "654", "type_code": "JSTEP", "description": "Premium Junior Suite 42sqm with Private Pool"},

    # STEP — One Bedroom Suite Sea View with Private Pool 65sqm (3 rooms)
    {"room": "356", "type_code": "STEP", "description": "One Bedroom Suite Sea View with Private Pool 65sqm"},
    {"room": "521", "type_code": "STEP", "description": "One Bedroom Suite Sea View with Private Pool 65sqm"},
    {"room": "653", "type_code": "STEP", "description": "One Bedroom Suite Sea View with Private Pool 65sqm"},

    # V1 — One Bedroom Waterfront Villa Sea View with Private Pool 95sqm (11 rooms)
    {"room": "203", "type_code": "V1", "description": "One Bedroom Waterfront Villa Sea View with Private Pool 95sqm"},
    {"room": "204", "type_code": "V1", "description": "One Bedroom Waterfront Villa Sea View with Private Pool 95sqm"},
    {"room": "205", "type_code": "V1", "description": "One Bedroom Waterfront Villa Sea View with Private Pool 95sqm"},
    {"room": "206", "type_code": "V1", "description": "One Bedroom Waterfront Villa Sea View with Private Pool 95sqm"},
    {"room": "207", "type_code": "V1", "description": "One Bedroom Waterfront Villa Sea View with Private Pool 95sqm"},
    {"room": "208", "type_code": "V1", "description": "One Bedroom Waterfront Villa Sea View with Private Pool 95sqm"},
    {"room": "209", "type_code": "V1", "description": "One Bedroom Waterfront Villa Sea View with Private Pool 95sqm"},
    {"room": "210", "type_code": "V1", "description": "One Bedroom Waterfront Villa Sea View with Private Pool 95sqm"},
    {"room": "211", "type_code": "V1", "description": "One Bedroom Waterfront Villa Sea View with Private Pool 95sqm"},
    {"room": "212", "type_code": "V1", "description": "One Bedroom Waterfront Villa Sea View with Private Pool 95sqm"},
    {"room": "214", "type_code": "V1", "description": "One Bedroom Waterfront Villa Sea View with Private Pool 95sqm"},

    # V2 — Two Bedroom Villa Sea View with Private Pool 115sqm (14 rooms)
    {"room": "201", "type_code": "V2", "description": "Two Bedroom Villa Sea View with Private Pool 115sqm"},
    {"room": "202", "type_code": "V2", "description": "Two Bedroom Villa Sea View with Private Pool 115sqm"},
    {"room": "216", "type_code": "V2", "description": "Two Bedroom Villa Sea View with Private Pool 115sqm"},
    {"room": "218", "type_code": "V2", "description": "Two Bedroom Villa Sea View with Private Pool 115sqm"},
    {"room": "221", "type_code": "V2", "description": "Two Bedroom Villa Sea View with Private Pool 115sqm"},
    {"room": "223", "type_code": "V2", "description": "Two Bedroom Villa Sea View with Private Pool 115sqm"},
    {"room": "401", "type_code": "V2", "description": "Two Bedroom Villa Sea View with Private Pool 115sqm"},
    {"room": "402", "type_code": "V2", "description": "Two Bedroom Villa Sea View with Private Pool 115sqm"},
    {"room": "403", "type_code": "V2", "description": "Two Bedroom Villa Sea View with Private Pool 115sqm"},
    {"room": "541", "type_code": "V2", "description": "Two Bedroom Villa Sea View with Private Pool 115sqm"},
    {"room": "542", "type_code": "V2", "description": "Two Bedroom Villa Sea View with Private Pool 115sqm"},
    {"room": "545", "type_code": "V2", "description": "Two Bedroom Villa Sea View with Private Pool 115sqm"},
    {"room": "546", "type_code": "V2", "description": "Two Bedroom Villa Sea View with Private Pool 115sqm"},
    {"room": "547", "type_code": "V2", "description": "Two Bedroom Villa Sea View with Private Pool 115sqm"},

    # VW — Two Bedroom Sea View Wellness Villa with Private Pool 125sqm (11 rooms)
    {"room": "215", "type_code": "VW", "description": "Two Bedroom Sea View Wellness Villa with Private Pool 125sqm"},
    {"room": "217", "type_code": "VW", "description": "Two Bedroom Sea View Wellness Villa with Private Pool 125sqm"},
    {"room": "219", "type_code": "VW", "description": "Two Bedroom Sea View Wellness Villa with Private Pool 125sqm"},
    {"room": "220", "type_code": "VW", "description": "Two Bedroom Sea View Wellness Villa with Private Pool 125sqm"},
    {"room": "222", "type_code": "VW", "description": "Two Bedroom Sea View Wellness Villa with Private Pool 125sqm"},
    {"room": "224", "type_code": "VW", "description": "Two Bedroom Sea View Wellness Villa with Private Pool 125sqm"},
    {"room": "225", "type_code": "VW", "description": "Two Bedroom Sea View Wellness Villa with Private Pool 125sqm"},
    {"room": "543", "type_code": "VW", "description": "Two Bedroom Sea View Wellness Villa with Private Pool 125sqm"},
    {"room": "544", "type_code": "VW", "description": "Two Bedroom Sea View Wellness Villa with Private Pool 125sqm"},
    {"room": "548", "type_code": "VW", "description": "Two Bedroom Sea View Wellness Villa with Private Pool 125sqm"},
    {"room": "671", "type_code": "VW", "description": "Two Bedroom Sea View Wellness Villa with Private Pool 125sqm"},

    # V3 — Three Bedroom Villa Sea View with Private Pool 130sqm (2 rooms)
    {"room": "404", "type_code": "V3", "description": "Three Bedroom Villa Sea View with Private Pool 130sqm"},
    {"room": "761", "type_code": "V3", "description": "Three Bedroom Villa Sea View with Private Pool 130sqm"},

    # MANSION — The Mansion 550sqm (1 room)
    {"room": "600", "type_code": "MANSION", "description": "The Mansion 550sqm"},
]

assert len(HEATABLE_ROOMS) == 47, f"expected 47 heatable rooms, got {len(HEATABLE_ROOMS)}"

HEATABLE_ROOM_NUMBERS: set[str] = {r["room"] for r in HEATABLE_ROOMS}
HEATABLE_TYPE_CODES: set[str] = {r["type_code"] for r in HEATABLE_ROOMS}


def is_booked_category_heatable(booked_category: str | None) -> bool:
    """Phase 60 — does the booked (paid) category come with heated-pool service?

    True only if booked_category is one of the heatable type codes. Upgrades
    from non-heatable categories (e.g., DLX → V1) return False because the
    guest didn't pay for heated-pool service.

    JSTEP/STEP return True here, but the caller must additionally check the
    comment_extractions pool_heating flag — JSTEP/STEP heating is on-request,
    not automatic.
    """
    if not booked_category:
        return False
    return booked_category.upper() in HEATABLE_TYPE_CODES


def is_villa_category(category: str | None) -> bool:
    """True if category is V1/V2/VW/V3 or MANSION (always-heated when booked)."""
    if not category:
        return False
    cat = category.upper()
    return cat.startswith("V") or cat == "MANSION"


def effective_pool_heating_v60(
    booked_category: str | None,
    actual_category: str | None,
    ce_pool_heating: bool,
) -> bool:
    """Phase 60 rule — replaces phase49 ``_effective_pool_heating``.

    Heating service follows what was BOOKED (the paid product), not what
    the guest physically got assigned. So upgrades from non-heatable
    categories don't get heating service.

    Falls back to actual_category if booked is null (legacy data).
    """
    booked = (booked_category or actual_category or "").strip().upper()
    if not booked:
        return False
    if not is_booked_category_heatable(booked):
        return False
    if is_villa_category(booked):
        return True  # paid for villa-tier — heated by default
    if booked in ("JSTEP", "STEP"):
        return bool(ce_pool_heating)  # on request via housekeeping comment
    return False
