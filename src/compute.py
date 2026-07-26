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
    if a != d:
        return False
    # Phase 37 — continue-stay suppression. If this reservation's
    # arrival date == another reservation's departure date in the same
    # room for the same guest, it's an Opera stay extension, not a
    # real arrival. Don't count it.
    if r.get("_continue_stay_role") == "extension":
        return False
    return True


def _departs_on(r: dict[str, Any], d: date) -> bool:
    dep = _as_date(r.get("departure"))
    if dep != d:
        return False
    # Phase 37 — continue-stay suppression. If this reservation's
    # departure date matches another reservation's arrival date in the
    # same room for the same guest, the guest isn't leaving — they're
    # continuing the stay under a new resv_name_id.
    if r.get("_continue_stay_role") == "original":
        return False
    return True


def _in_house_on(r: dict[str, Any], d: date) -> bool:
    """Guest is staying through night of `d` (arrival<=d, departure>d)."""
    a = _as_date(r.get("arrival"))
    dep = _as_date(r.get("departure"))
    if a is None or dep is None:
        return False
    return a <= d < dep


# ─── Phase 37 — continue-stay detection ────────────────────────────────────
# Opera issues a new resv_name_id when a guest extends their stay beyond
# the original reservation's departure date. The same guest then looks
# like a departure + arrival pair on the changeover date. From an
# operational point of view they're a single continuous stay — they don't
# check out and back in. The dashboard should reflect that.
#
# We detect pairs by same room + same guest where R1.departure ==
# R2.arrival, then tag both reservations with `_continue_stay_role`:
#   'original'  — R1 (the earlier one). Its departure is suppressed.
#   'extension' — R2 (the later one).  Its arrival is suppressed.
#
# `in_house` is unaffected because both records are physically present
# on the changeover date (R1 ends at noon, R2 starts at 3pm — same room,
# same person, no checkout in practice).

def _guest_match_key(r: dict[str, Any]) -> tuple:
    """Stable key identifying the same person across reservation records.
    Prefer guest_name_id (Opera's stable per-guest id). Fall back to
    name + nationality when guest_name_id is missing."""
    gnid = r.get("guest_name_id")
    if gnid is not None:
        return ("gnid", gnid)
    last  = (r.get("guest_name") or "").strip().lower()
    first = (r.get("guest_first_name") or "").strip().lower()
    nat   = (r.get("guest_country") or r.get("nationality") or "").strip().lower()
    return ("name", last, first, nat)


def tag_continue_stay_pairs(records: list[dict[str, Any]]) -> int:
    """Mutate `records` in place, tagging each member of a continue-stay
    pair with `_continue_stay_role`. Returns the number of pairs found.

    Pair condition:
      - Same room (case-sensitive string match — Opera room codes are stable)
      - Same guest (per `_guest_match_key`)
      - R1.departure == R2.arrival as calendar dates
    """
    by_room_arrive: dict[tuple[str, date], list[dict[str, Any]]] = {}
    for r in records:
        room = r.get("room")
        a_date = _as_date(r.get("arrival"))
        if not room or a_date is None:
            continue
        by_room_arrive.setdefault((room, a_date), []).append(r)

    pairs = 0
    for r in records:
        room = r.get("room")
        d_date = _as_date(r.get("departure"))
        if not room or d_date is None:
            continue
        # Look for any reservation arriving on r's departure date in the
        # same room. If found and same guest, this is a continue-stay pair.
        candidates = by_room_arrive.get((room, d_date)) or []
        if not candidates:
            continue
        my_key = _guest_match_key(r)
        for partner in candidates:
            if partner is r:
                continue
            if _guest_match_key(partner) != my_key:
                continue
            # Found pair: r is the "original" (departing), partner is the
            # "extension" (arriving on the same date).
            r["_continue_stay_role"] = "original"
            partner["_continue_stay_role"] = "extension"
            pairs += 1
            break  # one partner is enough; pairs are 1:1
    return pairs


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


