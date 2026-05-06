# Lovable handoff — Phase 43 + 44 edge function work

Two bugs the Python repo can't reach. Both live in Supabase edge functions
on Lovable's side.

## Phase 43 — Post-submit email send hook is broken

**Symptom:** Every FAM trip and site inspection sits at `sent_at = NULL`
after submission AND after approval. The approval email never reaches
the approver (Thelxi). Confirmed cases:

- `Sunday Natural FAM` (id `2eb76808-...`) — approved 2026-05-03 by
  d.daios, never sent
- `Beleon FAM Trip` (id `32247a6f-...`) — approved 2026-05-03 by d.daios,
  never sent
- `SO HOSPITALITY` site inspection (id `880e10b0-...`) — approved
  2026-05-03 by d.daios, never sent
- 3 new FAM trips imported tonight (`NOW YOGA`, `ABERCROMBIE & KENT`,
  `FAM NAUTIL`) — `pending_approval` status, has token, but
  `sent_at = NULL`

**Where to look:** the edge function `ingest-fam-trip-from-onedrive`
(and any `submit_for_approval`/`admin_review` RPC). After a row is
inserted with `status = pending_approval` (or transitions from
`pending_approval` → `approved`), an email should fire to the relevant
party. Right now nothing fires.

**Acceptance criteria:**
- Importing a new FAM trip via `python src/fam_trip_sync.py` results in
  an approval email landing in `approver_user_id`'s inbox within 60 s.
- Approving a trip via the dashboard fires a "FAM trip confirmed" email
  to the operations team / agency contact.

Worth checking the Resend logs (or whichever provider is wired up) for
errors during the May 3rd 21:30 window — three submissions within 3 s
all failed silently.

## Phase 44 — Site inspection OneDrive sync edge function

**Symptom:** Thelxi drops PDFs into `DailyFlash/SITE INSPECTIONS/`
expecting auto-import like FAM trips. Nothing happens because there's
no edge function for site inspections.

**What's already done in the Python repo (commit pushed tonight):**

- `src/onedrive.py` — `list_site_inspection_pdfs()` lists the folder
- `src/site_inspection_sync.py` — module mirrors `fam_trip_sync.py`,
  parses filename → `(travel_agency, inspection_date)`, POSTs to
  `INGEST_SITE_INSPECTION_URL` env var
- `src/cron.py` — wired into the nightly pipeline alongside fam-sync

**What Lovable needs to add:**

A new edge function `ingest-site-inspection-from-onedrive` that mirrors
`ingest-fam-trip-from-onedrive`:

- POST body shape:
  ```json
  {
    "pdf_filename": "INSPECTION VISIT - EASYJET - 29.04.26.pdf",
    "pdf_base64": "...",
    "pdf_size_bytes": 12345,
    "travel_agency": "EASYJET",
    "inspection_date": "2026-04-29",
    "onedrive_item_id": "...",
    "onedrive_etag": "...",
    "created_by_user_id": "<admin uuid>"
  }
  ```
- Auth: `Bearer ${PIPELINE_SECRET}`
- Behaviour:
  1. Dedup by `onedrive_item_id` (or `attachment_path` / filename)
  2. Upload PDF to a `site-inspection-pdfs` storage bucket (mirror the
     fam-trip-pdfs bucket policies — see `db/phase34_2_fam_trip_pdfs_insert_policy.sql`)
  3. INSERT into `site_inspections` with status `pending_approval`,
     approval_token, submitted_at, approver default `d.daios`
     (Phase 45 trigger will swap this to Thelxi automatically — applied
     tonight via `db/phase45_approver_default_thelxi.sql`)
  4. Trigger Phase 43 email send hook
- Response shape:
  ```json
  {"inspection_id": "...", "skipped": false}
  ```
  or `{"skipped": true}` for already-imported.

Once the edge function exists, set `INGEST_SITE_INSPECTION_URL` on
Railway (both `dailyflash` and `fortunate-mindfulness` services) and the
nightly cron starts importing.

**Acceptance criteria:**
- Drop a PDF into `DailyFlash/SITE INSPECTIONS/` named like
  `INSPECTION VISIT - SOME AGENCY - 06.05.2026.pdf`
- Run `python src/site_inspection_sync.py`
- A `site_inspections` row appears with that travel_agency + date,
  status=pending_approval, approver=Thelxi, approval email sent

## Note on .msg/.eml files

The site inspections folder contains 4 `.msg`/`.eml` Outlook exports.
These are **out of scope** for the auto-sync — Python's filter only
considers PDFs. If Thelxi is uploading inspections as `.msg` files
expecting auto-import, that's a workflow conversation: either she
exports to PDF, or we add email-parsing logic later (Phase 46+).
