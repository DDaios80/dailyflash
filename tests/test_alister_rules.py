"""Offline tests for the 2026-11-03 A-lister / dignitary rules.

The Ambassador of India (Ravi Shankar, comp stay, Opera note "TOP VIP -
EXTRA ATTENTION") was on Special Attention but not on the A-lister panel:
research never saw the notes, and the comp company on the booking is the
resort's own. No network or database needed.
"""
from __future__ import annotations

from datetime import date, datetime

from alister import _user_prompt, subjects_from_reservation
from compute import dignitary_match, front_office_top_vip, special_attention_reason
from daily import assemble_payload_in_memory


def _r(comments: str = "", **kw) -> dict:
    return {"comments": comments, **kw}


# ─── dignitary_match ────────────────────────────────────────────────────────

def test_dignitary_matches_real_titles():
    assert dignitary_match(_r("Ambassador of India to Greece - TOP VIP")) == "Ambassador"
    assert dignitary_match(_r("H.E. the Indian Ambassador and spouse")) == "Ambassador"
    assert dignitary_match(_r("client is Minister so we need the best rooms")) == "Minister"
    assert dignitary_match(_r("Mr Ali alatteya is teh minister of Quatar")) == "Minister"
    assert dignitary_match(_r("might be the Honorary Consulate of Tunisia in Styria")) == "Consul"
    assert dignitary_match(_r("Ο κ. Χ είναι ο Πρέσβης της Ινδίας")) == "Ambassador"
    assert dignitary_match(_r("ΠΡΕΣΒΗΣ ΤΗΣ ΙΝΔΙΑΣ")) == "Ambassador"
    assert dignitary_match(_r("Υφυπουργός Τουρισμού")) == "Minister"


def test_dignitary_ignores_marketing_and_lookalikes():
    for text in (
        "πρέπει να την κάνουμε ambassador, ξέρει όλη την καλή κοινωνία",
        "Έρχετε σαν ambassador των Sunday Natural και Influencer",
        "να γίνει ο Ambassador μας",
        "brand ambassador of Lululemon",
        "she will be an ambassador to us",
        "JOSEPH IS A SALES CONSULTANT IN OUR CHESTER TRAVEL CENTRE",
        "SASA NIK CONSULTING LLC",
        "TOP VIP - EXTRA ATTENTION",
        "Mr. Crosetto is the direct cousin of the Italian Minister of Defense",
        "ο ξάδερφος του υπουργού",
        "",
    ):
        assert dignitary_match(_r(text)) is None, text


def test_dignitary_reaches_special_attention():
    reason = special_attention_reason(_r("Ambassador of India to Greece"))
    assert reason and "Dignitary (Ambassador)" in reason


# ─── front_office_top_vip ───────────────────────────────────────────────────

def test_top_vip_line():
    assert front_office_top_vip(_r("TOP VIP - EXTRA ATTENTION")) == "TOP VIP - EXTRA ATTENTION"
    assert front_office_top_vip(_r("3 kids\n\nMR.CASPI OMRI TOP VIP FORMER NBA PLAYER\nrooms")) \
        == "MR.CASPI OMRI TOP VIP FORMER NBA PLAYER"
    assert front_office_top_vip(_r("TOP VIP-EXTRA ATTENTION")) == "TOP VIP-EXTRA ATTENTION"
    assert front_office_top_vip(_r("VIP for TA")) is None
    assert front_office_top_vip(_r("")) is None


# ─── research input ─────────────────────────────────────────────────────────

AMBASSADOR = {
    "resv_name_id": 3322689,
    "guest_first_name": "Ravi",
    "guest_name": "Shankar",
    "guest_country_desc": "India",
    "company_name": "HELLAS HOLIDAY HOTELS S.A.",
    "group_name": "COMPLIMENTARY 2026",
    "special_requests": "VIP",
    "complimentary_yn": "Y",
    "comments": "TOP VIP - EXTRA ATTENTION\n\nAmbassador of India to Greece",
    "room": "147148",
    "arrival": datetime(2026, 11, 3),
    "departure": datetime(2026, 11, 5),
    "adults": 3,
}


def test_research_gets_notes_not_own_company():
    (subject,) = subjects_from_reservation(AMBASSADOR)
    assert subject.company is None
    assert subject.hotel_notes == "TOP VIP - EXTRA ATTENTION Ambassador of India to Greece"
    prompt = _user_prompt(subject)
    assert "Ambassador of India to Greece" in prompt
    assert "HELLAS HOLIDAY" not in prompt


def test_real_company_still_passed():
    (subject,) = subjects_from_reservation({**AMBASSADOR, "company_name": "Siemens Healthineers"})
    assert subject.company == "Siemens Healthineers"


# ─── A-lister panel ─────────────────────────────────────────────────────────

def _panel(records, findings=None):
    payload = assemble_payload_in_memory(records, {}, findings or {}, date(2026, 11, 3), [])
    return payload["alister_findings"]


def test_top_vip_arrival_reaches_panel_when_research_found_nothing():
    rows = _panel([AMBASSADOR])
    assert len(rows) == 1
    row = rows[0]
    assert row["matched_name"] == "Ravi Shankar"
    assert row["category"] == "Front office VIP"
    assert row["review_status"] == "confirmed"
    assert row["room"] == "147148"
    assert "TOP VIP - EXTRA ATTENTION" in row["summary"]


def test_research_row_wins_over_front_office_row():
    finding = {
        "matched_name": "Ravi Shankar", "relationship": "self", "is_notable": True,
        "confidence": 95, "category": "Politician", "summary": "Ambassador of India to Greece",
        "evidence_urls": [], "photo_url": None, "review_status": "confirmed",
        "disprove_confidence": 0,
    }
    rows = _panel([AMBASSADOR], {3322689: [finding]})
    assert len(rows) == 1
    assert rows[0]["category"] == "Politician"


def test_top_vip_not_arriving_today_stays_off_panel():
    rows = _panel([{**AMBASSADOR, "arrival": datetime(2026, 11, 2)}])
    assert rows == []