# 2026-07-26 — industry-VIP titles in COMMENTS promote to Special Attention.
# Reservations often carry notes like "Vice President of Zakynthos Lesante
# Luxury Hotel & Spa" with no VIP code set in Opera; hospitality peers
# deserve the Special Attention list even when web research (A-lister)
# finds no public notability. Matched with word boundaries, so e.g.
# "PRESIDENTIAL SUITE" does not fire "president".
INDUSTRY_VIP_KEYWORDS = (
    "vice president",
    "vice-president",
    "ceo",
    "chief executive",
    "managing director",
    # "of" required: "Ceremony conducted by the General Manager" (our own
    # GM at a guest's wedding) must not promote the couple.
    "general manager of",
    "hotel owner",
    "owner of",
    "hotelier",
    "chairman",
    "διευθύνων σύμβουλος",
    "γενικός διευθυντής",
    "ιδιοκτήτης ξενοδοχείου",
)


# Phase 38 — patterns identifying guests who are themselves travel-trade
# representatives staying at the property (FAM trips, site inspections,
# individual TA familiarisations). Distinct from regular guests booked
# THROUGH a travel agent (every booking has a travel_agent_name; we
# don't flag those). Operations want these flagged as "Special
# Attention" so the team can wow them — happy stay = more bookings.
TRAVEL_AGENT_GUEST_MARKET_PATTERNS = (
    "FAM",        # "FAM TRIP", "FAM 2026"
    "TRADE",      # "TRADE", "TRADE INVITATIONAL"
    "INDUSTRY",   # "INDUSTRY", "INDUSTRY GUEST"
)
TRAVEL_AGENT_GUEST_COMMENT_PHRASES = (
    "travel agent",
    "ta visit",
    "ta staying",
    "ta stay",
    "fam trip",
    "site inspection",
    "trade rep",
    "trade visit",
)


def _is_travel_agent_guest(r: dict[str, Any]) -> bool:
    """True if the guest is a travel-trade representative staying at the
    property (FAM trip, site inspection, individual TA visit). Returns
    false for regular guests booked through a travel agent."""
    market = (r.get("market_desc") or "").upper()
    if any(p in market for p in TRAVEL_AGENT_GUEST_MARKET_PATTERNS):
        return True
    c = (r.get("comments") or "").lower()
    if c and any(p in c for p in TRAVEL_AGENT_GUEST_COMMENT_PHRASES):
        return True
    return False

# Travel-agent / group names that warrant special attention on their own.
# Kept tight to genuine luxury channels.
# Pruning history (Daios Cove):
#   23 Apr 2026 — removed WEBHOTELIER (booking engine, not a TA),
#                 AIRTOURS / DERTOUR / DER TOUR / JET2 (bulk contract operators)
#   3 May 2026 — removed ODEON (their CORAL contract is a bulk operation —
#                Axel Born / Paulina Born and many others were being flagged
#                with no other signal). Genuine Odeon VIPs still get flagged
#                via their VIP / honeymoon / comments / allergy fields.
#
# These pools are now driven by `src/property_config.py` so each Daios
# Group property can have its own channel mix. Cove and DLL share the
# same defaults today; they can diverge once DLL has a real booking
# history.
from property_config import get as _get_property
_PROPERTY = _get_property()

NOTABLE_AGENT_KEYWORDS = _PROPERTY.notable_agent_keywords


# Booking.com bookings are flagged in their own Flash Report section so
# teams can give them exceptional service — the goal is raising the
# Booking.com rating from 8.7 to 9.2+ (the "Excellent" threshold). In the
# Opera export, these bookings carry travel_agent_name = "BOOKING" or
# "BOOK" (variant label).
BOOKING_COM_TRAVEL_AGENTS = _PROPERTY.booking_com_travel_agents


# Tour-operator / commercial-channel keywords that EXCLUDE a stay from
# "partner arrivals" even when the booking is complimentary. These are
# industry comp stays — TA/TO staff perks, agency-employee bookings,
# bulk-channel comp — distinct from true partner arrivals (artists,
# chefs, colleagues, internal collaborators that the resort hosts).
# Same set as the agents pruned from NOTABLE_AGENT_KEYWORDS — these are
# bulk-contract / commercial channels, not luxury or partner channels.
TOUR_OPERATOR_KEYWORDS = _PROPERTY.tour_operator_keywords


# Guests to keep off every arrival surface (privacy/VIP requests). Applied to
# the arrival lists here and the A-lister research in daily.py. Occupancy and
# in-house lists are untouched — this only hides the *arrival*.
SUPPRESSED_ARRIVAL_NAMES = _PROPERTY.suppressed_arrival_names


def _norm_name(s: str) -> str:
    """Lower-case, strip accents/diacritics, collapse whitespace."""
    import unicodedata
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    return " ".join(s.lower().split())


