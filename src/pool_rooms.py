"""Phase 61 — Registry of ALL rooms with private pools (for cleaning ops).

Source of truth: ``All Rooms with Pool.xlsx`` (rooms division), 5/9/2026.
137 unique physical rooms across 12 distinct pool-bearing categories. The
maintenance coordinator uses this to plan poolboy cleaning schedules: count
of occupied pool rooms per day = number of pools to clean.

How this differs from ``heatable_rooms.py``:

- ``heatable_rooms.py`` lists 47 rooms eligible for heating (V*, MANSION,
  JSTEP, STEP). Used for the heating grid.
- ``pool_rooms.py`` (this module) lists ALL 137 private-pool rooms.
  Includes DLXP (49) and Collection categories (CJSTEP, CPJSTP, CPRESP,
  CSTEP — 41 rooms total). Used for the cleaning forecast.

Important rate-plan alias:
- DLXP = Half Board, DJSTEP = Residents' Club (RC). SAME 49 physical rooms,
  different rate plans. The xlsx lists DJSTEP with NUMBER_ROOMS=0 and no
  room list to make this explicit. Bookings under either label occupy the
  same pool.

Other categories with private pools that DO NOT appear in this registry
because they're combinations of existing rooms (not separate physical
inventory):
- C2BSP — Collection Two Bedroom Suite, made by combining a CJSTEP +
  CPRESP into a single booking (e.g. rooms "110+112" → one C2BSP). The
  component rooms remain in the registry under their primary categories.
  Operationally one of the two component pools is the "active" pool per
  C2BSP booking; the housekeeping team knows which.
"""
from __future__ import annotations


# Each entry: room number, primary type code, short description, heatable flag.
# Categories ordered roughly by floor / wing for readability.
POOL_ROOMS: list[dict] = [
    # ── DLXP / DJSTEP (49) — Deluxe Sea View 42sqm with Individual Pool.
    # NEVER heated by service. DLXP=HB, DJSTEP=RC (same physical inventory).
    *[
        {"room": r, "type_code": "DLXP", "description": "Deluxe Room Sea View 42sqm with Individual Pool", "heatable": False}
        for r in [
            "325", "326", "327", "331", "332", "340", "341", "342", "343",
            "344", "346", "347", "348", "349", "350", "351", "352", "353",
            "365", "366", "367", "368", "369", "527", "528", "529", "534",
            "535", "536", "601", "615", "616", "617", "618", "619", "634",
            "635", "636", "640", "641", "642", "656", "657", "658", "659",
            "663", "665", "667", "668",
        ]
    ],

    # ── JSTEP (5) — Premium Junior Suite 42sqm with Private Pool. On request.
    *[
        {"room": r, "type_code": "JSTEP", "description": "Premium Junior Suite 42sqm with Private Pool", "heatable": True}
        for r in ["329", "525", "538", "605", "654"]
    ],

    # ── STEP (3) — One Bedroom Suite Sea View with Private Pool 65sqm. On request.
    *[
        {"room": r, "type_code": "STEP", "description": "One Bedroom Suite Sea View with Private Pool 65sqm", "heatable": True}
        for r in ["356", "521", "653"]
    ],

    # ── V1 (11) — One Bedroom Waterfront Villa Sea View with Private Pool 95sqm.
    *[
        {"room": r, "type_code": "V1", "description": "One Bedroom Waterfront Villa Sea View with Private Pool 95sqm", "heatable": True}
        for r in ["203", "204", "205", "206", "207", "208", "209", "210", "211", "212", "214"]
    ],

    # ── V2 (14) — Two Bedroom Villa Sea View with Private Pool 115sqm.
    *[
        {"room": r, "type_code": "V2", "description": "Two Bedroom Villa Sea View with Private Pool 115sqm", "heatable": True}
        for r in ["201", "202", "216", "218", "221", "223", "401", "402", "403", "541", "542", "545", "546", "547"]
    ],

    # ── VW (11) — Two Bedroom Sea View Wellness Villa with Private Pool 125sqm.
    *[
        {"room": r, "type_code": "VW", "description": "Two Bedroom Sea View Wellness Villa with Private Pool 125sqm", "heatable": True}
        for r in ["215", "217", "219", "220", "222", "224", "225", "543", "544", "548", "671"]
    ],

    # ── V3 (2) — Three Bedroom Villa Sea View with Private Pool 130sqm.
    *[
        {"room": r, "type_code": "V3", "description": "Three Bedroom Villa Sea View with Private Pool 130sqm", "heatable": True}
        for r in ["404", "761"]
    ],

    # ── MANSION (1) — The Mansion 550sqm.
    {"room": "600", "type_code": "MANSION", "description": "The Mansion 550sqm", "heatable": True},

    # ── CJSTEP (13) — The Collection Junior Suite Sea View 42sqm with private pool.
    *[
        {"room": r, "type_code": "CJSTEP", "description": "The Collection Junior Suite Sea View 42sqm with private pool", "heatable": False}
        for r in ["110", "116", "118", "147", "703", "719", "721", "726", "735", "738", "749", "802", "820"]
    ],

    # ── CPJSTP (7) — The Collection Premium Junior Suite 42sqm with private pool.
    *[
        {"room": r, "type_code": "CPJSTP", "description": "The Collection Premium Junior Suite 42sqm with private pool", "heatable": False}
        for r in ["701", "739", "751", "801", "805", "816", "822"]
    ],

    # ── CPRESP (14) — The Collection Premium One Bedroom Suite Sea View 85sqm with private pool.
    *[
        {"room": r, "type_code": "CPRESP", "description": "The Collection Premium One Bedroom Suite Sea View 85sqm with private pool", "heatable": False}
        for r in ["112", "114", "120", "148", "705", "717", "722", "724", "728", "736", "745", "747", "804", "818"]
    ],

    # ── CSTEP (7) — The Collection One Bedroom Suite Sea View 65sqm with private pool.
    *[
        {"room": r, "type_code": "CSTEP", "description": "The Collection One Bedroom Suite Sea View 65sqm with private pool", "heatable": False}
        for r in ["108", "141", "711", "731", "744", "806", "811"]
    ],
]

