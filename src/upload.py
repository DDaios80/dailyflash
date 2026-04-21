"""Upload a parsed Opera export into Supabase, optionally running Phase 2 extraction.

Flow:
  1. Create a row in `uploads`.
  2. Parse the xlsx → normalized records (ingest.py).
  3. Bulk insert into `reservations` in batches of 500.
  4. If --extract, run LLM pass over COMMENTS and insert into `comment_extractions`.

Re-running for the same filename + report_date replaces the prior upload
(deletes cascade through reservations → comment_extractions → alister_findings).

Usage:
    python src/upload.py samples/Daily\\ Flash\\ 20.04.2026.xlsx --date 2026-04-20
    python src/upload.py samples/Daily\\ Flash\\ 20.04.2026.xlsx --date 2026-04-20 --extract
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable

from ingest import parse_file, summarize
from supa import client


BATCH_SIZE = 500


def _jsonable(v: Any) -> Any:
    if isinstance(v, (datetime, date)):
        return v.isoformat()
    return v


def _record_to_row(upload_id: str, rec: dict) -> dict:
    row: dict = {"upload_id": upload_id}
    for k, v in rec.items():
        row["raw" if k == "raw" else k] = v if k == "raw" else _jsonable(v)
    return row


def _batches(xs: list, n: int) -> Iterable[list]:
    for i in range(0, len(xs), n):
        yield xs[i : i + n]


def _replace_existing_upload(sb, filename: str, report_date: date) -> None:
    """If an upload for the same filename + report_date exists, delete it (cascades)."""
    res = (
        sb.table("uploads")
        .select("id")
        .eq("filename", filename)
        .eq("report_date", report_date.isoformat())
        .execute()
    )
    for row in res.data or []:
        sb.table("uploads").delete().eq("id", row["id"]).execute()
        print(f"  replaced existing upload {row['id']} (cascade delete)")


def upload_file(
    xlsx_path,
    report_date: date,
    *,
    run_extract: bool = False,
    window_days: int = 7,
    dry_run: bool = False,
) -> str | None:
    path = Path(xlsx_path)
    records = parse_file(path)
    summary = summarize(records)
    print(f"Parsed {summary['total_real_reservations']} reservations from {path.name}")
    print(f"  by status: {summary['by_status']}")

    if dry_run:
        print("[dry-run] skipping Supabase insert")
        return None

    sb = client()
    _replace_existing_upload(sb, path.name, report_date)

    # 1. Create upload row
    res = sb.table("uploads").insert({
        "filename": path.name,
        "report_date": report_date.isoformat(),
        "row_count": len(records),
    }).execute()
    upload_id = res.data[0]["id"]
    print(f"Created uploads.id = {upload_id}")

    # 2. Insert reservations
    rows = [_record_to_row(upload_id, r) for r in records]
    inserted = 0
    resv_id_by_resv_name_id: dict[int, str] = {}
    for i, batch in enumerate(_batches(rows, BATCH_SIZE), start=1):
        res = sb.table("reservations").insert(batch).execute()
        for row in res.data or []:
            if row.get("resv_name_id"):
                resv_id_by_resv_name_id[row["resv_name_id"]] = row["id"]
        inserted += len(batch)
        print(f"  reservations batch {i}: inserted {len(batch)} ({inserted}/{len(rows)})")
    print(f"  reservations: {inserted} total")

    # 3. Optional Phase 2 extraction + persistence
    if run_extract:
        from extract import extract_batch, _should_extract  # lazy import

        end = report_date + timedelta(days=window_days)
        in_scope = [r for r in records if _should_extract(r, report_date, end)]
        print(f"Running extraction on {len(in_scope)} in-scope reservations...")
        extractions, errors, es = asyncio.run(extract_batch(in_scope))
        print(f"  extraction: {es['succeeded']}/{es['total']} ok, {es['failed']} failed")

        ext_rows = []
        for resv_name_id, ext in extractions.items():
            res_id = resv_id_by_resv_name_id.get(resv_name_id)
            if not res_id:
                continue
            data = ext.model_dump()
            ext_rows.append({
                "reservation_id": res_id,
                "allergies_present": data.get("allergies_present", False),
                "allergies_text": data.get("allergies_text"),
                "pool_fence": data.get("pool_fence", False),
                "pool_heating": data.get("pool_heating", False),
                "free_transfer": data.get("free_transfer", False),
                "free_upgrade": data.get("free_upgrade", False),
                "lco": data.get("late_checkout", False),
                "honeymoon": data.get("honeymoon", False),
                "amenities": data.get("amenities") or [],
                "ops_notes": data.get("ops_summary") or data.get("payment_notes"),
            })

        if ext_rows:
            for i, batch in enumerate(_batches(ext_rows, BATCH_SIZE), start=1):
                sb.table("comment_extractions").insert(batch).execute()
                print(f"  comment_extractions batch {i}: inserted {len(batch)}")
            print(f"  comment_extractions: {len(ext_rows)} total")

    print(f"\nDone. upload_id = {upload_id}")
    return upload_id


def _parse_date(s: str) -> date:
    return datetime.strptime(s, "%Y-%m-%d").date()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("xlsx")
    ap.add_argument("--date", required=True, type=_parse_date)
    ap.add_argument("--extract", action="store_true",
                    help="Also run Phase 2 extraction and persist comment_extractions")
    ap.add_argument("--window-days", type=int, default=7)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    try:
        upload_file(
            args.xlsx,
            args.date,
            run_extract=args.extract,
            window_days=args.window_days,
            dry_run=args.dry_run,
        )
    except Exception as e:
        print(f"upload failed: {type(e).__name__}: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