def is_suppressed_arrival(r: dict[str, Any]) -> bool:
    """True if this guest is on the property's arrival-suppression list.
    Each entry is a group of tokens (e.g. first name + surname); the guest is
    suppressed when ALL tokens in any group appear in their first+surname,
    accent-/case-/order-insensitive. Tolerant of Opera export formatting quirks;
    only the primary guest name is checked."""
    if not SUPPRESSED_ARRIVAL_NAMES:
        return False
    blob = _norm_name(f"{r.get('guest_first_name') or ''} {r.get('guest_name') or ''}")
    return any(
        all(_norm_name(tok) in blob for tok in group)
        for group in SUPPRESSED_ARRIVAL_NAMES
    )


def is_tour_operator_stay(r: dict[str, Any]) -> bool:
    """True if the reservation comes through a commercial tour operator
    or bulk channel. Used to exclude industry comp stays from
    `partner_arrivals` so the partner section stays focused on true
    partners (artists, chefs, colleagues, internal collaborators)."""
    ta = (r.get("travel_agent_name") or "").upper()
    g = (r.get("group_name") or "").upper()
    if not ta and not g:
        return False
    return any(kw in ta or kw in g for kw in TOUR_OPERATOR_KEYWORDS)


def is_booking_com(r: dict[str, Any]) -> bool:
    ta = (r.get("travel_agent_name") or "").strip().upper()
    if not ta:
        return False
    if ta in BOOKING_COM_TRAVEL_AGENTS:
        return True
    if "BOOKING.COM" in ta:
        return True
    return False


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


def industry_vip_match(r: dict[str, Any]) -> str | None:
    """Return the matched industry-VIP title phrase from COMMENTS, or None.
    Word-boundary matched so substrings inside other words don't fire."""
    c = (r.get("comments") or "").lower()
    if not c:
        return None
    for kw in INDUSTRY_VIP_KEYWORDS:
        if re.search(r"\b" + re.escape(kw) + r"\b", c):
            return kw
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

    # NOTE: allergy presence is NOT a special-attention reason on its own.
    # Allergies have their own dedicated `allergies_in_house` section in the
    # flash payload. Adding "Allergy note" here caused the same guest to
    # show up twice on the flash (once under Special Attention, once under
    # Allergies). If the guest has another genuine special-attention signal
    # (VIP, honeymoon, complimentary, etc.) they'll still appear here for
    # that reason — and separately in the allergies section.

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

    # Phase 38 — travel-trade guests staying as themselves (FAM trips,
    # site inspections, individual TA familiarisations). Detection via
    # market_desc patterns + comments phrases. Distinct from regular
    # guests booked THROUGH a TA.
    if _is_travel_agent_guest(r):
        reasons.append("Travel Agent")

    # 2026-07-26 — industry-VIP title mentioned in the reservation comments
    # (e.g. "Vice President of ... Hotel & Spa") with no VIP code in Opera.
    ivip = industry_vip_match(r)
    if ivip:
        reasons.append(f"Industry VIP ({ivip})")

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

TOTAL_SELLABLE_ROOMS = _PROPERTY.total_sellable_rooms  # property-driven (Cove=276, DLL=49)

@dataclass
class OccupancyDay:
    label: str                # "TODAY" / "TOMORROW" / "FOLLOWING"
    report_date: date
    occ_rooms: int
    guests_inh: int
    arrivals: int
    departures: int
    occupancy_pct: float
    # Phase 30 — finer breakdowns for the dashboard totals row.
    # Party-composition split: adults-only vs families with children.
    adults_only_rooms: int = 0
    adults_only_guests: int = 0
    family_rooms: int = 0
    family_guests: int = 0
    # Phase 36.1 — definitive totals for the dashboard's Kids + Cribs tiles.
    # Sum across in-house reservations (room-deduped). The dashboard reads
    # these directly instead of computing client-side (which had a bug
    # showing "1" for 50 family rooms).
    children_total: int = 0
    cribs_total: int = 0
    # Room-category split. Standard / Suite (no Collection) / Collection / Villa.
    # Counts are room counts; the dashboard can derive percentages.
    rooms_by_category: dict[str, int] = field(default_factory=dict)


