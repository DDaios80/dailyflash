"""Phase 3 — A-lister research agent.

For each guest (plus accompanying names), use Claude Opus 4.7 with the built-in
`web_search` tool to research whether the person is a notable public figure
(CEO/founder, athlete, artist, politician, etc.). Only findings with confidence
≥ threshold are surfaced. Evidence URLs are logged for audit.

Scope: runs per SUBJECT (one person). A reservation typically has self +
partner + accompanying → multiple subjects.

GDPR note: the cross-reference is a narrow "is this person publicly notable"
question for service enhancement. Not for marketing or profiling.

Usage:
    python src/alister.py samples/Daily\\ Flash\\ 20.04.2026.xlsx --date 2026-04-20
    python src/alister.py samples/Daily\\ Flash\\ 20.04.2026.xlsx --date 2026-04-20 --scope all
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, List, Optional

from dotenv import load_dotenv
from pydantic import BaseModel, Field

try:
    from anthropic import AsyncAnthropic, APIStatusError
except ImportError as e:  # pragma: no cover
    raise SystemExit("pip install -r requirements.txt") from e

from ingest import parse_file
from compute import special_attention_reason


ENV_PATH = Path(__file__).resolve().parent.parent / ".env"
load_dotenv(ENV_PATH, override=True)

DEFAULT_MODEL = os.environ.get("DAILY_FLASH_MODEL", "claude-opus-4-7")
DEFAULT_CONCURRENCY = int(os.environ.get("DAILY_FLASH_CONCURRENCY", "3"))
DEFAULT_CONFIDENCE_THRESHOLD = 70
DEFAULT_CACHE_TTL_DAYS = 180  # 6 months — repeater guests unlikely to change


# ─── Schema ─────────────────────────────────────────────────────────────────

class AListerFinding(BaseModel):
    matched_name: str
    relationship: str = Field(
        description="'self' | 'partner' | 'accompanying' — role of the matched person in the booking"
    )
    is_notable: bool
    confidence: int = Field(ge=0, le=100)
    category: Optional[str] = Field(
        default=None,
        description="E.g. 'CEO/Founder', 'Athlete', 'Actor', 'Politician', 'Musician', 'Author', 'Scientist', 'Royalty', 'Investor'."
    )
    summary: Optional[str] = Field(
        default=None, description="One-line plain-English summary, e.g. 'CEO of LinkedIn'."
    )
    evidence_urls: list[str] = Field(default_factory=list)
    reasoning: Optional[str] = Field(
        default=None, description="Brief justification of confidence (what disambiguated the match)."
    )


# ─── Subject extraction ─────────────────────────────────────────────────────

@dataclass
class Subject:
    """One person to research: could be the guest or an accompanying name."""
    display: str
    first_name: Optional[str]
    last_name: Optional[str]
    relationship: str  # 'self' | 'partner' | 'accompanying'
    resv_name_id: Optional[int]
    room: Optional[str]
    nationality: Optional[str]
    travel_agent: Optional[str]
    company: Optional[str]
    other_party_members: List[str] = field(default_factory=list)


_ACC_SPLIT_RE = re.compile(r"\s*/\s*")


def _split_accompanying(accompanying_names: str) -> list[tuple[str, str]]:
    """ACCOMPANYING_NAMES is 'Last, First / Last, First / ...' — return [(first, last), ...]."""
    out = []
    for piece in _ACC_SPLIT_RE.split(accompanying_names or ""):
        piece = piece.strip()
        if not piece:
            continue
        if "," in piece:
            last, first = [p.strip() for p in piece.split(",", 1)]
            out.append((first, last))
        else:
            out.append((piece, ""))
    return out


def subjects_from_reservation(r: dict) -> list[Subject]:
    """Expand a reservation into research subjects (self + partner + accompanying adults).

    Children are skipped — they're rarely publicly notable and researching
    them is both wasteful and uncomfortable. Heuristic: if the booking has
    CHILDREN > 0 and ACCOMPANYING_NAMES has ≥ CHILDREN entries, assume the
    last `CHILDREN` entries are the kids (Opera tends to list adults first).
    """
    first = (r.get("guest_first_name") or "").strip()
    last = (r.get("guest_name") or "").strip()
    nationality = r.get("guest_country_desc") or r.get("nationality")
    ta = r.get("travel_agent_name")
    company = r.get("company_name")
    acc_pairs = _split_accompanying(r.get("accompanying_names") or "")

    # Drop likely children off the end of the accompanying list.
    children_count = r.get("children") or 0
    if isinstance(children_count, int) and children_count > 0 and len(acc_pairs) >= children_count:
        acc_pairs = acc_pairs[: len(acc_pairs) - children_count]

    party = [f"{fn} {ln}".strip() for fn, ln in acc_pairs]

    out: list[Subject] = []

    if first or last:
        out.append(Subject(
            display=f"{first} {last}".strip(),
            first_name=first or None,
            last_name=last or None,
            relationship="self",
            resv_name_id=r.get("resv_name_id"),
            room=r.get("room"),
            nationality=nationality,
            travel_agent=ta,
            company=company,
            other_party_members=party,
        ))

    # First remaining accompanying = likely partner; rest = accompanying
    for idx, (fn, ln) in enumerate(acc_pairs):
        rel = "partner" if idx == 0 else "accompanying"
        display = f"{fn} {ln}".strip()
        others = [f"{first} {last}".strip()] + [
            f"{f2} {l2}".strip() for j, (f2, l2) in enumerate(acc_pairs) if j != idx
        ]
        out.append(Subject(
            display=display,
            first_name=fn or None,
            last_name=ln or None,
            relationship=rel,
            resv_name_id=r.get("resv_name_id"),
            room=r.get("room"),
            nationality=nationality,
            travel_agent=ta,
            company=company,
            other_party_members=[o for o in others if o],
        ))

    return out


# ─── Prompting ──────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """\
You are the A-lister research assistant for Daios Cove, a 5-star resort on \
Crete. The hotel wants to give exceptional service to notable guests — \
CEOs/founders, athletes, artists, politicians, authors, scientists, \
financiers, royalty, high-profile investors, etc.

