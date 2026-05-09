# Phase 57 — committee response emails from committee@daioscove.com

## What changes

When the chair (Valia or whoever the rotation lands on) replies to an
idea via email, the system sends a templated "Response sent" email to
the original submitter. Today that email comes from the default
flash@... address. Operations preference: it should come from
`committee@daioscove.com` so the submitter recognises it as the
official committee reply.

## Configurable via app_settings

The from address lives in `app_settings.committee_email_from`. Apply
`db/phase57_committee_email_from.sql` first; the value defaults to:

```
Daios Cove Committee <committee@daioscove.com>
```

Maria can change this via SQL or the admin UI later (when an admin
panel exposes app_settings). The format is RFC 5322 — `Display Name
<address>` — so inboxes show the friendly name.

## Lovable changes — paste into chat

> When the inbound-idea-reply edge function sends the templated
> "Response sent" email to the idea submitter (the same edge function
> Phase 56 is patching to stamp acknowledged_at and acknowledged_by),
> it currently uses the default flash@... from address. Change it to
> read `committee_email_from` from `app_settings` instead, falling back
> to the existing flash address if the row is missing.
>
> The value is in RFC 5322 format ("Display Name <address>"), pass it
> straight through to Resend's `from` field. Default after migration
> is "Daios Cove Committee <committee@daioscove.com>".
>
> Update only the email TO THE SUBMITTER. The chair's notification
> email (the inbound trigger that prompts the chair to reply) keeps
> its existing from address — that's a different relationship.

## DNS / domain verification reminder

For `committee@daioscove.com` to actually send via Resend without
hitting the spam folder, the daioscove.com domain needs to be verified
in the Resend dashboard with the right SPF / DKIM / DMARC records. If
flash@daioscove.com already works for inbound, the domain is likely
verified for outbound too — but worth checking before the first
committee email goes out under the new sender.

## Verification

After Lovable ships the change:

1. Submit a test idea (or pick a pending one).
2. Have a chair reply via email.
3. Check the submitter's inbox — From line should read
   "Daios Cove Committee <committee@daioscove.com>".
4. If it lands in Spam, the daioscove.com domain isn't fully verified
   in Resend — fix DNS first.
