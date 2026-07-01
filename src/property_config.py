"""Property-level configuration for the Daily Flash pipeline.

The pipeline supports multiple Daios Group properties (Daios Cove Crete,
Daios Luxury Living Thessaloniki, future ones). Each property has its
own Supabase project + Lovable workspace + Railway service, but shares
this Python codebase.

Per-property differences live in `_CONFIGS`. The active property is
selected at runtime via the `PROPERTY_CODE` env var, which defaults to
`COVE` for backward compatibility with the existing single-property
deployment.

Adding a new property:
1. Add a new entry to `_CONFIGS` with the property's specifics
2. Set `PROPERTY_CODE=<NEW>` on that property's Railway service
3. Set the property-specific MSGRAPH_ONEDRIVE_FOLDER, INGEST_FLASH_REPORT_URL,
   and SUPABASE_URL on Railway as usual

Code that needs property-specific values should call `get()` (not import
constants directly) so a future hot-reload mechanism stays cheap.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field


@dataclass(frozen=True)
class PropertyConfig:
    """Static config for a single Daios Group property."""
    code: str                              # short code, e.g. "COVE"
    name: str                              # human display name
    short_name: str                        # for tight contexts ("Daios Cove")
    domain: str                            # primary URL for staff dashboard
    location: str                          # "Crete", "Thessaloniki", etc.
    description: str                       # one-line for system prompts
    total_sellable_rooms: int              # for occupancy %
    onedrive_folder: str                   # default OneDrive base; overridable via env
    # Channel-mix knobs — different properties get bookings through
    # different operator pools, so the blacklists may differ.
    notable_agent_keywords: tuple[str, ...] = ()
    tour_operator_keywords: tuple[str, ...] = ()
    booking_com_travel_agents: tuple[str, ...] = ("BOOKING", "BOOK")
    # Guests to keep OFF every arrival surface of the flash (the arrival lists
    # and the A-lister guest research), e.g. privacy/VIP requests. Each entry is
    # a group of name tokens that must ALL appear in the guest's first+surname
    # (accent-/case-/order-insensitive) to suppress. Occupancy totals and
    # in-house lists are unaffected. Reversible: remove the entry.
    suppressed_arrival_names: tuple[tuple[str, ...], ...] = ()


# Default channel keyword pools. Properties can override per-config; if
# they don't, these apply.
_DEFAULT_NOTABLE = ("NYHAVN", "STRONG TRAVEL", "VIRTUOSO")
_DEFAULT_TOUR_OP = (
    "TUI", "DERTOUR", "DER TOUR", "JET2", "AIRTOURS",
    "ODEON", "WEBHOTELIER", "BOOKING", "BOOKING.COM",
)


_CONFIGS: dict[str, PropertyConfig] = {
    "COVE": PropertyConfig(
        code="COVE",
        name="Daios Cove Luxury Resort & Villas",
        short_name="Daios Cove",
        domain="flashreport.daioscove.com",
        location="Crete",
        description="Daios Cove, a 5-star resort on Crete",
        total_sellable_rooms=276,
        onedrive_folder="Daios Cove Crete/DailyFlash",
        notable_agent_keywords=_DEFAULT_NOTABLE,
        tour_operator_keywords=_DEFAULT_TOUR_OP,
        suppressed_arrival_names=(("theopisti", "pourliotopoulou"),),
    ),

    "DLL": PropertyConfig(
        code="DLL",
        name="Daios Luxury Living Thessaloniki",
        short_name="Daios Luxury Living",
        domain="daioshotels.com",            # confirm exact subdomain with admin
        location="Thessaloniki",
        description="Daios Luxury Living, a boutique hotel in Thessaloniki",
        total_sellable_rooms=49,
        onedrive_folder="Daios Luxury Living Thessaloniki",
        # Day-1 assumption: same channel-mix knobs as Cove. Can diverge later
        # once we have real DLL booking data and see different operator pools.
        notable_agent_keywords=_DEFAULT_NOTABLE,
        tour_operator_keywords=_DEFAULT_TOUR_OP,
    ),
}


def get() -> PropertyConfig:
    """Return the active property config, picked from PROPERTY_CODE env.
    Defaults to COVE if unset (preserves existing behaviour). Raises a
    clear error if the env names a property we don't know."""
    code = (os.environ.get("PROPERTY_CODE") or "COVE").strip().upper()
    if code not in _CONFIGS:
        known = ", ".join(sorted(_CONFIGS.keys()))
        raise RuntimeError(
            f"PROPERTY_CODE={code!r} is not a known property. Known codes: {known}. "
            f"Add a new entry to src/property_config.py::_CONFIGS to onboard it."
        )
    return _CONFIGS[code]


def get_by_code(code: str) -> PropertyConfig:
    """Look up a config by code (for tests / multi-property tooling)."""
    return _CONFIGS[code.upper()]


def all_codes() -> list[str]:
    return sorted(_CONFIGS.keys())
