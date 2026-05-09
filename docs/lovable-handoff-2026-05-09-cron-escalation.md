# Phase 55 — Move cron to 22:00 Athens + auto-escalate at 07:30 Athens

Two operational changes after this morning's missed flash incident:

1. Cron generates flash earlier (22:00 Athens, was 23:00+) so Thelxi has
   more waking time to approve before bed.
2. If flash isn't approved by 07:30 Athens, auto-escalate to d.daios so
   he can approve before the 08:00 mass-send window.

## Changes by ownership

### Railway (user / dashboard action)

**1. Move the existing `dailyflash` cron from current schedule to 22:00 Athens.**

Athens is UTC+3 in summer, UTC+2 in winter. The Railway service has
`TZ=Europe/Athens` set, so the cron expression is local time:

- New schedule: `0 22 * * *`  (every day at 22:00 Athens)

Find this in Railway dashboard → `dailyflash` service → Settings → Cron
Schedule. Replace the existing string.

**2. Add a NEW cron service (or scheduled task) for the escalation.**

- Name: `dailyflash-escalation` (or similar)
- Schedule: `30 7 * * *`  (every day at 07:30 Athens)
- Start command: `python src/escalate_flash.py`
- Same env vars as the main `dailyflash` service (SUPABASE_URL,
  SUPABASE_SERVICE_ROLE_KEY, SEND_FLASH_EMAIL_URL, PIPELINE_SECRET)
- Same git source / branch

The script (committed at `src/escalate_flash.py`) checks
`flash_email_approvals.approved_at` for today's report_date. If null
and not rejected, it calls `send-flash-email` with
`approver_email: d.daios@daioshotels.com` (the address is configurable
via `app_settings.flash_escalation_approver_email`).

### SQL (Lovable SQL editor)

Run `db/phase55_escalation_settings.sql`. Adds the
`flash_escalation_approver_email` row to `app_settings` with default
`d.daios@daioshotels.com`. Maria can change it later via SQL or the
admin UI when the `app_settings` panel exposes it.

### Lovable (chat panel prompt)

The existing `send-flash-email` edge function may not yet accept an
`approver_email` body field. Paste this into Lovable's chat:

> Update the `send-flash-email` edge function to accept an optional
> `approver_email` field in the POST body (alongside `date` and
> `mode`). When provided, send the preview/approval email to that
> address INSTEAD of the default approver resolved from app_settings.
>
> Also accept an optional `escalation: true` flag. When set, prepend
> the email subject with `[ESCALATION]` and add a sentence in the
> email body explaining that the original approver hasn't acted yet.
>
> The existing default behaviour (no approver_email, no escalation
> flag) must remain unchanged so the 22:00 nightly cron continues
> sending to Thelxi as today.

## After all three are in place

Daily flow:

- **22:00 Athens** — cron generates flash, sends preview to Thelxi.
- **22:00 → 07:30** — Thelxi has the whole evening + morning until 07:30
  to click Approve. If she does, mass-send fires immediately, recipients
  get the flash. Done.
- **07:30 Athens** — escalation cron checks approval status. If not
  approved, sends a fresh `[ESCALATION]` preview to d.daios.
- **07:30 → 08:00** — d.daios has 30 minutes to click Approve. If he
  does, recipients get the flash before 08:00.
- **08:00 onwards** — if still nothing approved, recipients don't get
  a flash today. Manual intervention needed (someone clicks Approve
  via either email link).

## Verification tomorrow morning

- Check that the 22:00 cron fired by querying flash_email_approvals
  for today's report_date.
- If still not approved by ~07:35 Athens, confirm d.daios received
  the escalation email.
