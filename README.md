# Daios Cove Daily Flash

Operational platform for Daios Cove resort management. Ingests reservations + guest data + ops signals from multiple sources, produces a daily flash report (PDF + email at 08:00 Athens) for the ExCo + extended team, and provides a React dashboard for ongoing operations: ideas + approvals + pool operations + Q&A logs + super-admin observability.

This README captures the system as of **2026-05-14** (Phase 67.1). For day-by-day evolution, see the [shipping log on Notion](https://www.notion.so/34cfd0735bf5814bb425daac35b0bf81).

---

## System overview

```
                  ┌─────────────────────────────────────────────┐
                  │                                             │
   Opera PMS ─────┤  OneDrive (xlsx) ──▶ daily.py (Python cron) │
                  │                              │              │
   Zoho ──────────┤  zoho ingest cron ──────▶    ▼              │
                  │                       Supabase Postgres     │
   Lovable UI ────┤  edge functions ──────────▶  │              │
                  │                              ▼              │
                  │                       Resend (email)        │
                  │                       Chrome headless (PDF) │
                  │                                             │
                  └─────────────────────────────────────────────┘
```

**Compute lives in three places:**

| Component | Tech | Hosting | Purpose |
|---|---|---|---|
| **Cron pipeline** | Python | Railway (`helpful-patience` project, `dailyflash` service) | Daily flash assembly, AI extraction, A-lister research, OneDrive + Zoho ingest, freshness checks |
| **Edge functions** | TypeScript / Deno | Lovable Cloud (Supabase) | Approval flows, email send, PDF generation, idea response RPCs, on-demand ingest |
| **Frontend** | React (Lovable) | `flashreport.daioscove.com` | Dashboard, idea submission + thread, approval review, pool operations, super-admin |

**Data lives in:**

- **Supabase Postgres** (`Sales Explorer` project): 50+ tables, extensive RLS, ~115+ SECURITY DEFINER RPCs.
- **Supabase Storage**: 7 buckets (`idea-photos`, `fam-trip-pdfs`, `site-inspection-pdfs`, `email-assets`, `inspection-attachments`, `exco-transcripts`, `exco-member-context`).
- **Notion**: shipping log + handoff documents.
- **OneDrive (corporate)**: source xlsx files + FAM trip + inspection PDFs.

---

## Repository layout

```
daily-flash/
├── src/                     # Python cron pipeline (deployed to Railway)
│   ├── cron.py              # Entry point — runs daily.py with date logic + Phase 47 deploy-skip
│   ├── daily.py             # The main pipeline: xlsx → envelope → flash_reports row
│   ├── extract.py           # Per-comment AI extraction via Claude (allergies, pool flags, etc.)
│   ├── compute.py           # Deterministic business logic (occupancy, special attention)
│   ├── bridge.py            # Postgres write layer (flash_reports, reservations, etc.)
│   ├── heatable_rooms.py    # Phase 60: 47-room pool heating registry + rules
│   ├── pool_rooms.py        # Phase 61: 137-room private pool registry (cleaning + fence)
│   ├── fam_trip_sync.py     # OneDrive → fam_trips ingest
│   ├── site_inspection_sync.py  # OneDrive → site_inspections ingest
│   ├── email_dispatcher.py  # Pre-email validation + chunking helpers
│   ├── heartbeat.py         # Phase 68: uptime pinger (see Monitoring section)
│   └── ...                  # Other helpers (upload.py, alister.py, supa.py, etc.)
├── db/                      # Supabase migrations (phase-numbered SQL files)
│   ├── schema.sql           # Initial Phase 1 schema
│   ├── phaseN_*.sql         # Each phase = one migration applied via Supabase SQL editor
│   └── ...                  # Currently 50+ phase files
├── edge-functions.archive-2026-05-14/  # 📦 ARCHIVED stale copies (pre-Phase 60.8).
│                            # Source of truth = Lovable Cloud. See "Edge functions" section.
├── lovable-edge-functions/  # Lovable-managed newer edge functions (approve-fam-trip, approve-site-inspection, ingest-site-inspection-from-onedrive)
├── docs/                    # Audit docs + Lovable handoff prompts + handover notes
├── ops/                     # Operational scripts (manual triggers, debug helpers)
├── tools/                   # One-off utility scripts
├── samples/                 # Reference xlsx files for local testing
├── railway.toml             # Railway deploy config (cron schedule + start command)
├── Dockerfile               # Python pipeline container
├── requirements.txt         # Python deps
└── README.md                # This file
```

---

## Data flows

### Daily flash generation (the critical path)

```
22:00 Athens (19:30 UTC)
   │
   ▼
Railway cron fires → src/cron.py
   │
   ├─ Phase 47 check: did Railway just redeploy? If so, skip (next scheduled fire runs)
   ├─ Find xlsx in OneDrive sync folder for tomorrow's date
   ├─ src/daily.py: parse + extract + compute + assemble envelope
   │   ├─ AI per-comment extraction (Claude) for allergies, pool flags, etc.
   │   ├─ A-lister research (Firecrawl) for repeater guests
   │   ├─ Compute pool heating grid (Phase 60, 47 rooms)
   │   ├─ Compute pool cleaning + fence grids (Phase 61, 137 rooms)
   │   ├─ Compute cribs grid (Phase 62, dynamic ~90 rooms)
   │   └─ Soft-deprecated: legacy pool_heating[] field (Phase 60 follow-up, soaking until 2026-05-21)
   ├─ Write flash_reports row to Supabase
   ├─ Trigger edge function: generate-flash-pdf
   ├─ Phase 60.3 freshness sanity check on comment_extractions
   └─ Phase 68 heartbeat ping to BetterUptime
   │
   ▼
08:00 Athens — send-executive-briefing-email (Lovable edge function, scheduled)
   │
   ├─ Fetch latest flash_reports row
   ├─ Fetch recipients from app_settings.morning_briefing_recipients (~64)
   ├─ Chunk into ≤49-BCC batches (Resend 50-recipient limit)
   └─ Send via Resend API from flash@daioscove.com
```

### Idea response thread (Phase 65/65.1/66/67/67.1)

```
User submits idea via /ideas/new
   │
   ├─ submit_idea() RPC inserts into `ideas` table
   ├─ Phase 66 trigger: auto-set assigned_to_user_id = current_committee_chair_id()
   │
   ▼
Idea thread renders on /ideas/{id}
   │
   ├─ Display: COMMITTEE RESPONSES (newest first)
   │           Original committee_response field backfilled as first entry
   │
   ▼
Chair / assignee adds response → idea_add_response() RPC
   │
   ├─ Phase 67: author_user_id = ideas.assigned_to_user_id (sticky, not auth.uid())
   ├─ submitted_by_user_id = auth.uid() (audit trail)
   ├─ Refuses if status doesn't change OR idea has no assignee
   ├─ Inserts into idea_responses table
   └─ Updates ideas.status + committee_response (latest pointer)
   │
   ▼
Edit response → idea_response_edit() RPC (Phase 65.1)
   │
   ├─ Gated by author OR submitter OR super_admin
   ├─ Updates response_text, bumps revision_count, sets updated_at
   └─ Frontend shows "(edited Xm ago · revised N times)"
```

### Approval flows (FAM trips, site inspections, groups)

```
External party → OneDrive PDF drop OR direct UI submission
   │
   ▼
src/{fam_trip,site_inspection}_sync.py ingests the file
   │
   ├─ Phase 14.1: AI parses itinerary
   ├─ submit_*_for_approval() RPC sets status='pending_approval'
   └─ Sends approval email to assigned approver
   │
   ▼
Approver clicks email link OR uses admin in-app review
   │
   ├─ admin_review_{fam_trip,inspection,group}() RPC (Phase 32, Phase 34)
   ├─ Approve path: status='approved', approved_at, approver_user_id (atomic)
   ├─ Reject path: status='rejected', rejected_at, rejected_by_user_id, rejection_reason (atomic — Phase 64 fix)
   └─ On approve: triggers send-to-recipients edge function
```

---

## Deployment

### Auto-deploy (the happy path)

GitHub `DDaios80/dailyflash` `main` branch is connected to Railway's `dailyflash` service in the `helpful-patience` project. Every push to `main` triggers an auto-deploy.

**Verified working** as of 2026-05-14. The May 6 → May 13 silent deploy gap saga is closed (auto-deploy chain healthy).

### Manual deploy fallback

```bash
cd ~/daily-flash
railway up    # uploads current working directory as a one-shot deploy
```

⚠️ `railway up` does NOT disconnect the GitHub source. After a manual deploy, subsequent pushes to `main` still auto-deploy normally.

### Database migrations

**Manual, via Supabase SQL editor.** No migration runner exists. Workflow:

1. Write migration as `db/phaseN_*.sql`
2. Commit + push to `main` (gets versioned in git)
3. Copy file contents (`cat db/phaseN_*.sql | pbcopy`)
4. Open Supabase SQL editor (Sales Explorer project) → new query → ⌘V → Run
5. Verify output matches the migration's verification SELECT

**Convention** (Phase 51 onwards): every new function/view migration must grant to all four PostgREST-relevant roles:

```sql
grant execute on function my_fn(...) to authenticator, anon, authenticated, service_role;
```

See Phase 63 audit and `docs/2026-05-14-update-statement-audit.md` for the structural conventions.

### Edge functions

**Source of truth: Lovable Cloud.** Do not treat any folder in this repo as canonical edge function code.

Repository layout for edge functions (as of 2026-05-14 cleanup):

- **`edge-functions.archive-2026-05-14/`** — Archived stale snapshots of 19 edge functions, captured pre-Phase 60.8. Preserved in git history for reference (e.g., to compare against old PDF/email rendering) but NOT current. If you need the live code, look in Lovable Cloud, not here.
- **`lovable-edge-functions/`** — Three newer functions that Lovable's tooling mirrored here at some point (`approve-fam-trip`, `approve-site-inspection`, `ingest-site-inspection-from-onedrive`). These may also be stale relative to Lovable Cloud — treat as reference, not source of truth.

**To modify an edge function**: do it via Lovable's chat UI. Lovable deploys directly to Lovable Cloud. There is no automatic mirror back to this repo.

**To verify deployed code** (per the verification protocol after Lovable's false-claim pattern):
1. Ask Lovable to paste the deployed function body in chat
2. Or query Supabase: `select prosrc from pg_proc where proname = 'function_name';` (for Postgres functions)
3. Don't rely on the repo's archive folder for current behavior

**Future improvement**: build a periodic sync script that downloads current Lovable Cloud edge function code into a `edge-functions.lovable-sync/` folder. Not done yet (Tier 2 was an archive decision, not a sync investment).

---

## Monitoring

### Uptime heartbeat (Phase 68)

After every successful cron run, `src/cron.py` calls `ping_heartbeat()` (see `src/heartbeat.py`). This sends an HTTP GET to a monitoring service. If no ping arrives within the configured grace period (e.g., 26h for a daily cron), the service alerts the operator.

**Setup steps:**

1. Sign up for free monitoring service (BetterUptime / Better Stack / Healthchecks.io recommended)
2. Create a heartbeat monitor:
   - **Name**: `Daily flash cron`
   - **Type**: Heartbeat / Dead-man's switch
   - **Grace period**: 26 hours (cron runs daily, so up to 26h between pings = OK)
   - **Alert channels**: email, SMS, Slack, whatever you prefer
3. Copy the heartbeat URL the service provides (looks like `https://heartbeat.betterstack.com/api/v1/heartbeat/<token>`)
4. Set Railway env var: `HEARTBEAT_URL=<the URL>`
5. Trigger a deploy (or wait for next scheduled run)
6. Verify in Railway logs: `[heartbeat] ping OK (daily)` after the cron completes

If `HEARTBEAT_URL` is unset, the function is a no-op — safe for local dev.

### Existing freshness check (Phase 60.3)

`src/cron.py` queries `max(extracted_at)` on `comment_extractions` after every cron run. If stale (>1 day), logs a `WARNING` to stderr (visible in Railway logs). This caught the April 21 → May 9 silent-drop incident retroactively.

### Other observability

- **Railway logs**: real-time stderr/stdout from cron runs. Searchable in dashboard.
- **Supabase logs**: query logs, RLS denials, function errors. Accessible per-table.
- **Lovable Cloud edge function logs**: invocation count + duration + errors. In Lovable's dashboard.
- **Resend dashboard**: email delivery success rates + bounces.
- **Super-admin → System health tab**: in-app, aggregated stats from `platform_events`.

---

## Operations runbook

### "Morning briefing didn't send"

1. Check Supabase: `select max(computed_at) from flash_reports;` — did last night's cron write a row? If no, problem is upstream (cron).
2. Check Lovable edge function logs for `send-executive-briefing-email`. Did it invoke at 08:00? Did all chunks succeed?
3. Check Resend dashboard for delivery / bounce status.
4. If chunked send had per-recipient failures: re-invoke with `only_recipients=<failed-list>` (Phase 54 pattern, see 10 May 2026 entry in shipping log).

### "Idea submission fails"

1. Check Phase 66 trigger: `select pg_get_triggerdef(oid) from pg_trigger where tgname = 'trg_autoassign_idea_to_chair';` should return 1 row.
2. Check `current_committee_chair_id()` returns non-NULL.
3. Verify `assigned_to_user_id` column exists on `ideas` (not `assigned_user_id` — Phase 67.1 lesson).

### "Idea response submission fails"

1. Check the idea has an assignee: `select assigned_to_user_id from ideas where id = '...';`. If NULL, the Phase 67 RPC will refuse. Backfill via `committee_update_idea` or direct UPDATE.
2. Check `submitted_by_user_id` column exists on `idea_responses` (Phase 67 addition).
3. Check user has execute permission on `idea_add_response` (Phase 63 ensured this).

### "Auto-deploy didn't fire"

1. Check Railway dashboard → service → Source: should show `DDaios80/dailyflash` with Disconnect button (means connected). Auto-deploy toggle below: should show Disable button (means enabled).
2. Check Railway → Deployments: any recent failed builds?
3. Check GitHub repo → Settings → Webhooks: is Railway's webhook there + recent deliveries successful?
4. Manual fallback: `railway up` from local. Then re-investigate webhook.

### "PostgREST PGRST205 error on a table/RPC"

1. Verify the table/function has GRANT to `authenticator` role: `grant ... to authenticator, anon, authenticated, service_role`.
2. Force PostgREST cache reload: `notify pgrst, 'reload schema';`
3. Reference: Phase 51 + Phase 63 set this convention. If a new migration created an object without the grant, fix it via a follow-up migration.

---

## Key conventions

### Schema naming (work-in-progress)

The schema has accumulated multiple naming patterns over time. Today's preferred convention (going forward):

- **Status enum**: `status` (or `approval_status` if `status` is overloaded)
- **State transition timestamp**: `{verb}_at` (e.g., `approved_at`, `rejected_at`, `acknowledged_at`, `resolved_at`)
- **Actor for transition**: `{verb}_by_user_id` (e.g., `approved_by_user_id`, `rejected_by_user_id`, `acknowledged_by`)
- **Assignment**: `assigned_to_user_id` + `assigned_to_name` (denormalized display)

⚠️ Existing tables don't all follow this. Audit + rename pending — see `docs/2026-05-14-update-statement-audit.md` for the structural recommendations.

### Migration phases

Migrations are numbered sequentially: `phase{N}_{description}.sql`. New migrations should:

1. Be idempotent (`if not exists`, `create or replace`)
2. Include a header comment explaining motivation + linkage to prior phases
3. End with a verification SELECT that confirms the change applied
4. Grant new functions to all 4 PostgREST roles (Phase 51 convention)
5. Call `notify pgrst, 'reload schema';` after structural changes

### Atomic status transitions

⚠️ Critical pattern enforced by code review (not yet by DB constraints):

> Any UPDATE that changes a status enum value MUST write the corresponding `{status}_at` timestamp AND the `{status}_by_user_id` actor reference in the same statement. If the columns don't exist for the new status, ADD THEM in the same migration that introduces the transition.

This convention is documented in `docs/2026-05-14-update-statement-audit.md` and was retroactively enforced via Phase 56, 58, 64.

### Lovable verification protocol

⚠️ Lovable's chat-claimed "shipped" is not reliable. Verified failure pattern across at least 4 features in 2026. Standard verification before accepting "shipped":

1. Quote file path + line numbers modified
2. Paste deployed code (not proposed code)
3. Live screenshot of the rendered output
4. Only then call it shipped

---

## Project context

- **Single tenant**: built for Daios Cove specifically. Not multi-property.
- **Bus factor**: 1 (Dimitrios). Pairing with a second developer is the highest-leverage next step.
- **Test coverage**: 0% formally. All testing is manual via UI + SQL queries.
- **Phases shipped**: 67+ as of 2026-05-14.

## Links

- **Shipping log**: https://www.notion.so/34cfd0735bf5814bb425daac35b0bf81
- **Production dashboard**: https://flashreport.daioscove.com
- **Super-admin**: https://flashreport.daioscove.com/super-admin (requires super_admin role)
- **Repository**: https://github.com/DDaios80/dailyflash
- **Railway project**: helpful-patience / dailyflash service
- **Supabase project**: Sales Explorer
- **Lovable project**: Daios Cove Flash

## Phase history (high level)

Phase numbering is dense — see Notion for the complete log. High-level milestones:

| Phase range | Topic |
|---|---|
| 1-4 | Core schema + xlsx ingest + occupancy + special-attention |
| 5-9 | Email pipeline, role gates, FAM trip submission |
| 10-15 | A-lister hardening, ideas + my page + user management |
| 16-22 | Super admin, idea reminders, ExCo rotation, gamification |
| 23-29 | Reissue flash, ask-daios Q&A, knowledge graph, dedup logic |
| 31-36 | Pool heating dedup, explore range, admin in-app approvals, kid ages |
| 41-48 | Edge function dedup, backfill scripts, comment hash |
| 49-58 | Pool heating room rules, PostgREST cache eradication, recipients, escalation (abandoned), idea acknowledgement, state-machine audit |
| 59-62 | Zoho priority strip trigger, pool heating redesign (47-room grid), pool cleaning + fence + cribs panels |
| 63-67 | Defensive grants audit, reject-path audit-trail, ideas response thread + edit + auto-assign + sticky author |
| 68 | Uptime heartbeat (this README) |
