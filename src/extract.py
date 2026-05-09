"""Phase 2 — LLM extraction over the COMMENTS free-text field.

Uses `client.messages.parse()` with a Pydantic schema for structured output.
Runs in parallel across reservations via AsyncAnthropic + a semaphore.
System prompt is cached so the 100–200 daily calls pay only the per-record delta.

Usage:
    python src/extract.py samples/Daily\\ Flash\\ 20.04.2026.xlsx --date 2026-04-20
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable, Optional

from dotenv import load_dotenv
from pydantic import BaseModel, Field

try:
    from anthropic import AsyncAnthropic
except ImportError as e:  # pragma: no cover
    raise SystemExit(
        "Install deps first:  pip install -r requirements.txt"
    ) from e

from ingest import parse_file


ENV_PATH = Path(__file__).resolve().parent.parent / ".env"
# override=True so shell-exported empty values don't shadow the .env file
load_dotenv(ENV_PATH, override=True)

DEFAULT_MODEL = os.environ.get("DAILY_FLASH_MODEL", "claude-opus-4-7")
DEFAULT_CONCURRENCY = int(os.environ.get("DAILY_FLASH_CONCURRENCY", "5"))
DEFAULT_WINDOW_DAYS = 7


# ─── Schema ─────────────────────────────────────────────────────────────────

class CommentExtraction(BaseModel):
    """Structured fields extracted from the Opera COMMENTS free-text field."""

    allergies_present: bool = Field(
        description="True if the comment mentions ANY allergy or intolerance (nut, peanut, gluten, dairy, seafood, etc.)."
    )
    allergies_text: Optional[str] = Field(
        default=None,
        description="Verbatim or close paraphrase of the allergy sentence. Null if no allergy.",
    )
    pool_fence: bool = Field(
        description=(
            "True if a pool safety fence is requested or arranged for this stay. "
            "Set True for any of: 'POOL FENCE', 'pool fence', 'pool fence free', "
            "'FOC pool fence', 'fence around the pool', 'kid-proof the pool', "
            "'pool gate', 'pool safety', or the Greek equivalents "
            "('κάγκελο πισίνας', 'φράχτης πισίνας', 'περίφραξη πισίνας'). "
            "Set False if the request was denied / declined / not approved. "
            "DO NOT infer fence from kids alone ('1 kid = 4 y.o.', "
            "'child age 3', 'baby', 'toddler') — only an explicit fence "
            "request or arrangement counts. False if the comment doesn't "
            "mention a fence at all."
        )
    )
    pool_heating: bool = Field(
        description=(
            "True if pool heating is requested or arranged for this stay. "
            "Set True for any of: 'HP', 'heated pool', 'pool heating', "
            "'please heat the pool', 'make sure the pool is heated', or "
            "the Greek equivalents ('ζεστή πισίνα', 'θέρμανση πισίνας', "
            "'να ζεσταθεί η πισίνα', 'θερμαινόμενη πισίνα'). "
            "Set False if the request was denied / declined / not approved "
            "(e.g. 'requested pool heating - denied', 'pool heating not "
            "approved'). The denial overrides the request — read the whole "
            "phrase, not just the keyword. "
            "DO NOT infer heating from villa booking, private-pool category, "
            "or season — only an explicit heating request or arrangement "
            "in the comment counts. False if the comment doesn't mention "
            "pool heating at all."
        )
    )
    free_transfer: bool = Field(
        description="True if arrival/departure transfer is complimentary (e.g. 'free transfers')."
    )
    free_upgrade: bool = Field(
        description="True if a room upgrade is complimentary (e.g. 'FREE UPGRADE DLX-->CJSTE')."
    )
    late_checkout: bool = Field(
        description="True if a late check-out is requested or granted (e.g. 'LCO until midday')."
    )
    honeymoon: bool = Field(
        description="True if the stay is marked as honeymoon or anniversary celebration."
    )
    vip_flag: bool = Field(
        description="True if the comment explicitly flags VIP / extra attention / staff booking."
    )
    already_in_house: bool = Field(
        description="True if the comment indicates the guest is already in house from a prior date."
    )
    amenities: list[str] = Field(
        default_factory=list,
        description=(
            "Physical welcome-amenity items prepared for the guest's room or "
            "arrival — concrete things placed in the room or served on arrival. "
            "Examples: 'Raki 200ml', 'fresh fruit basket', 'sparkling wine', "
            "'in-room breakfast', 'olive paste platter', 'flower arrangement', "
            "'sugared almonds bed decor'. "
            "DO NOT include: resort credits, discounts, free upgrades, free "
            "transfers, requested room equipment (high chairs, bottle "
            "warmers, baby food, extra towels), mobility/accessibility aids, "
            "payment/tax instructions, or any operational requests. "
            "Those go in ops_notes or the specific boolean fields. "
            "Empty list if there are no welcome-amenity items."
        ),
    )
    payment_notes: Optional[str] = Field(
        default=None,
        description=(
            "Short note about payment, tax, or resort-credit instructions "
            "(e.g. '100% on arrival', 'government tax paid by guest on check-out', "
            "'€75 JET2 resort credit per booking — not valid for Spa or Kids Club'). "
            "Null if none."
        ),
    )
    ops_notes: str = Field(
        description=(
            "Structured operational notes for front office / guest relations. "
            "Multi-line bullet list capturing EVERY important detail from the "
            "comment that doesn't already fit allergies, amenities, or the "
            "specific boolean flags. Format: each fact on its own line, prefixed "
            "with '- '. Cover: party context (honeymoon, anniversary, VIP, "
            "trade booking, repeater), room-equipment requests (baby gear, "
            "mobility aids, extra towels), upgrade/transfer/late-checkout "
            "requests, special occasions, dietary preferences (non-allergy), "
            "first-aid/medical context, room view or rate notes, anything "
            "marked URGENT/PRIORITY. Empty string only if the comment is "
            "truly empty or 'TEST'."
        )
    )


# ─── Prompt ─────────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """\
You are an information-extraction assistant for the Daios Cove 5-star resort in \
Crete. Your job is to read the free-text COMMENTS field from an Opera PMS \
reservation record and return a structured JSON object describing what the \
front-office and guest-relations teams need to know for today's Daily Flash.