# Room-category buckets. Heuristic on `room_category_label` from Opera.
# Order matters — first match wins.
def _room_bucket(label: str | None) -> str:
    if not label:
        return "Unknown"
    code = label.strip().upper()
    if not code:
        return "Unknown"
    if code.startswith("V"):
        return "Villa"
    # Collection — every C-prefix code observed in production is a
    # Collection-tier room (CJSTE, CJSTEP, CSTEP, CPRES, CPRESP, CPJSTP).
    # If a future non-Collection C-code appears, narrow this rule.
    if code.startswith("C"):
        return "Collection"
    # Suite (non-Collection) — D-prefix Junior Suites and bare suite codes:
    # DJSTE, DJSTEP, JSTEP, STE, STEP. They share STE/STP/JST suffixes.
    if "STE" in code or "JST" in code or code.endswith("STP"):
        return "Suite"
    return "Standard"


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

    # Phase 30 — party-composition + category breakdowns. Counted at room
    # level (not reservation level) so a guest sharing a room with an extra
    # bed isn't double-counted.
    # Phase 36.1 — also sum the total kids and total cribs across in-house
    # so the dashboard "Kids" tile reads from a definitive number rather
    # than aggregating on the UI side (which had a bug producing "1" for
    # 50 family rooms — wrong by 70x).
    rooms_seen: set[str] = set()
    adults_only_rooms = 0
    adults_only_guests = 0
    family_rooms = 0
    family_guests = 0
    children_total = 0
    cribs_total = 0
    rooms_by_category: dict[str, int] = {}
    for r in in_house:
        room = r.get("room")
        if not room or room in rooms_seen:
            continue
        rooms_seen.add(room)
        children = int(r.get("children") or 0)
        cribs = int(r.get("cribs") or 0)
        pax = int(r.get("pax") or 0)
        if children > 0:
            family_rooms += 1
            family_guests += pax
        else:
            adults_only_rooms += 1
            adults_only_guests += pax
        children_total += children
        cribs_total += cribs
        bucket = _room_bucket(r.get("room_category_label") or r.get("booked_room_category_label"))
        rooms_by_category[bucket] = rooms_by_category.get(bucket, 0) + 1

    return OccupancyDay(
        label="",  # filled in by compute_flash
        report_date=d,
        occ_rooms=occ_rooms,
        guests_inh=guests_inh,
        arrivals=arrivals,
        departures=departures,
        occupancy_pct=round(pct, 2),
        adults_only_rooms=adults_only_rooms,
        adults_only_guests=adults_only_guests,
        family_rooms=family_rooms,
        family_guests=family_guests,
        children_total=children_total,
        cribs_total=cribs_total,
        rooms_by_category=rooms_by_category,
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
    # Phase 29.1 — raw Opera COMMENTS so admin can opt to print them in
    # the flash PDF appendix. Most renderers ignore this field; only the
    # PDF template's renderCommentsAppendix uses it when include_comments
    # is on.
    comments: str | None = None
    # Phase 36 — party composition for the dashboard guest cards. Operations
    # need the kid count + individual ages to plan kids-club staffing,
    # restaurant high-chair allocation, breakfast counts, etc.
    adults: int | None = None
    children_count: int | None = None
    children_ages: str | None = None   # raw Opera ages text, e.g. "3, 7"
    cribs: int | None = None


@dataclass
class DailyFlash:
    report_date: date
    occupancy: list[dict[str, Any]]
    special_attention_arrivals: list[dict[str, Any]] = field(default_factory=list)
    special_attention_departures: list[dict[str, Any]] = field(default_factory=list)
    complimentary_partner_arrivals: list[dict[str, Any]] = field(default_factory=list)
    pep_arrivals: list[dict[str, Any]] = field(default_factory=list)
    booking_com_arrivals: list[dict[str, Any]] = field(default_factory=list)
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
        comments=(r.get("comments") or None),
        # Phase 36 — party composition
        adults=r.get("adults"),
        children_count=r.get("children"),
        children_ages=(r.get("ages") or None),
        cribs=r.get("cribs"),
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

    # Phase 37 — tag continue-stay reservation pairs (Opera issues a new
    # resv_name_id when a guest extends their stay; without tagging,
    # the same guest looks like a departure + arrival on the same day in
    # the same room). _arrives_on / _departs_on suppress these tagged
    # records so they don't inflate the arrival/departure counts or
    # appear on the special-attention lists.
    pairs_found = tag_continue_stay_pairs(records)
    if pairs_found:
        print(f"  Phase 37: tagged {pairs_found} continue-stay reservation pair(s)")

    tomorrow = report_date + timedelta(days=1)
    following = report_date + timedelta(days=2)

    occ_today = occupancy_for_day(records, report_date)
    occ_tomorrow = occupancy_for_day(records, tomorrow)
    occ_following = occupancy_for_day(records, following)
    occ_today.label = "TODAY"
    occ_tomorrow.label = "TOMORROW"
    occ_following.label = "FOLLOWING"

    arrivals = [
        r for r in records
        if _arrives_on(r, report_date) and not is_suppressed_arrival(r)
    ]
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

    # Partner arrivals = complimentary + companions, EXCLUDING commercial
    # tour-operator channels. Tour operator staff getting industry comp is
    # a different category and should not appear under "partners" — that
    # surface is for artists, chefs, colleagues, and internal collaborators.
    complimentary_partner_arrivals = [
        _guest_payload(r)
        for r in arrivals
        if is_complimentary(r)
        and r.get("accompanying_names")
        and not is_tour_operator_stay(r)
    ]

    pep_arrivals = [_guest_payload(r) for r in arrivals if is_pep(r)]

    booking_com_arrivals = [
        _guest_payload(r) for r in arrivals if is_booking_com(r)
    ]

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
        booking_com_arrivals=_by_room(booking_com_arrivals),
        birthdays_today=birthdays,
        allergies_in_house=_by_room(allergies_in_house),
    )


