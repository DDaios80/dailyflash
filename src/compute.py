"""Deterministic Daily Flash computations.

Given a list of normalized reservation records (from ingest.parse_file) and a
report_date, compute the fields that appear on the Daily Flash — without any
LLM, web search, or admin-entered data.

Out-of-scope for Phase 1 (handled later):
  • COMMENTS-based allergy/pool/amenity extraction (Phase 2)
  • A-lister enrichment (Phase 3)
  • Weather / MOD / hotel events / show rooms (admin input or external APIs)
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import date, datetime, timedelta
from typing import Any, Iterable


# ─── Helpers ────────────────────────────────────────────────────────────────

def _as_date(v: Any) -> date | None:
    if v is None:
        return None
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    return None


def _arrives_on(r: dict[str, Any], d: date) -> bool:
    a = _as_date(r.get("arrival"))
    return a == d


def _departs_on(r: dict[str, Any], d: date) -> bool:
    dep = _as_date(r.get("departure"))
    return dep == d


def _in_house_on(r: dict[str, Any], d: date) -> bool:
    """Guest is staying through night of `d` (arrival<=d, departure>d)."""
    a = _as_date(r.get("arrival"))
    dep = _as_date(r.get("departure"))
    if a is None or dep is None:
        return False
    return a <= d < dep


# ─── Special attention rules ────────────────────────────────────────────────

COMPLIMENTARY_GROUP_KEYWORDS = ("COMPLIMENTARY",)
PEP_GROUP_KEYWORDS = ("PEP",)
FF_KEYWORDS = ("F&F", "FAMILY & FRIENDS", "FAM&FRIENDS")

ALLERGY_KEYWORDS = (
    "allerg",  # covers allergy, allergies, allergic
    "peanut", "gluten", "lactose", "dairy", "seafood", "shellfish",
    "nut allerg",  # narrow match for nuts
)

SPECIAL_REQUEST_ATTENTION_CODES = {"HON", "VIP"}

# Phrases in COMMENTS that should promote the guest to special attention.
COMMENT_ATTENTION_PHRASES = (
    "vip",                 # "VIP Booking"
    "extra attention",
    "tui staff",
    "dertour staff",
    "staff",               # broad, but comments rarely mention "staff" otherwise
    "demanding",
    "prize winner",
)

# Travel-agent / group names that warrant special attention on their own.
# Kept tight to genuine luxury channels. Removed 23 Apr 2026:
#   WEBHOTELIER — direct booking engine, not a TA
#   AIRTOURS, DERTOUR, DER TOUR, JET2 — bulk contract tour operators, not VIP
NOTABLE_AGENT_KEYWORDS = (
    "NYHAVN",
    "ODEON",
    "STRONG TRAVEL",
    "VIRTUOSO",
)


def is_complimentary(r: dict[str, Any]) -> bool:
    if (r.get("complimentary_yn") or "").upper() == "Y":
        return True
    if (r.get("market_desc") or "").strip().lower() == "complimentary":
        return True
    g = (r.get("group_name") or "").upper()
    return any(k in g for k in COMPLIMENTARY_GROUP_KEYWORDS)


def is_pep(r: dict[str, Any]) -> bool:
    if (r.get("market_desc") or "").strip().upper() == "PEP":
        return True
    g = (r.get("group_name") or "").upper()
    return any(k in g for k in PEP_GROUP_KEYWORDS)


def has_allergy_keywords(r: dict[str, Any]) -> bool:
    """Quick keyword prefilter. Real extraction happens in Phase 2."""
    c = (r.get("comments") or "").lower()
    if not c:
        return False
    return any(k in c for k in ALLERGY_KEYWORDS)


def _comment_attention_match(r: dict[str, Any]) -> str | None:
    """Return the matched COMMENTS phrase that implies special attention, or None."""
    c = (r.get("comments") or "").lower()
    if not c:
        return None
    for phrase in COMMENT_ATTENTION_PHRASES:
        if phrase in c:
            return phrase
    return None


def _special_request_codes(r: dict[str, Any]) -> set[str]:
    raw = (r.get("special_requests") or "").upper()
    return {code.strip() for code in raw.replace(",", " ").split() if code.strip()}


def special_attention_reason(r: dict[str, Any]) -> str | None:
    """Return a human-readable reason the guest qualifies for special attention,
    or None if they do not. Matches the rules the user confirmed."""
    reasons: list[str] = []

    vip = (r.get("vip") or "").strip()
    vip_desc = (r.get("guest_vip_desc") or "").strip()
    if vip:
        reasons.append(vip_desc or vip)

    if is_complimentary(r):
        reasons.append("Complimentary")
    if is_pep(r):
        reasons.append("PEP")

    g = (r.get("group_name") or "").upper()
    if any(k in g for k in FF_KEYWORDS):
        reasons.append("F&F")

    codes = _special_request_codes(r)
    if "HON" in codes:
        reasons.append("Honeymoon")
    if "VIP" in codes and "VIP" not in " ".join(reasons).upper():
        reasons.append("VIP request")

    if has_allergy_keywords(r):
        reasons.append("Allergy note")

    phrase = _comment_attention_match(r)
    if phrase == "vip":
        reasons.append("VIP in notes")
    elif phrase == "extra attention":
        reasons.append("Extra attention")
    elif phrase in ("tui staff", "dertour staff", "staff"):
        reasons.append("Staff")
    elif phrase == "demanding":
        reasons.append("Demanding guest")
    elif phrase == "prize winner":
        reasons.append("Prize winner")

    ta = (r.get("travel_agent_name") or "").upper()
    group = (r.get("group_name") or "").upper()
    for kw in NOTABLE_AGENT_KEYWORDS:
        if kw in ta or kw in group:
            reasons.append(kw.title())
            break

    # De-dupe while preserving order
    seen: set[str] = set()
    out: list[str] = []
    for reason in reasons:
        key = reason.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(reason)

    return " / ".join(out) if out else None


# ─── Occupancy trio (today / tomorrow / following) ──────────────────────────

TOTAL_SELLABLE_ROOMS = 276  # Daios Cove total sellable rooms (confirmed 2026-04-21)

@dataclass
class OccupancyDay:
    label: str                # "TODAY" / "TOMORROW" / "FOLLOWING"
    report_date: date
    occ_rooms: int
    guests_inh: int
    arrivals: int
    departures: int
    occupancy_pct: float


def occupancy_for_day(records: Iterable[dict[str, Any]], d: date, total_rooms: int = TOTAL_SELLABLE_ROOMS) -> OccupancyDay:
    records = list(records)
    in_house = [r for r in records if _in_house_on(r, d)]
    # Distinct rooms actually occupied
    rooms_in_use = {r.get("room") for r in in_house if r.get("room")}
    occ_rooms = len(rooms_in_use)
    guests_inh = sum((r.get("pax") or 0) for r in in_house)
    arrivals = sum(1 for r in records if _arrives_on(r, d))
    departures = sum(1 for r in records if _departs_on(r, d))
    pct = (occ_rooms / total_rooms * 100) if total_rooms else 0.0
    label = {0: "TODAY", 1: "TOMORROW", 2: "FOLLOWING"}.get(0)  # overwritten below
    return OccupancyDay(
        label="",  # filled in by compute_flash
        report_date=d,
        occ_rooms=occ_rooms,
        guests_inh=guests_inh,
        arrivals=arrivals,
        departures=departures,
        occupancy_pct=round(pct, 2),
    )


# ─── Guest attributes for UI rendering ──────────────────────────────────────

def display_name(r: dict[str, Any]) -> str:
    title = (r.get("guest_title_desc") or r.get("guest_title") or "").strip()
    first = (r.get("guest_first_name") or "").strip()
    last = (r.get("guest_name") or "").strip()
    parts = [p for p in (title, first, last) if p]
    return " ".join(parts)


def birthday_on(r: dict[str, Any], d: date) -> bool:
    b = _as_date(r.get("birth_date"))
    if b is None:
        return False
    return (b.month, b.day) == (d.month, d.day)


# ─── The main roll-up ───────────────────────────────────────────────────────

@dataclass
class FlashGuest:
    room: str | None
    name: str
    reason: str | None
    vip: str | None
    travel_agent: str | None
    group_name: str | None
    accompanying: str | None
    nationality: str | None
    allergy_flag: bool = False
    honeymoon: bool = False
    resv_name_id: int | None = None


@dataclass
class DailyFlash:
    report_date: date
    occupancy: list[dict[str, Any]]
    special_attention_arrivals: list[dict[str, Any]] = field(default_factory=list)
    special_attention_departures: list[dict[str, Any]] = field(default_factory=list)
    complimentary_partner_arrivals: list[dict[str, Any]] = field(default_factory=list)
    pep_arrivals: list[dict[str, Any]] = field(default_factory=list)
    birthdays_today: list[dict[str, Any]] = field(default_factory=list)
    allergies_in_house: list[dict[str, Any]] = field(default_factory=list)

    # Admin-entered / external — empty in Phase 1
    weather: list[dict[str, Any]] = field(default_factory=list)
    mod: dict[str, Any] | None = None
    hotel_events: list[str] = field(default_factory=list)
    site_inspections: list[str] = field(default_factory=list)
    group_events: list[str] = field(default_factory=list)
    show_rooms: list[str] = field(default_factory=list)
    pool_heating: list[dict[str, Any]] = field(default_factory=list)


def _guest_payload(r: dict[str, Any]) -> dict[str, Any]:
    return asdict(FlashGuest(
        room=r.get("room"),
        name=display_name(r) or (r.get("guest_name") or ""),
        reason=special_attention_reason(r),
        vip=r.get("vip"),
        travel_agent=r.get("travel_agent_name"),
        group_name=r.get("group_name"),
        accompanying=r.get("accompanying_names"),
        nationality=r.get("guest_country_desc") or r.get("nationality"),
        allergy_flag=has_allergy_keywords(r),
        honeymoon="HON" in _special_request_codes(r),
        resv_name_id=r.get("resv_name_id"),
    ))


def compute_flash(
    records: list[dict[str, Any]],
    report_date: date,
    promoted_rooms: set[str] | None = None,
) -> DailyFlash:
    """Compute everything the deterministic layer can provide for `report_date`.

    `promoted_rooms`: rooms manually promoted to special attention by Guest Relations.
    """
    promoted_rooms = promoted_rooms or set()

    tomorrow = report_date + timedelta(days=1)
    following = report_date + timedelta(days=2)

    occ_today = occupancy_for_day(records, report_date)
    occ_tomorrow = occupancy_for_day(records, tomorrow)
    occ_following = occupancy_for_day(records, following)
    occ_today.label = "TODAY"
    occ_tomorrow.label = "TOMORROW"
    occ_following.label = "FOLLOWING"

    arrivals = [r for r in records if _arrives_on(r, report_date)]
    departures_ = [r for r in records if _departs_on(r, report_date)]
    in_house = [r for r in records if _in_house_on(r, report_date)]

    def _is_special(r: dict[str, Any]) -> bool:
        if special_attention_reason(r):
            return True
        return r.get("room") in promoted_rooms

    special_arrivals = [
        _guest_payload(r) for r in arrivals if _is_special(r)
    ]
    special_departures = [
        _guest_payload(r) for r in departures_ if _is_special(r)
    ]

    complimentary_partner_arrivals = [
        _guest_payload(r)
        for r in arrivals
        if is_complimentary(r) and r.get("accompanying_names")
    ]

    pep_arrivals = [_guest_payload(r) for r in arrivals if is_pep(r)]

    birthdays = [
        _guest_payload(r)
        for r in in_house
        if birthday_on(r, report_date)
    ]

    allergies_in_house = [
        _guest_payload(r)
        for r in in_house
        if has_allergy_keywords(r)
    ]

    # Stable sort by room number for display
    def _by_room(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        def key(x: dict[str, Any]) -> tuple[int, str]:
            room = (x.get("room") or "")
            try:
                return (0, f"{int(room):05d}")
            except (TypeError, ValueError):
                return (1, room)
        return sorted(items, key=key)

    return DailyFlash(
        report_date=report_date,
        occupancy=[asdict(o) for o in (occ_today, occ_tomorrow, occ_following)],
        special_attention_arrivals=_by_room(special_arrivals),
        special_attention_departures=_by_room(special_departures),
        complimentary_partner_arrivals=_by_room(complimentary_partner_arrivals),
        pep_arrivals=_by_room(pep_arrivals),
        birthdays_today=birthdays,
        allergies_in_house=_by_room(allergies_in_house),
    )