Rules:
1. ALLERGIES are the single most important field. If the text contains ANY \
   indication of allergy, intolerance, or special dietary need, set \
   `allergies_present=true` and copy the relevant sentence to `allergies_text`. \
   Never bury an allergy inside ops_notes and leave the field false. If it \
   says "cross-check allergies with the guests" with no specific allergen, \
   still mark `allergies_present=true`.

2. Distinguish request vs granted. "free transfer" → free_transfer=true. \
   "check if departure transfer is required" → free_transfer=false.

3. `amenities` IS STRICTLY for physical welcome-amenity items placed in the \
   room or served on arrival. Examples that ARE amenities: "Raki 200ml", \
   "fresh fruit basket", "sparkling wine", "in-room breakfast", "olive paste \
   platter", "flower arrangement", "sugared almonds bed decor", "honey & \
   walnuts platter". \
   Examples that are NOT amenities (these go in ops_notes or specific boolean \
   fields): \
     - Resort credits and discounts ("€75 resort credit", "20% off KEPOS") \
     - Free upgrades ("upgrade to CPRESP if available") → free_upgrade=true + ops_notes \
     - Free transfers → free_transfer=true \
     - Requested room equipment ("bottle warmer", "high chair", "baby food set", \
       "extra towels", "interconnecting room") \
     - Mobility / accessibility aids ("wheelchair", "ramp access") \
     - Payment instructions ("government tax on checkout") → payment_notes \
   When in doubt, ask: "is this a physical item the team places in the room \
   as a gift?" If yes → amenity. If no → ops_notes.