# ─── Phase 24: zoho merge ────────────────────────────────────────────────
# Bucketise zoho_notes (already populated by ingest-zoho-notes) into
# payload sections, filtered to guests who are in-house on report_date.
# Match guest by reservation_ref (confirmation_no) when present, fall back
# to room number (raw int extracted from reservations.room).

import re
from collections import Counter, defaultdict


_ROOM_INT_RE = re.compile(r"^\s*(\d+)")


def _room_int(s: Any) -> str | None:
    if s is None:
        return None
    m = _ROOM_INT_RE.match(str(s))
    return m.group(1) if m else None


def merge_zoho_into_flash(
    records: list[dict[str, Any]],
    zoho: dict[str, list[dict]],
    report_date: date,
) -> dict[str, Any]:
    """Compute the new payload sections from zoho_notes buckets.

    `records` are the parsed Opera reservations rows. `zoho` is the dict
    returned by zoho_fetch.fetch_zoho_for_report.
    """
    in_house = [r for r in records if _in_house_on(r, report_date)]
    rooms_in_house = {
        _room_int(r.get("room")) for r in in_house if r.get("room")
    }
    rooms_in_house.discard(None)
    # Cast to str defensively — Opera xlsx may parse confirmation_no as
    # numeric, in which case .strip() would AttributeError on a float.
    confirmations: dict[str, dict] = {}
    for r in in_house:
        cn = r.get("confirmation_no")
        if cn is None:
            continue
        cn_str = str(cn).strip()
        if cn_str:
            confirmations[cn_str] = r

    def _is_in_house(note: dict) -> bool:
        ref = str(note.get("reservation_ref") or "").strip()
        if ref and ref in confirmations:
            return True
        rn = _room_int(note.get("room"))
        return bool(rn and rn in rooms_in_house)

    def _bucket(key: str) -> list[dict]:
        return [n for n in (zoho.get(key) or []) if _is_in_house(n)]

    # HSK aggregation: count + top items per room (in-house only).
    hsk_in_house = _bucket("hsk_orders")
    by_room: dict[str, list[dict]] = defaultdict(list)
    for n in hsk_in_house:
        rn = _room_int(n.get("room"))
        if rn:
            by_room[rn].append(n)
    hsk_summary = []
    for rn, items in by_room.items():
        names: Counter = Counter()
        for it in items:
            label = (it.get("subject") or it.get("body") or "")[:60].strip()
            if label:
                names[label] += 1
        hsk_summary.append({
            "room": rn,
            "count": len(items),
            "top_items": [{"item": k, "qty": v} for k, v in names.most_common(5)],
        })
    hsk_summary.sort(key=lambda x: -x["count"])

    return {
        "zoho_allergies":          _bucket("allergies"),
        "zoho_medical_notes":      _bucket("medical"),
        "zoho_pending_complaints": _bucket("pending_complaints"),
        "zoho_todays_activities":  list(zoho.get("boat_trips") or []),  # date already filtered
        "zoho_hsk_summary":        hsk_summary[:50],
    }
