# docs/ INDEX

Navigation file for the `docs/` folder. Created 2026-05-14 during the strategic-review session (see Notion shipping log, 14 May entry).

The folder accumulated 22 files of mixed purpose over the spring: audit docs, Lovable handoff prompts, team conventions, and snapshots of diagnostic work. This INDEX groups them so future-you (or a second developer) can navigate without reading each filename.

**Rule of thumb when adding new docs:**

| Doc type | Naming pattern | Notes |
|---|---|---|
| **Audit / structural review** | `YYYY-MM-DD-{topic}.md` | Date-prefixed. Stays in `docs/` permanently as reference. |
| **Lovable handoff (active)** | `lovable-handoff-YYYY-MM-DD-{topic}.md` or `lovable-prompt-{topic}.md` | Pre-ship. Move to `docs/archive/` once the corresponding phase ships and is verified. |
| **Team operational convention** | `team-note-{topic}.md` | Stays in `docs/` permanently. |
| **Notion fill-in (transient)** | `notion-fill-in-{topic}.md` | Delete from repo once content is applied to the shipping log. (Today's lesson — these accumulated during the weekend Notion write outage.) |

---

## Active reference (read these first)

| File | Purpose | Status |
|---|---|---|
| `2026-05-14-update-statement-audit.md` | UPDATE-statement audit identifying 7 confirmed half-finished-state-machine bugs across `idea_*`, `admin_review_*`, `mark_*_sent` RPCs. Prioritized fix list. **P1 (reject paths) was shipped as Phase 64; P2 (ideas `_by` columns) and P3 (atomic-transition convention enforcement) are open recommendations.** | ⭐ Active reference |
| `2026-05-08-postgrest-cache-eradication.md` | Root-cause analysis of the PostgREST PGRST205 schema cache problem. Documents the Phase 51 convention: **grant to all 4 PostgREST roles (authenticator, anon, authenticated, service_role).** This convention is now enforced by Phase 63 across all public functions. | ⭐ Active reference (historical analysis, conventions live) |
| `team-note-inspection-upload-convention.md` | Operational convention for OneDrive site-inspection PDF filenames. Reference for the team when uploading. | ⭐ Active reference (team-facing) |

---

## Shipped Lovable handoffs (historical, kept for context)

These were prompts written for Lovable that resulted in shipped code. Kept in the repo as a record of what was asked + how it shipped. Could be moved to `docs/archive/` if you want to declutter.

### Phase 49-58 era (early May)

| File | Phase / Shipped as |
|---|---|
| `lovable-handoff-2026-05-06.md` | Phase 46 — `approve-fam-trip` / `approve-site-inspection` edge functions |
| `lovable-handoff-2026-05-09-cron-escalation.md` | Phase 55 — **ABANDONED** (per shipping log). Empty Railway service `dailyflash-escalation` remains in `helpful-patience` project as dead infra; CLI can't delete services. |
| `lovable-handoff-2026-05-09-committee-email-from.md` | Phase 57 — `committee@daioscove.com` From-line. Domain verified 2026-05-14. |
| `lovable-handoff-2026-05-09-morning-briefing-ui.md` | Phase 54 — admin-managed morning briefing recipients list |
| `lovable-handoff-2026-05-09-ideas-inbound-handler.md` | Phase 56 — atomic `acknowledged_at` on `committee_update_idea` RPC |
| `lovable-handoff-2026-05-09-zoho-priority-strip.md` | Phase 59 — DB trigger to strip Zoho housekeeping priority explanations |

### Phase 60.x era (pool operations redesign)

| File | Phase / Shipped as |
|---|---|
| `lovable-handoff-2026-05-09-pool-heating-redesign.md` | Phase 60 — 47-room pool heating grid + 14-day Gantt |
| `lovable-prompt-extraction-quality-and-override.md` | Phase 60.5 + 60.6 — in-house re-extraction + strengthened LLM prompt |
| `lovable-prompt-ingest-flash-report-diagnostic.md` | Phase 60.x — diagnostic for the silent `DO NOTHING` upsert bug |
| `lovable-prompt-ingest-flash-report-extraction-fix.md` | Phase 60.x — fix for the upsert bug (Lovable shipped `DO UPDATE`) |
| `lovable-prompt-ingest-flash-report-reservations-fix.md` | Phase 52 — preserve `comment_extractions` across upload replacement |
| `lovable-prompt-pdf-pool-heating-fix.md` | Phase 60.8 (first round) — PDF reads from `pool_heating_grid` |
| `lovable-prompt-pdf-email-diagnostic.md` | Phase 60.8 — diagnostic verifying the PDF/email field swap |
| `lovable-prompt-send-flash-email-regression.md` | Phase 60.8 — 50-recipient Resend chunking bug fix |
| `lovable-prompt-ideas-cc-chair.md` | 9 May — CC chair on idea response emails |

### Phase 61-62 era (pool/cribs panels)

| File | Phase / Shipped as |
|---|---|
| `lovable-handoff-pool-cleaning.md` | Phase 61 — pool cleaning forecast (137-room inventory) |
| `lovable-prompt-pool-cleaning-grid-calendar.md` | Phase 61.1 — pool cleaning grid + calendar (mirrors heating layout) |
| `lovable-prompt-pool-fence-grid-calendar.md` | Phase 61.2 — pool fence grid + calendar (137-button) |
| `lovable-prompt-cribs-grid-calendar.md` | Phase 62 — cribs grid + calendar + 7-day forecast |

---

## Notion fill-in docs (lesson: don't queue these in the repo)

These existed as `notion-fill-in-*.md` during the 2026-05-09 → 2026-05-13 weekend Notion write outage. They held shipping-log content waiting for the Notion API to recover. **Deleted from the repo on 2026-05-14 (commit `c9e3b6e`)** after the content landed in Notion.

Pattern for future Notion outages: try the write first. If MCP returns `RequestTimeoutError`, **verify with search before retrying** — the write usually committed server-side anyway. Only queue content in the repo if multiple retries genuinely fail.

---

## Archive (does not currently exist)

Suggested future structure: `docs/archive/{year}/` for shipped Lovable handoffs that are >3 months old. Keeps active reference docs scannable.

Not implemented yet (the 22 current files are all from April-May 2026, recent enough to leave inline). When the volume gets uncomfortable, move shipped handoffs >90 days old.

---

## Cross-references

- **Shipping log** (chronological narrative): https://www.notion.so/34cfd0735bf5814bb425daac35b0bf81
- **Repo root README**: `../README.md` (system architecture + data flows + runbook)
- **Migration files**: `../db/phase*.sql`
- **Edge function repo drift**: `../edge-functions/` is stale vs Lovable Cloud (see README's "Edge functions" section)