4. `payment_notes` captures payment/tax/resort-credit terms (who pays what, \
   when, what's included or excluded). Keep it concise but complete.

5. `ops_notes` is a MULTI-LINE BULLET LIST, one fact per line, prefixed with \
   "- ". It must capture EVERY important detail from the comment that doesn't \
   already fit allergies, amenities, or specific boolean flags. Do not compress \
   to one sentence. Do not omit details to keep it short. The front office \
   reads this when they pull up the guest record — losing context here means \
   losing it for service. \
   Cover: party context (honeymoon, anniversary, trade booking, repeater, VIP), \
   equipment requests, upgrade/transfer/late-checkout context, special occasions, \
   medical/first-aid context, room view or rate notes, anything URGENT.

6. If the comment is empty, the string "TEST", or meaningless, set all \
   booleans to false, lists to empty, strings to null, and ops_notes to "".

7. Never invent facts. If the comment doesn't say it, don't set it.

Examples:

COMMENTS:
"Dr. Magistro is allergic to nuts (zoho notes from previous stay) -> plz \
cross-check allergies with the guests.\n\nPOOL FENCE FREE\n3 kids = new born \
baby & 2y.o & 4 y.o.\n\nRequest for LCO until midday"

→ allergies_present=true, allergies_text="Dr. Magistro is allergic to nuts",
  pool_fence=true, late_checkout=true,
  ops_notes="- Family of 3 children: newborn, 2 y.o., 4 y.o.\\n- Late checkout requested until midday\\n- Pool fence requested\\n- Cross-check allergy details with guest on arrival"

COMMENTS:
"Repeaters:\nRaki 200ml & 1L Water\nMini Rusks, olive paste platter\n1xFree of \
Thermal Spa Suite for 2 Adults\nin-room breakfast\n\nGOVERNMENT TAX TO BE PAID \
BY THE GUESTS UPON CHECK OUT"

→ amenities=["Raki 200ml", "1L Water", "Mini Rusks", "olive paste platter",
             "Thermal Spa Suite for 2 Adults", "in-room breakfast"],
  payment_notes="Government tax paid by guests on check-out",
  ops_notes="- Repeater guests — set up arrival amenity package"

COMMENTS:
"DLXP on RC as per SL - sea view please\n\nKrystle Johnston - Head of Sales \
at Destinology\nLauren Dempster - Sales Manager at Destinology (Mother and \
daughter)\nbaby = 10 m. - plz place a Bottle warmer/steriliser, High chair, \
Baby food set\n\nRepeaters:\nRaki 200ml & 1L Water\nMini Rusks, olive paste \
platter\n1xFree of Thermal Spa Suite for 2 Adults\nin-room breakfast\n\n\
€100 resort credit per booking (non-refundable, valid for extra F&B, spa \
treatments, vitality pool — excludes creche and spa products)"

→ amenities=["Raki 200ml", "1L Water", "Mini Rusks", "olive paste platter",
             "Thermal Spa Suite for 2 Adults", "in-room breakfast"],
  payment_notes="€100 Destinology resort credit per booking (non-refundable) — valid for extra F&B, spa treatments, vitality pool. Excludes creche and spa products.",
  vip_flag=true,
  ops_notes="- Trade booking — Destinology Head of Sales (Krystle Johnston) and Sales Manager (Lauren Dempster), mother and daughter\\n- DLXP on RC rate, sea view requested\\n- 10-month-old baby — place bottle warmer/steriliser, high chair, baby food set in room\\n- Repeater guests — set up arrival amenity package"

COMMENTS:
"already in house from 18/04\nfree transfers\n\nFirst aid training kids club"
→ already_in_house=true, free_transfer=true,
  ops_notes="- Already in house since 18/04\\n- Child attending first-aid kids-club session"

COMMENTS:
"INFORM FOR FREE UPGRADE CJSTE-->CPRESP"
→ free_upgrade=true,
  ops_notes="- Free upgrade CJSTE → CPRESP to be communicated to the guest on arrival"

COMMENTS:
"VIP Booking - Extra Attention"
→ vip_flag=true,
  ops_notes="- VIP booking — give extra attention on arrival"

COMMENTS:
"TEST"
→ all false/null/empty, ops_notes="".
"""


# ─── Helpers ────────────────────────────────────────────────────────────────

def _should_extract(r: dict[str, Any], start: date, end: date) -> bool:
    """In-scope reservations: arriving in [start, end] AND non-empty comments."""
    arrival = r.get("arrival")
    if isinstance(arrival, datetime):
        arrival = arrival.date()
    if not isinstance(arrival, date):
        return False
    if not (start <= arrival <= end):
        return False
    comments = (r.get("comments") or "").strip()
    if not comments:
        return False
    return True


def hash_comment(text: str | None) -> str:
    """Phase 48 — deterministic hash of a reservation comment. Used to
    detect mid-stay comment edits and trigger a fresh extraction. We
    normalise (strip + collapse whitespace + lowercase) before hashing
    so cosmetic edits (extra spaces, casing) don't trigger needless
    re-extractions."""
    import hashlib
    import re as _re
    raw = (text or "").strip()
    normalised = _re.sub(r"\s+", " ", raw).lower()
    return hashlib.sha256(normalised.encode("utf-8")).hexdigest()


def select_extraction_candidates(
    records: list[dict[str, Any]],
    report_date: date,
    window_days: int,
    *,
    existing_hash_by_rnid: dict[int, str | None] | None = None,
) -> list[dict[str, Any]]:
    """Phase 48 — return reservations that need extraction.

    Two cases:

    1. Arrival window. Existing logic — any reservation arriving in
       [report_date, report_date + window_days] with non-empty comments.

    2. In-house with changed comments. Any reservation currently in-house
       (arrived <= report_date <= departure) whose comment hash differs
       from the stored hash in `comment_extractions.comment_hash`.

    Caller passes `existing_hash_by_rnid` keyed on `resv_name_id`. Pass
    None or an empty dict to skip case 2 (e.g. when running locally
    without DB access).
    """
    end = report_date + timedelta(days=window_days)

    arrivals = [r for r in records if _should_extract(r, report_date, end)]

    if not existing_hash_by_rnid:
        return arrivals

    arrival_rnids = {r.get("resv_name_id") for r in arrivals}
    in_house_changed: list[dict[str, Any]] = []
    for r in records:
        rnid = r.get("resv_name_id")
        if rnid is None or rnid in arrival_rnids:
            continue
        arrival = r.get("arrival")
        if isinstance(arrival, datetime):
            arrival = arrival.date()
        departure = r.get("departure")
        if isinstance(departure, datetime):
            departure = departure.date()
        if not isinstance(arrival, date) or not isinstance(departure, date):
            continue
        if not (arrival <= report_date <= departure):
            continue
        comments = (r.get("comments") or "").strip()
        if not comments:
            continue
        current_hash = hash_comment(comments)
        stored_hash = existing_hash_by_rnid.get(rnid)
        if stored_hash != current_hash:
            in_house_changed.append(r)

    return arrivals + in_house_changed


def _reservation_prompt(r: dict[str, Any]) -> str:
    name_parts = [r.get("guest_title_desc") or r.get("guest_title") or "",
                  r.get("guest_first_name") or "",
                  r.get("guest_name") or ""]
    name = " ".join(p for p in name_parts if p)
    header = (
        f"Room {r.get('room') or '?'} | Guest: {name or '(unknown)'} | "
        f"Arrival: {r.get('arrival')} | Party: {r.get('pax') or '?'} "
        f"({r.get('adults') or 0} adults / {r.get('children') or 0} children)"
    )
    comments = (r.get("comments") or "").strip()
    return f"{header}\n\nCOMMENTS:\n\"\"\"\n{comments}\n\"\"\""


# ─── Extraction ─────────────────────────────────────────────────────────────

async def _extract_one(
    client: AsyncAnthropic,
    record: dict[str, Any],
    model: str,
    semaphore: asyncio.Semaphore,
) -> tuple[Optional[int], Optional[CommentExtraction], Optional[str]]:
    async with semaphore:
        try:
            response = await client.messages.parse(
                model=model,
                max_tokens=1024,
                system=[{
                    "type": "text",
                    "text": SYSTEM_PROMPT,
                    "cache_control": {"type": "ephemeral"},
                }],
                messages=[{"role": "user", "content": _reservation_prompt(record)}],
                output_format=CommentExtraction,
            )
            return record.get("resv_name_id"), response.parsed_output, None
        except Exception as e:  # extraction should never abort the batch
            return record.get("resv_name_id"), None, f"{type(e).__name__}: {e}"


async def extract_batch(
    records: list,
    *,
    model: str = DEFAULT_MODEL,
    concurrency: int = DEFAULT_CONCURRENCY,
) -> tuple:
    """Run extraction for all provided records concurrently.

    Returns (extractions_by_resv_name_id, errors_by_resv_name_id, usage_summary).
    """
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise RuntimeError(
            "ANTHROPIC_API_KEY not set. Add it to .env before running Phase 2."
        )

    client = AsyncAnthropic()
    semaphore = asyncio.Semaphore(concurrency)
    tasks = [_extract_one(client, r, model, semaphore) for r in records]
    results = await asyncio.gather(*tasks)

    extractions: dict[int, CommentExtraction] = {}
    errors: dict[int, str] = {}
    for resv_id, extraction, err in results:
        if resv_id is None:
            continue
        if err:
            errors[resv_id] = err
        elif extraction is not None:
            extractions[resv_id] = extraction

    summary = {
        "total": len(records),
        "succeeded": len(extractions),
        "failed": len(errors),
    }
    return extractions, errors, summary


# ─── CLI ────────────────────────────────────────────────────────────────────

def _parse_date(s: str) -> date:
    return datetime.strptime(s, "%Y-%m-%d").date()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("xlsx")
    ap.add_argument("--date", required=True, type=_parse_date)
    ap.add_argument("--window-days", type=int, default=DEFAULT_WINDOW_DAYS,
                    help=f"Extract for arrivals within N days (default {DEFAULT_WINDOW_DAYS})")
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--concurrency", type=int, default=DEFAULT_CONCURRENCY)
    ap.add_argument("--limit", type=int, default=None,
                    help="Only extract for the first N in-scope records (debug)")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    records = parse_file(args.xlsx)
    end = args.date + timedelta(days=args.window_days)
    in_scope = [r for r in records if _should_extract(r, args.date, end)]
    if args.limit:
        in_scope = in_scope[: args.limit]

    print(f"Extracting {len(in_scope)} reservations "
          f"(arrivals {args.date}..{end}, non-empty comments) "
          f"with model={args.model}, concurrency={args.concurrency}")

    extractions, errors, summary = asyncio.run(
        extract_batch(in_scope, model=args.model, concurrency=args.concurrency)
    )

    print(f"\nResult: {summary['succeeded']}/{summary['total']} succeeded, "
          f"{summary['failed']} failed")

    # Key findings: allergies, honeymoon, free upgrades, pool fence
    allergies = [(rid, x) for rid, x in extractions.items() if x.allergies_present]
    upgrades = [(rid, x) for rid, x in extractions.items() if x.free_upgrade]
    pool_fence = [(rid, x) for rid, x in extractions.items() if x.pool_fence]
    honeymoon = [(rid, x) for rid, x in extractions.items() if x.honeymoon]

    by_resv_id = {r.get("resv_name_id"): r for r in in_scope}

    def _show(title: str, items: list[tuple[int, CommentExtraction]], field: str | None = None) -> None:
        print(f"\n── {title} ({len(items)}) ──")
        for rid, ext in items:
            r = by_resv_id.get(rid, {})
            room = r.get("room") or "?"
            name = (r.get("guest_first_name") or "") + " " + (r.get("guest_name") or "")
            extra = getattr(ext, field) if field else ext.ops_notes
            print(f"  {room:>5}  {name.strip():<30}  {extra}")

    _show("ALLERGIES", allergies, "allergies_text")
    _show("POOL FENCE", pool_fence)
    _show("FREE UPGRADES", upgrades)
    _show("HONEYMOON", honeymoon)

    if errors:
        print(f"\n── ERRORS ({len(errors)}) ──")
        for rid, err in list(errors.items())[:10]:
            print(f"  resv_name_id={rid}: {err}")

    if args.json:
        out = {str(rid): ext.model_dump() for rid, ext in extractions.items()}
        print("\n── JSON ──")
        print(json.dumps(out, indent=2, ensure_ascii=False))

    return 0 if not errors else 2


if __name__ == "__main__":
    sys.exit(main())