Your job: given ONE subject (name + nationality + context), use web_search to \
determine whether they are a publicly notable person, and return a structured \
JSON finding.

Rules:
1. Use web_search efficiently — 1 to 3 targeted queries are usually enough. \
   Combine the full name with nationality, industry hints, or the partner's \
   name when useful. Prefer authoritative sources: Wikipedia, LinkedIn, \
   company about-pages, major news outlets.
2. Be skeptical of common names. John Smith from the UK is not notable \
   without a specific match. Require:
   - At least ONE concrete, dated, verifiable piece of evidence (role title, \
     company, published work, team affiliation).
   - Nationality / context match (do not confuse two people sharing a name).
3. Confidence score:
   - 90–100: multiple reputable sources, context matches cleanly
   - 70–89: one strong source + context match
   - 50–69: plausible but ambiguous — DO NOT flag as notable
   - 0–49: common name / no evidence
4. `is_notable=true` only if confidence ≥ 70 AND the person has public \
   standing the hotel would want to know about. Don't flag private \
   professionals (accountants, local GPs) as notable.
5. `category` must be one of: CEO/Founder, Executive, Investor, Athlete, \
   Actor, Musician, Artist, Author, Journalist, Politician, Scientist, \
   Academic, Royalty, Influencer, Media Personality, Other. Null if not \
   notable.
