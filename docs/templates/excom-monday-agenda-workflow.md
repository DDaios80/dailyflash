# ExCom Monday Agenda — chair's weekly workflow

Companion to `excom-monday-agenda-template.md`. The template captures the FORMAT; this captures the PROCESS.

---

## Timeline

| When | Who | What |
|---|---|---|
| **Thursday** | Chair | Send call-for-input message to ExCom WhatsApp/email: "Please send by Sunday 18:00 — (a) on-track status (2-5 bullets), (b) new items proposing for Monday, (c) carry-over items still open." |
| **Friday-Sunday** | ExCom members | Reply with their bullets + any new items. Use the Ideas system + `idea_to_monday()` RPC to flag specific ideas for Monday discussion. |
| **Sunday evening** | Chair | Copy `excom-monday-agenda-template.md` to `excom-meeting-<DD.MM>.md`. Fill in placeholders. Distribute by 21:00. |
| **Monday 13:00** | Chair | Run the meeting on time. |
| **Monday post-meeting** | Chair | Use `idea_decide_at_monday()` RPC to record each decision in the Ideas system. |

---

## Where the content comes from

Each block has a typical source:

### Pre-reads
- Documents shared by ExCom members during the week
- Decks attached to ideas in the Ideas system
- External vendor proposals routed via the Chair

### A. Status snapshot
- Direct input from each ExCom member in response to Thursday's call-for-input
- Format: `<Name>: <item> | <item> | <item>`
- Rule: no discussion, ~1 min per person

### B. Strategic framework
- Only when a tool / framework / shared mental model needs introduction
- Examples from history: PRINCE2 Lite walkthrough (11 May 2026)
- Skip in weeks where no such item is queued

### C. Carry-over decisions
- Items marked `status = 'for_monday'` in the `ideas` table from previous weeks
- Query: `select id, body, for_monday_reason from ideas where status = 'for_monday' order by for_monday_at;`
- Aim for 2-3 items at 15 min each

### D. New strategic items
- Ideas submitted during this week that the chair wants to escalate to Monday
- Or new operational items raised by ExCom members
- Aim for 1-2 items

### E. Heads-up batch
- Information-only updates from ExCom members
- 3-5 items at 2-5 min each

### F. Close
- AOB
- Handover to next week's chair (query: `select email from excom_rotation_upcoming(1);`)

---

## Time budget arithmetic

The template defaults: 10 + (15) + 45 + 20 + 12 = **102 min** allocated, **18 min buffer** for AOB + overruns.

If you find yourself adding more items than the time budget allows, the deferral hierarchy is:
1. Drop items from E (Heads-up batch) — move to written follow-up via email
2. Compress B (Strategic framework) — make it a Heads-up instead of a walkthrough
3. Reduce C items from 3 to 2
4. Last resort: extend meeting to 130 min, communicate in advance

Never let A (Status snapshot) eat into discussion time. If it's running long, move overflow status to written follow-up.

---

## Using the Ideas system

The Daily Flash app has integrated Monday-agenda tooling at the DB layer (Phase 25). The chair can:

- **Flag an idea for Monday discussion**: call `idea_to_monday(p_idea_id, p_reason)` — sets `status = 'for_monday'`, captures the reason
- **Record decisions from Monday**: call `idea_decide_at_monday(p_idea_id, p_outcome, p_notes, p_action_assignee)` — sets `status = 'decided_at_monday'`, captures notes
- **Generate a Monday agenda automatically** (if/when the UI ships): the `generate_monday_agenda()` and `monday_agenda_view()` RPCs exist as scaffolding for the eventual chair-desk view

For now, the chair queries `ideas` manually or via the Chair Desk view in the Lovable dashboard.

---

## Future automation (queued Lovable feature)

A "**Monday Agenda Builder**" view in the dashboard would streamline this dramatically:

1. **Sidebar**: each ExCom member with their status-update input box (auto-emailed Thursday, due Sunday)
2. **Main area**: drag-and-drop assembly of the agenda blocks from:
   - Submitted status snapshots (Block A)
   - Ideas marked `for_monday` (Block C carry-overs)
   - New ideas not yet triaged (Block D candidates)
   - Heads-up submissions (Block E)
3. **Time-budget meter**: shows allocated vs 120-min total as items are added
4. **Generate Markdown**: outputs the filled-in template for the chair to review + send
5. **Track decisions**: after the meeting, each item gets an outcome (decided / tabled / action-assigned) which writes back to the ideas system

This would integrate with the existing Phase 25 schema (`monday_agendas` table, `generate_monday_agenda()` RPC). The DB layer is already there — only the UI layer is missing.

Tracked in the project todo list as "Lovable Phase 36/36.1 dashboard".
