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
        description="True if the comment mentions a pool fence (e.g. 'POOL FENCE', 'pool fence free')."
    )
    pool_heating: bool = Field(
        description="True if pool heating is mentioned (e.g. 'HP', 'heated pool', 'pool heating')."
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
            "List of specific amenities offered — e.g. 'Raki 200ml', "
            "'breakfast in room', 'spa voucher', 'olive paste platter'. "
            "Empty list if none."
        ),
    )
    payment_notes: Optional[str] = Field(
        default=None,
        description=(
            "Short note about payment or tax instructions (e.g. "
            "'100% on arrival', 'government tax paid by guest on check-out'). "
            "Null if none."
        ),
    )
    ops_summary: str = Field(
        description=(
            "One-sentence operational summary of the comment, oriented to front office / "
            "guest relations. Leave empty string if comment is truly empty."
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
   Never bury an allergy inside ops_summary and leave the field false. If it \
   says "cross-check allergies with the guests" with no specific allergen, \
   still mark `allergies_present=true`.
2. Distinguish request vs granted. "free transfer" → free_transfer=true. \
   "check if departure transfer is required" → free_transfer=false.
3. `amenities` is a list of concrete items offered to the guest (gift, \
   voucher, in-room breakfast, spa treatments). Do NOT include generic \
   operational notes.
4. `payment_notes` captures who pays what and when (100% on arrival, climate \
   tax by guest, etc.). Keep it concise.
5. `ops_summary` is ONE sentence, factual, no fluff. Do not duplicate other \
   fields.
6. If the comment is empty, the string "TEST", or meaningless, set all \
   booleans to false, lists to empty, strings to null, and ops_summary to "".
7. Never invent facts. If the comment doesn't say it, don't set it.

Examples:

COMMENTS:
"Dr. Magistro is allergic to nuts (zoho notes from previous stay) -> plz \
cross-check allergies with the guests.\n\nPOOL FENCE FREE\n3 kids = new born \
baby & 2y.o & 4 y.o.\n\nRequest for LCO until midday"

→ allergies_present=true, allergies_text="Dr. Magistro is allergic to nuts",
  pool_fence=true, late_checkout=true, ops_summary="Allergy (nuts), pool fence requested, 3 young children, late checkout requested until midday."

COMMENTS:
"Repeaters:\nRaki 200ml & 1L Water\nMini Rusks, olive paste platter\n1xFree of \
Thermal Spa Suite for 2 Adults\nin-room breakfast\n\nGOVERNMENT TAX TO BE PAID \
BY THE GUESTS UPON CHECK OUT"

→ amenities=["Raki 200ml", "1L Water", "Mini Rusks", "olive paste platter",
             "Thermal Spa Suite for 2 adults", "in-room breakfast"],
  payment_notes="Government tax paid by guests on check-out",
  ops_summary="Repeater welcome amenities prepared; guest to pay government tax on checkout."

COMMENTS:
"already in house from 18/04\nfree transfers\n\nFirst aid training kids club"
→ already_in_house=true, free_transfer=true,
  ops_summary="Guest already in house since 18/04; free transfers; child attending first-aid kids-club session."

COMMENTS:
"INFORM FOR FREE UPGRADE CJSTE-->CPRESP"
→ free_upgrade=true, ops_summary="Free upgrade CJSTE to CPRESP to be communicated to the guest."

COMMENTS:
"VIP Booking - Extra Attention"
→ vip_flag=true, ops_summary="VIP booking; give extra attention on arrival."

COMMENTS:
"TEST"
→ all false/null/empty.
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
            extra = getattr(ext, field) if field else ext.ops_summary
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