6. `evidence_urls` — up to 3 links you actually used to reach the conclusion.
7. `reasoning` — one short sentence explaining what disambiguated the match \
   (or why you're not confident).
8. Return ONLY the JSON object. No preamble. No code fences. No trailing \
   explanation. Your response MUST start with `{` and end with `}`. Anything \
   else will be treated as a malformed response.

JSON schema (all fields required):
{
  "matched_name": string,
  "relationship": "self" | "partner" | "accompanying",
  "is_notable": boolean,
  "confidence": integer (0-100),
  "category": string | null,
  "summary": string | null,
  "evidence_urls": string[],
  "reasoning": string | null
}
"""


def _user_prompt(s: Subject) -> str:
    parts = [f"Subject: {s.display}"]
    parts.append(f"Relationship in booking: {s.relationship}")
    if s.nationality:
        parts.append(f"Nationality: {s.nationality}")
    if s.travel_agent:
        parts.append(f"Travel agent: {s.travel_agent}")
    if s.company:
        parts.append(f"Company on booking: {s.company}")
    if s.other_party_members:
        parts.append("Travelling with: " + ", ".join(s.other_party_members))
    parts.append("\nResearch whether this person is publicly notable. Return the JSON finding.")
    return "\n".join(parts)


# ─── Extraction ─────────────────────────────────────────────────────────────

_EXPECTED_KEYS = {"is_notable", "confidence", "matched_name", "category", "summary"}


def _parse_finding_text(text: str, subject: Subject) -> Optional[AListerFinding]:
    """Extract and validate the JSON object from Claude's final text block.

    Robust against:
      - markdown code fences (```json ... ```)
      - preamble prose before the JSON
      - trailing prose after the JSON
      - multiple `{` candidates (URLs, escaped chars, etc.)
    """
    if not text:
        return None
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*\n?", "", text)
        text = re.sub(r"\s*```\s*$", "", text)

    decoder = json.JSONDecoder()
    # Try decoding at every `{` until one yields a dict that looks like our schema.
    for i, c in enumerate(text):
        if c != "{":
            continue
        try:
            data, _ = decoder.raw_decode(text[i:])
        except json.JSONDecodeError:
            continue
        if not isinstance(data, dict):
            continue
        # Must look like our finding shape, not some incidental JSON literal
        if not _EXPECTED_KEYS & set(data.keys()):
            continue
        return _build_finding(data, subject)
    return None


def _build_finding(data: dict, subject: Subject) -> Optional[AListerFinding]:
    data.setdefault("matched_name", subject.display)
    data.setdefault("relationship", subject.relationship)
    data.setdefault("evidence_urls", [])
    data.setdefault("category", None)
    data.setdefault("summary", None)
    data.setdefault("reasoning", None)
    if isinstance(data.get("is_notable"), str):
        data["is_notable"] = data["is_notable"].strip().lower() in ("true", "yes")
    try:
        if isinstance(data.get("confidence"), str):
            data["confidence"] = int(float(data["confidence"]))
    except (TypeError, ValueError):
        data["confidence"] = 0
    # Coerce is_notable to bool if still not
    if not isinstance(data.get("is_notable"), bool):
        data["is_notable"] = False
    # Clamp confidence
    try:
        data["confidence"] = max(0, min(100, int(data.get("confidence", 0))))
    except (TypeError, ValueError):
        data["confidence"] = 0
    try:
        return AListerFinding(**data)
    except Exception:
        return None


async def _research_one(
    client: AsyncAnthropic,
    subject: Subject,
    model: str,
    semaphore: asyncio.Semaphore,
) -> tuple[Subject, Optional[AListerFinding], Optional[str]]:
    """Research one subject with retry on 529 overload."""
    async with semaphore:
        last_err: Optional[str] = None
        for attempt in range(4):  # 1 try + 3 retries
            try:
                response = await client.messages.create(
                    model=model,
                    max_tokens=4000,  # big enough to avoid mid-JSON truncation
                    system=[{
                        "type": "text",
                        "text": SYSTEM_PROMPT,
                        "cache_control": {"type": "ephemeral"},
                    }],
                    tools=[{"type": "web_search_20260209", "name": "web_search", "max_uses": 3}],
                    messages=[{"role": "user", "content": _user_prompt(subject)}],
                )
                text = ""
                for block in reversed(response.content):
                    if block.type == "text":
                        text = block.text
                        break
                finding = _parse_finding_text(text, subject)
                if finding is None:
                    return subject, None, f"could not parse JSON from final message: {text[:200]!r}"
                return subject, finding, None
            except APIStatusError as e:
                # 529 overload, 500/502/503 = transient; retry. 4xx = give up.
                status = getattr(e, "status_code", None)
                if status is not None and 500 <= status < 600:
                    last_err = f"APIStatusError {status}"
                else:
                    return subject, None, f"{type(e).__name__}: {e}"
            except Exception as e:
                return subject, None, f"{type(e).__name__}: {e}"
            # Exponential backoff: 2s, 5s, 12s
            await asyncio.sleep(2 * (2 ** attempt) + 1)
        return subject, None, last_err or "retries exhausted"


def _cache_key(subject: Subject) -> tuple[str, str, str | None]:
    """Normalised cache key: lowercase name parts, raw nationality."""
    first = (subject.first_name or "").strip().lower()
    last = (subject.last_name or "").strip().lower()
    nat = (subject.nationality or None)
    return first, last, nat


def _load_cache(subjects: list[Subject], ttl_days: int) -> dict[tuple[str, str, str | None], AListerFinding]:
    """Fetch any recent cached findings for the given subjects.

    Returns a dict keyed by (first_lower, last_lower, nationality) → finding.
    Subjects without a first+last name aren't cacheable (nothing to key on).
    """
    from supa import client  # lazy

    if not subjects:
        return {}

    sb = client()
    cutoff = (datetime.utcnow() - timedelta(days=ttl_days)).replace(microsecond=0).isoformat() + "Z"

    # We can't express a big OR cleanly in PostgREST, so fetch all rows newer
    # than the cutoff and filter client-side. Fine at this scale (thousands).
    res = (
        sb.table("researched_subjects")
        .select("*")
        .gte("researched_at", cutoff)
        .execute()
    )
    by_key: dict[tuple[str, str, str | None], AListerFinding] = {}
    for row in res.data or []:
        key = (
            (row.get("first_name") or "").strip().lower(),
            (row.get("last_name") or "").strip().lower(),
            row.get("nationality"),
        )
        try:
            by_key[key] = AListerFinding(
                matched_name=row.get("matched_name") or "",
                relationship=row.get("relationship") or "self",
                is_notable=bool(row.get("is_notable")),
                confidence=int(row.get("confidence") or 0),
                category=row.get("category"),
                summary=row.get("summary"),
                evidence_urls=row.get("evidence_urls") or [],
                reasoning=row.get("reasoning"),
            )
        except Exception:
            continue

    # Only keep entries relevant to the current subjects
    wanted = {_cache_key(s) for s in subjects}
    return {k: v for k, v in by_key.items() if k in wanted}


def _write_cache(subjects_and_findings: list[tuple[Subject, AListerFinding]]) -> int:
    """Upsert freshly-researched findings into the cache."""
    from supa import client
    if not subjects_and_findings:
        return 0
    sb = client()
    rows = []
    seen: set = set()
    for subj, f in subjects_and_findings:
        key = _cache_key(subj)
        if not key[0] and not key[1]:
            continue  # no name to cache on
        if key in seen:
            continue
        seen.add(key)
        rows.append({
            "first_name": key[0],
            "last_name": key[1],
            "nationality": key[2],
            "matched_name": f.matched_name,
            "relationship": f.relationship,
            "is_notable": f.is_notable,
            "confidence": f.confidence,
            "category": f.category,
            "summary": f.summary,
            "reasoning": f.reasoning,
            "evidence_urls": f.evidence_urls or [],
            "researched_at": datetime.utcnow().replace(microsecond=0).isoformat() + "Z",
        })
    if not rows:
        return 0
    # Upsert on the unique (first_name, last_name, nationality) constraint
    sb.table("researched_subjects").upsert(
        rows, on_conflict="first_name,last_name,nationality"
    ).execute()
    return len(rows)


async def research_subjects(
    subjects: list[Subject],
    *,
    model: str = DEFAULT_MODEL,
    concurrency: int = DEFAULT_CONCURRENCY,
    use_cache: bool = True,
    cache_ttl_days: int = DEFAULT_CACHE_TTL_DAYS,
) -> tuple[list[tuple[Subject, AListerFinding]], list[tuple[Subject, str]], dict]:
    """Research all subjects. If use_cache, skip anyone found in researched_subjects.

    Returns (findings, errors, stats) where stats includes cache hit counts.
    """
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise RuntimeError("ANTHROPIC_API_KEY not set in .env")

    findings: list[tuple[Subject, AListerFinding]] = []
    errors: list[tuple[Subject, str]] = []

    cache_hits: list[tuple[Subject, AListerFinding]] = []
    to_research = subjects
    if use_cache:
        try:
            cache = _load_cache(subjects, cache_ttl_days)
            for s in subjects:
                hit = cache.get(_cache_key(s))
                if hit is not None:
                    # Copy cached finding, overriding relationship to match THIS booking.
                    copy = hit.model_copy(update={"relationship": s.relationship})
                    cache_hits.append((s, copy))
            hit_keys = {_cache_key(s) for s, _ in cache_hits}
            to_research = [s for s in subjects if _cache_key(s) not in hit_keys]
        except Exception as e:
            print(f"  cache load failed (continuing without cache): {e}")

    findings.extend(cache_hits)

    if to_research:
        client = AsyncAnthropic(max_retries=5)
        semaphore = asyncio.Semaphore(concurrency)
        tasks = [_research_one(client, s, model, semaphore) for s in to_research]
        results = await asyncio.gather(*tasks)
        fresh: list[tuple[Subject, AListerFinding]] = []
        for subj, finding, err in results:
            if finding is not None:
                fresh.append((subj, finding))
            elif err is not None:
                errors.append((subj, err))
        findings.extend(fresh)

        if use_cache and fresh:
            try:
                written = _write_cache(fresh)
                print(f"  cache write: {written} rows upserted")
            except Exception as e:
                print(f"  cache write failed (findings still returned): {e}")

    stats = {
        "total_subjects": len(subjects),
        "cache_hits": len(cache_hits),
        "researched": len(to_research),
        "errors": len(errors),
        "flagged_any": sum(1 for _, f in findings if f.is_notable),
    }
    return findings, errors, stats


def persist_findings(
    subjects_and_findings: list[tuple[Subject, AListerFinding]],
    report_date: date,
) -> int:
    """Write all findings (notable or not) to alister_findings in Supabase.

    Keeps an audit trail of who was researched — GDPR-friendly and lets
    Guest Relations see why someone was NOT flagged.
    """
    from supa import client  # lazy import so the CLI works offline too

    sb = client()

    # Resolve the upload_id for this report_date (latest upload wins).
    up = (
        sb.table("uploads")
        .select("id,uploaded_at")
        .eq("report_date", report_date.isoformat())
        .order("uploaded_at", desc=True)
        .limit(1)
        .execute()
    )
    if not up.data:
        raise RuntimeError(f"No upload found for report_date={report_date}")
    upload_id = up.data[0]["id"]

    # Map resv_name_id → reservations.id (uuid) for this upload.
    # Page through in case >1000 rows (Supabase default cap is 1000).
    id_map: dict[int, str] = {}
    offset = 0
    page = 1000
    while True:
        res = (
            sb.table("reservations")
            .select("id,resv_name_id")
            .eq("upload_id", upload_id)
            .range(offset, offset + page - 1)
            .execute()
        )
        if not res.data:
            break
        for r in res.data:
            if r.get("resv_name_id"):
                id_map[r["resv_name_id"]] = r["id"]
        if len(res.data) < page:
            break
        offset += page

    rows = []
    skipped_no_match = 0
    for subj, f in subjects_and_findings:
        res_id = id_map.get(subj.resv_name_id) if subj.resv_name_id else None
        if not res_id:
            skipped_no_match += 1
            continue
        rows.append({
            "reservation_id": res_id,
            "matched_name": f.matched_name,
            "relationship": f.relationship,
            "is_notable": f.is_notable,
            "confidence": f.confidence,
            "category": f.category,
            "summary": f.summary,
            "evidence_urls": f.evidence_urls or [],
        })

    if not rows:
        print(f"  persist: 0 rows (skipped {skipped_no_match} with no reservation match)")
        return 0

    # Batch insert
    inserted = 0
    for i in range(0, len(rows), 500):
        batch = rows[i : i + 500]
        sb.table("alister_findings").insert(batch).execute()
        inserted += len(batch)
    print(f"  persist: {inserted} rows inserted ({skipped_no_match} skipped)")
    return inserted


# ─── CLI ────────────────────────────────────────────────────────────────────

def _parse_date(s: str) -> date:
    return datetime.strptime(s, "%Y-%m-%d").date()


def _should_research(r: dict, start: date, end: date) -> bool:
    arrival = r.get("arrival")
    if isinstance(arrival, datetime):
        arrival = arrival.date()
    if not isinstance(arrival, date):
        return False
    return start <= arrival <= end


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("xlsx")
    ap.add_argument("--date", required=True, type=_parse_date)
    ap.add_argument("--window-days", type=int, default=0,
                    help="0 = only the report date; N>0 = report_date..+N")
    ap.add_argument("--scope", choices=("special", "all"), default="special",
                    help="'special' = only special-attention arrivals (safer); 'all' = every arrival")
    ap.add_argument("--threshold", type=int, default=DEFAULT_CONFIDENCE_THRESHOLD,
                    help=f"Confidence threshold to surface (default {DEFAULT_CONFIDENCE_THRESHOLD})")
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--concurrency", type=int, default=DEFAULT_CONCURRENCY)
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--persist", action="store_true",
                    help="Write all findings to Supabase alister_findings")
    ap.add_argument("--no-cache", action="store_true",
                    help="Ignore the researched_subjects cache; re-research everyone")
    ap.add_argument("--cache-ttl-days", type=int, default=DEFAULT_CACHE_TTL_DAYS)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    records = parse_file(args.xlsx)
    end = args.date + timedelta(days=args.window_days)
    arrivals = [r for r in records if _should_research(r, args.date, end)]

    if args.scope == "special":
        arrivals = [r for r in arrivals if special_attention_reason(r)]

    subjects: list[Subject] = []
    for r in arrivals:
        subjects.extend(subjects_from_reservation(r))

    if args.limit:
        subjects = subjects[: args.limit]

    if not subjects:
        print("No subjects to research. Exiting.")
        return 0

    print(f"Researching {len(subjects)} subjects from {len(arrivals)} arrivals "
          f"(scope={args.scope}, dates={args.date}..{end}, model={args.model}, "
          f"concurrency={args.concurrency}, cache={'off' if args.no_cache else f'{args.cache_ttl_days}d TTL'})")

    findings, errors, stats = asyncio.run(
        research_subjects(
            subjects,
            model=args.model,
            concurrency=args.concurrency,
            use_cache=not args.no_cache,
            cache_ttl_days=args.cache_ttl_days,
        )
    )

    flagged = [(s, f) for s, f in findings if f.is_notable and f.confidence >= args.threshold]
    print(f"\nResult: {stats['cache_hits']} cache hits, {stats['researched']} newly researched, "
          f"{stats['errors']} errors, {len(flagged)} FLAGGED at ≥{args.threshold}")

    if flagged:
        print("\n── A-LISTER FINDINGS ──")
        for s, f in sorted(flagged, key=lambda sf: -sf[1].confidence):
            print(f"\n  Room {s.room or '-'} — {f.matched_name} ({f.relationship}, conf {f.confidence})")
            print(f"    Category: {f.category}")
            print(f"    {f.summary}")
            if f.reasoning:
                print(f"    (why: {f.reasoning})")
            for url in f.evidence_urls[:3]:
                print(f"    → {url}")

    if errors:
        print(f"\n── ERRORS ({len(errors)}) ──")
        for s, err in errors[:10]:
            print(f"  {s.display}: {err}")

    if args.persist:
        print("\n── PERSISTING TO SUPABASE ──")
        persist_findings(findings, args.date)

    if args.json:
        out = [{"subject": asdict(s), "finding": f.model_dump()} for s, f in findings]
        print("\n── JSON ──")
        print(json.dumps(out, indent=2, ensure_ascii=False))

    return 0


if __name__ == "__main__":
    sys.exit(main())
