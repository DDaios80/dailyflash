# Site Inspection Upload — Filename Convention

The Daily Flash dashboard auto-ingests site inspections from OneDrive,
but only files matching a specific naming pattern. Files that don't
match are silently skipped and never reach the dashboard.

## What to upload

**Location:** `OneDrive → Daios Cove Crete → DailyFlash → SITE INSPECTIONS`

**File type:** PDF only. `.msg` and `.eml` Outlook exports are NOT
ingested — the system can't read email attachments.

**Filename pattern:** `INSPECTION VISIT - <AGENCY> - DD.MM.YY.pdf`

## Examples that work

```
INSPECTION VISIT - EASYJET - 29.04.26.pdf
INSPECTION VISIT - SO.HOSPITALITY - 02.05.2026.pdf
INSPECTION VISIT - TUI GROUP - 15.06.26.pdf
```

Both 2-digit (`26`) and 4-digit (`2026`) years work. Extra spaces are
tolerated. Agency names with dots, spaces, or hyphens are fine.

## Examples that DON'T work (currently sitting unprocessed)

```
SITE INSPECTION - Mosaic Tourism Consulting.msg     ← .msg, not PDF
SITE INSPECTION - VINTAGE CREATIVE_BELEON.msg       ← .msg, not PDF
MAY 11__SITE INSPECTION @10_00.msg                  ← .msg, not PDF
SITE INSPECTION - KIRSTIN BAUGUT .eml               ← .eml, not PDF
```

These files are in OneDrive but the dashboard will never show them
because the sync can't parse them.

## How to convert an Outlook email to the correct format

1. Open the email in Outlook.
2. **File → Save As → PDF** (or **File → Print → Save as PDF**).
3. Rename the resulting PDF to the pattern:
   `INSPECTION VISIT - <AGENCY> - DD.MM.YY.pdf`
   (use the agency name from the email and the inspection date, not
   today's date).
4. Drag the PDF into the `SITE INSPECTIONS` OneDrive folder.
5. Within ~30 minutes, the inspection appears on the dashboard and the
   approver (Thelxi by default) gets an approval email.

## How to backfill the unprocessed ones

For each `.msg` file currently in the folder that represents a real
upcoming inspection:

1. Open the `.msg` in Outlook.
2. Save as PDF.
3. Rename to the convention above.
4. Save into the same folder.
5. (Optional) Delete the original `.msg` to keep the folder tidy — the
   sync ignores it either way.

The system de-duplicates by filename, so re-running won't create
duplicates if you re-upload the same name.

## Why this matters

Site inspections that don't make it into the dashboard:
- Don't get auto-distributed to the inspection recipient list
- Don't appear in the morning briefing or daily flash email
- Don't have an approval audit trail

Following the naming convention is the difference between an inspection
the whole team is prepared for vs. one that surprises front office on
the day.

---

*Reference: the auto-ingest pipeline is documented in
`src/site_inspection_sync.py`. Edit the regex there if the team's
naming workflow legitimately changes in the future.*
