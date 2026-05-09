# Phase 56 — fix ideas inbound-reply handler to stamp acknowledged_at

## The bug

Today (2026-05-09), Valia (the ExCom chair) replied via email to a
notification about idea `4d3beabc-caaf-4d4d-b25e-2e41d43e841a` ("Proposal
for review of HB / FB beverage inclusion policy"). The system:

1. ✅ Captured her reply text into `ideas.committee_response` (1424 chars).
2. ✅ Sent the templated "Response sent" email to the submitter (Maria).
3. ❌ Did NOT stamp `acknowledged_at`.
4. ❌ Did NOT stamp `acknowledged_by`.

So the dashboard still shows the idea as "Awaiting committee review"
even though the response went out and is stored. Half-finished
state-machine.

## The fix

There's an edge function (or RPC) that processes inbound replies to
`flash@daioscove.com` for idea threads and writes them to
`ideas.committee_response`. Find it (likely names: `process-idea-reply`,
`inbound-idea-handler`, `parse-idea-email`, or similar) and add two
field assignments to its UPDATE statement:

```typescript
const { error } = await supabase
  .from("ideas")
  .update({
    committee_response: <parsed reply body>,
    // ↓ NEW — stamp the acknowledged fields atomically with the response
    acknowledged_at: new Date().toISOString(),
    acknowledged_by: <user_id of the email sender (Valia in today's case)>,
    updated_at: new Date().toISOString(),
  })
  .eq("id", <matched idea_id>);
```

Resolving the sender's user_id: look up `auth.users.id` by `email`
matching the `From:` address on the inbound email (probably already
parsed by the handler, just not used).

If the From address doesn't match any user, fall back to the current
`committee_chair_user_id` from `app_settings` — the chair is the most
likely sender of an idea reply.

## Paste this into Lovable's chat

> The edge function that processes inbound email replies to
> `flash@daioscove.com` for ideas captures the reply body into
> `ideas.committee_response` but forgets to stamp `acknowledged_at`
> and `acknowledged_by`. Result: the dashboard shows the idea as
> "Awaiting committee review" indefinitely even though the reply
> is stored and the response email already went out to the submitter.
>
> Find that edge function and update its UPDATE statement to ALSO set:
>   - `acknowledged_at = now()`
>   - `acknowledged_by = <user_id of the From address>` (or fall
>     back to `app_settings.committee_chair_user_id` if no match)
>
> Atomically with the existing committee_response write. Verify by
> sending a test reply from a chair address — the idea card should
> flip from "Awaiting committee review" to acknowledged state
> immediately.

## Companion SQL backfill

Apply `db/phase56_ideas_acknowledged_backfill.sql` once. It stamps
`acknowledged_at = updated_at` on every existing idea where
`committee_response` is populated but `acknowledged_at` is NULL. That
restores all historical broken rows in one pass.

`acknowledged_by` is left NULL for historical rows since we don't have
a reliable signal for which chair replied. Future replies (after
Lovable ships the fix) will populate both fields atomically.

## Why we ran into this today

Same operational gap also affected the morning briefing recipient
miss (Phase 55) and last week's pool heating dashboard (Phase 49–52):
state changes that should be atomic across multiple fields in a
state-machine often slip when a feature ships without an end-to-end
test that walks every field. Worth a quick audit pass on the other
state-machines in this codebase: fam_trips, site_inspections, groups
all have the same pattern (`status`, `*_at` timestamps, `*_by` user
references). If any of those have similar half-finished writes, the
same kind of "approved but not really" UI state can hide bugs.