assert len(POOL_ROOMS) == 137, f"expected 137 pool rooms, got {len(POOL_ROOMS)}"

# Fast-lookup index.
POOL_ROOM_BY_NUMBER: dict[str, dict] = {r["room"]: r for r in POOL_ROOMS}
POOL_ROOM_NUMBERS: set[str] = set(POOL_ROOM_BY_NUMBER.keys())

# Rate-plan aliases for category breakdowns. Keys are labels that should be
# treated as occupying the same physical inventory as the value.
CATEGORY_ALIASES: dict[str, str] = {
    "DJSTEP": "DLXP",  # Residents' Club rate plan on DLXP rooms
}


def is_pool_room(room: str | None) -> bool:
    """True if the room number has a private pool."""
    return room is not None and room in POOL_ROOM_NUMBERS


def primary_category(room: str | None) -> str | None:
    """Primary type code for the room (the registered one), or None."""
    if room is None:
        return None
    entry = POOL_ROOM_BY_NUMBER.get(room)
    return entry["type_code"] if entry else None


def normalize_category(label: str | None) -> str | None:
    """Resolve a booked/actual category label through the alias map.

    DJSTEP → DLXP. Everything else passes through unchanged. None → None.
    """
    if not label:
        return None
    label = label.strip().upper()
    return CATEGORY_ALIASES.get(label, label)


# All labels that imply the booking has a private pool. Includes both the
# canonical category codes (in POOL_ROOMS) and rate-plan aliases (DJSTEP).
POOL_BEARING_LABELS: set[str] = (
    {r["type_code"] for r in POOL_ROOMS} | set(CATEGORY_ALIASES.keys())
)


def display_category_for_cleaning(
    booked_label: str | None,
    actual_label: str | None,
    room_number: str | None,
) -> str | None:
    """Pick the category label to show in the cleaning-breakdown.

    Operationally, the cleaning team services the PHYSICAL pool, so we
    prefer the actual room category. Booked is a fallback only.

    Rules:
    1. If actual is a pool category (DLXP, V1, CJSTEP, etc.), use actual.
       This is the cleanest operational view: how many DLXP pools, how
       many V1 pools.
    2. Otherwise, fall back to booked (rare — mostly empty actual).
    3. Final fallback: registry primary type code by room number.

    Note on DLXP/DJSTEP rate plans: Opera collapses both to actual="DLXP"
    in ``room_category_label`` regardless of HB vs RC. So this view shows
    "DLXP" for all 49 physical DLXP rooms regardless of rate plan. A
    separate rate-plan breakdown (using booked label) can be added later
    if the coordinator needs the HB/RC split.
    """
    booked = (booked_label or "").strip().upper()
    actual = (actual_label or "").strip().upper()
    if actual and actual in POOL_BEARING_LABELS:
        return actual
    if booked and booked in POOL_BEARING_LABELS:
        return booked
    return primary_category(room_number)
