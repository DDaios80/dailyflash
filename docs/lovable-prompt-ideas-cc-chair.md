# Lovable prompt — CC the chair on idea-response emails

## What

When the chair (Valia or whoever the rotation lands on) replies to an idea,
the system sends a templated "Response sent" email to the original
submitter. Right now the chair isn't on that email at all. They should be
CC'd so:

- The chair has a record in their own inbox of what was sent on their
  behalf
- The chair sees how the response was formatted/wrapped
- The submitter sees the chair's address (real human) alongside the
  no-reply committee@ from-address, which builds trust

## Where

The `send-idea-response` edge function. Add a `cc` field to the Resend
(or whichever provider) call.

## How to find the chair's email

The chair is recorded on the idea record when they ack the response.
Phase 56's atomic write updates these columns at the same time:

- `acknowledged_at`
- `committee_response`
- `acknowledged_by` (uuid → auth.users.id)

So:

```
const { data: ackUser } = await supabase
  .from('auth.users')
  .select('email')
  .eq('id', idea.acknowledged_by)
  .single();

const chairEmail = ackUser?.email;
```

If `acknowledged_by` is null (legacy idea, response came in some other
way) — skip the CC and continue. Don't fail the email send.

## Body of the change

Add to the existing `send-idea-response` function, in the spot that
builds the email payload:

```
const emailPayload = {
  from: 'Daios Cove Committee <committee@daioscove.com>',
  to: idea.submitter_email,
  cc: chairEmail ? [chairEmail] : undefined,
  subject: ...,
  html: ...,
};
```

If you're using Resend's API directly the field is also `cc` (array of
strings or undefined).

## Edge cases

- **Chair email lookup fails** (auth lookup error, RLS, network): continue
  without CC. Log the failure but don't fail the send. The submitter
  must still get their reply.
- **Chair replied to themselves** (theoretically possible if the chair
  IS the submitter — happens with internal test ideas): CC their own
  address, no harm. Don't try to dedupe.
- **`acknowledged_by` is null**: skip CC. This is the case for ideas
  that pre-date Phase 56's atomic-write fix.

## Verification after deploy

1. Submit a test idea (or pick a pending one in `ideas` table).
2. Have a chair reply (via the chair email link or via the dashboard
   committee response field).
3. Check the submitter's inbox — From: `committee@daioscove.com`,
   CC: chair's email.
4. Check the chair's Sent / Inbox — they should have a copy of the
   response that went to the submitter.

## Out of scope

- Auto-reply behavior changes (none).
- Chair-email rotation logic (no change).
- Whether to BCC vs CC (CC by design — both parties see each other,
  matches our brand-as-real-people preference).
