# Notion fill-in — Phases 52–58 (8 May 2026)

These shipped on 8 May evening through early 9 May, but the original Notion
update timed out and they never made it into the shipping log. Today's
9 May session covered 59 + 60.x + 61 inline; this doc fills the gap.

**Target page:** Daily Flash — Shipping Log
https://www.notion.so/34cfd0735bf5814bb425daac35b0bf81

**Two updates needed:**

## 1. TOC entry update

Find:
```
- [8 May 2026](#8-may-2026) — Phase 49: pool heating dashboard respects room category rules. Villas always shown (action required), Collection / DLXP / DJSTEP suppressed from heating, JSTEP/STEP only when commented. Pool fence requests surface across all categories (kid safety).
```

Replace with:
```
- [8 May 2026](#8-may-2026) — Phases 49–58: pool heating room-category rules (49); PostgREST cache eradication via in-memory compute + authenticator grants (50, 50.1, 50.2, 51); upload-replacement extraction preservation (52); morning-briefing recipients (54); cron escalation drafted then abandoned (55); atomic `acknowledged_at` + state-machine audit (56, 58); committee email From-line pending domain verification (57). Pool fence requests surface across all categories (kid safety).
```

## 2. Section content insert

Find anchor:
```
<td>GRANT to authenticator role for PostgREST cache discovery</td>
</tr>
</table>
---
## 8 May 2026
Follow-up on yesterday
```

Insert the following content between `</table>` and `---` (i.e., extending
the "## 8 May 2026 (continued)" section, before the older "## 8 May 2026"
section starts):

---

### Phase 52 — Preserve `comment_extractions` across upload replacement

Phase 41 cleanup logic in `ingest-flash-report` deletes the previous upload's reservations when a new one arrives, then upserts the new ones with potentially new UUIDs. Without preservation logic, every cron would orphan all `comment_extractions` rows (FK on `reservation_id`) and the cache would never warm up.

Phase 52 added re-linking: match new reservations to old by `resv_name_id`, redirect the `comment_extractions.reservation_id` FK to the new UUID. Verified zero orphans across all 214 historical extractions.

*Note: today's (9 May) discovery showed Phase 52's preservation worked correctly, but the PRIMARY upsert-on-conflict in the same edge function was broken (`DO NOTHING` instead of `DO UPDATE`). The two are independent bugs in the same function.*

### Phase 54 — Morning briefing recipient list (admin-managed)

User: "we need a section where the admin will upload a bulk email list (like for FAM trips and Inspections) and also have the possibility to add or remove recipients at a later stage." Plus: "copy recipient list for FAM trips and inspections and use the same list for the morning briefing."

Seeded `app_settings.morning_briefing_recipients` with the 4469-char shared list. Lovable shipped the admin UI (at `/admin`) plus the edge function update. Initially the morning send only reached 7 of ~64 recipients (UI was done but edge function still using a hardcoded fallback list); Lovable patched, redeployed, verified working on 9 May.

### Phase 55 — Cron escalation (ABANDONED)

User initially asked for an auto-escalation: if Thelxi misses the 22:00 preview email, escalate to `d.daios` at 07:30 the next morning. Drafted SQL (`db/phase55_escalation_settings.sql`), Python (`src/escalate_flash.py`), Railway config (`railway.escalation.toml`), and a handoff doc. Created an empty Railway service `dailyflash-escalation` in the `helpful-patience` project via CLI, set 5 env vars (SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY, SEND_FLASH_EMAIL_URL, PIPELINE_SECRET, TZ).

Then: "will not deploy Phase 55. I will keep the current flow." Empty Railway service stays in the dashboard as dead infra (CLI can't delete services). Git artifacts kept as dead code in case we revisit.

### Phase 56 (commit `7c94504`) — Atomic `acknowledged_at` in `committee_update_idea` RPC

Bug surfaced when Valia (ExCom chair) replied to an idea via email: `committee_response` got 1424 chars stored, but `acknowledged_at` stayed NULL — so the system thought the chair hadn't replied yet. Half-finished state machine in the inbound-handler RPC: only some columns were written.

Lovable patched: state writes now atomic (`acknowledged_at`, `committee_response`, `acknowledged_by` all set in one transaction). Backfilled 4 historical rows where `committee_response` was set but `acknowledged_at` was NULL.

**Architectural takeaway**: half-finished state machines are a class of bug worth auditing. `fam_trips`, `site_inspections`, `groups` all have the same shape (status enum + `*_at` timestamp + `*_by` user reference). Phase 58 audit followed.

### Phase 57 (commit `6557f18`) — Committee response emails from `committee@daioscove.com`

User: "email responses from the committee shall be sent by `committee@daioscove.com`." Phase 57 swaps the `From` line for idea-response emails from the default `flash@daioscove.com` to a dedicated `committee@daioscove.com` address — maps the messaging architecture to the operational reality (the committee is a distinct voice, not the flash bot).

SQL + handoff doc shipped, but blocked on Resend domain verification for `daioscove.com`. User chose to wait rather than use a temporary fallback. Still pending end of 9 May.

### Phase 58 (commit `1d2e3aa`) — State-machine audit + backfill across `fam_trips` and `site_inspections`

Follow-on from Phase 56's takeaway. Audited every status-enum table for the same half-finished-state-machine pattern. Found 4 rows in `fam_trips` and `site_inspections` with `approved_at` set but `approver_user_id` NULL. Backfilled all 4 to `d.daios@daioshotels.com`. `groups` table verified empty (Phase 34 migration never applied; no operational impact — deploy `db/phase34_group_approvals.sql` when groups feature is wired up).

**Structural fix forward queued**: code-review pass on all UPDATE statements that touch a status enum to ensure `*_at` and `*_by` writes are atomic. Lovable AI's tendency to write half-state UPDATEs is the root cause; the structural fix is pattern enforcement in code review (e.g. require an RPC for any status transition), not just patch-by-patch backfill.

### Phase 53 (queued, not shipped)

Bridge UPSERT fix on `reservations.resv_name_id`. Risk: today's investigation initially suggested the same shape of bug as Phase 60.x's `comment_extractions` discovery, but Lovable's SQL editor verification showed the live `reservations` table is healthy (4039 rows, multiple recent uploads). False alarm at the data layer; the issue was Python/PostgREST stale-cache. Phase 53 stays queued for whenever a real reservations bug surfaces.
