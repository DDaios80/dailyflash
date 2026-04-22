# Daily Flash — email delivery setup

Ship the daily flash report as role-tailored HTML emails to every user in the
Lovable Cloud auth table, **after human approval** by the Rooms Division
Manager (Thelxi Smyrnaki). Fires at **06:00 Europe/Athens** every morning.

End state:
```
  23:00 Athens  Railway `dailyflash` cron  → OneDrive xlsx → pipeline → edge
                                              function → flash_reports row

  06:00 Athens  Railway `email-dispatcher` cron  → send-flash-email (mode=preview)
                                              → single email to Thelxi with
                                                Approve / Reject buttons
                                              → flash_email_approvals row (w/ token)

  ~06:15 (Thelxi clicks Approve)  →  approve-flash-email?token=…&action=approve
                                              → marks approved, calls
                                                send-flash-email (mode=fanout)
                                              → fan-out to all 59 other
                                                recipients (role-filtered)
                                              → Resend → inboxes
```

If Thelxi clicks Reject, or never clicks, nothing goes out.

All pieces are committed to the repo. You (human) wire them up.

## Part 1 — Extend the role enum (Lovable SQL editor)

Open Lovable → Supabase → SQL editor → paste the contents of
`db/phase5_email_roles.sql` → Run.

Expected: 12 `ALTER TYPE user_role ADD VALUE` lines succeed, plus the updated
helpers (`can_see_guest_detail`, `can_see_alister`, `email_recipients`) and
the new `email_deliveries` table.

**Known Postgres quirk:** `ALTER TYPE ADD VALUE` cannot run in the same
transaction that subsequently uses the new value. If Lovable's SQL runner
wraps everything in one transaction and complains, run the file twice —
first pass adds the enum values, second pass picks up the rest.

Verify:
```sql
select unnest(enum_range(null::user_role)) as role;
```
You should see all 16 values: admin, management, guest_relations, front_office,
sales, marketing, accounting, it, call_center, general, housekeeping, fnb,
maintenance, reservations, kepos, kids_club.

## Part 2 — Verify the sender domain in Resend (~5 min)

1. Log in to https://resend.com → **Domains** → **Add Domain** → enter
   `daioshotels.com`.
2. Resend gives you 4 DNS records to add (MX, TXT SPF, TXT DKIM, optional DMARC).
   They look roughly like:
   - `send.daioshotels.com` → MX → `feedback-smtp.eu-west-1.amazonses.com`
   - `send.daioshotels.com` → TXT → `v=spf1 include:amazonses.com ~all`
   - `resend._domainkey.daioshotels.com` → TXT → `p=MIGfMA0GCSqGSIb3DQEBAQUAA4...`
   - `_dmarc.daioshotels.com` → TXT → `v=DMARC1; p=none;` (optional)
3. Paste these into whichever DNS provider hosts `daioshotels.com` (GoDaddy,
   Cloudflare, Route53 — wherever your Office 365 MX records live).
4. Back in Resend, click **Verify** — propagation usually takes 5-30 min.
5. Once verified, grab an API key: Resend → **API Keys** → **Create** →
   scope it to **Full access** (sending) → copy.

> **Important:** Verify `daioshotels.com` as the sending domain, but do NOT
> change the existing MX record on `daioshotels.com` itself — that still
> points at Microsoft 365 for receiving email. Resend only needs a subdomain
> (`send.daioshotels.com`) plus the DKIM TXT.

## Part 3 — Add secrets to Lovable Cloud

In Lovable → Project Settings → Secrets (or Edge Functions → Secrets, UI
varies), add:

| Key | Value |
|---|---|
| `RESEND_API_KEY` | the key you just generated |
| `EMAIL_FROM` | `Daios Cove Flash <flash@daioshotels.com>` (or any `@daioshotels.com` address on the verified domain) |
| `DASHBOARD_URL` | the public URL of your Lovable deployment (e.g. `https://daios-flash.lovable.app`) |
| `APPROVER_EMAIL` | `thelxi.smyrnaki@daioshotels.com` — Rooms Division Manager who approves each day's send |
| `SEND_FLASH_EMAIL_URL` | `https://<project-ref>.supabase.co/functions/v1/send-flash-email` — used by the approval fn to trigger fanout |
| `APPROVE_FLASH_EMAIL_URL` | `https://<project-ref>.supabase.co/functions/v1/approve-flash-email` — used by send-flash-email to build Approve/Reject links |
| `PIPELINE_SECRET` | already set — same as the ingest function |

`SUPABASE_URL` and `SUPABASE_SERVICE_ROLE_KEY` are automatically injected by
Lovable; you don't need to set them.

## Part 4 — Deploy the two edge functions (Lovable)

Both functions live in `edge-functions/` in this repo — paste each file into
a new Lovable Cloud edge function with the matching name.

1. **`send-flash-email`** — copy `edge-functions/send-flash-email/index.ts`.
   Handles preview, fanout, and direct modes.
2. **`approve-flash-email`** — copy `edge-functions/approve-flash-email/index.ts`.
   The endpoint Thelxi's Approve/Reject links hit.

Deploy both. URLs will be:
```
https://<project-ref>.supabase.co/functions/v1/send-flash-email
https://<project-ref>.supabase.co/functions/v1/approve-flash-email
```
where `<project-ref>` matches the one in your ingest URL
(`wgbghdbfmapuqbfeiygb` at time of writing).

> **Important:** the `APPROVE_FLASH_EMAIL_URL` secret you set in Part 3 must
> match the `approve-flash-email` function URL. Double-check before the first
> live send.

## Part 5 — Add the 60 users to Lovable auth

Lovable → Auth → Users → **Invite user** or **Create user** for each row in
`Contact list for flash report 2026.xlsx`. After creating each one, assign
their role with:

```sql
select set_user_role('<email>', '<role>'::user_role);
```

Roles map straight from the xlsx column:
- `Management` → `management`
- `Guest Relations` → `guest_relations`
- `Front Office` → `front_office`
- `Housekeeping` → `housekeeping`
- `F&B` → `fnb`
- `Maintenance` → `maintenance`
- `Sales` → `sales`
- `Marketing` → `marketing`
- `Accounting` → `accounting`
- `IT` → `it`
- `Call Center` → `call_center`
- `Reservations` → `reservations`
- `Kepos` → `kepos`
- `Kids Club` → `kids_club`
- `general` → `general`

> **Security note:** the xlsx contains plaintext passwords. Don't commit it,
> don't share it over insecure channels, and force a password reset at first
> login (Lovable Auth supports this on user creation). If those passwords
> have already been distributed over email/Slack/WhatsApp, rotate them
> before inviting the users.

## Part 6 — Smoke tests (recommended before adding everyone)

### 6a. Direct-to-yourself (`mode=direct` bypasses approval)

With just yourself (`d.daios@daioshotels.com`, role `management`) in
`user_roles`, hit the function with `mode=direct` to skip the approval gate:

```bash
curl -X POST https://<project-ref>.supabase.co/functions/v1/send-flash-email \
  -H "Authorization: Bearer $PIPELINE_SECRET" \
  -H "Content-Type: application/json" \
  -d '{"mode":"direct","date":"2026-04-22","only_recipients":["d.daios@daioshotels.com"]}'
```

Check your inbox. The email should show:
- Daios Cove branded header
- "Role: Management"
- Occupancy card (3 days)
- Special attention, complimentary, PEP, birthdays, allergies, A-lister, pool heating, daily briefing (whichever have data)
- Link to the full dashboard

### 6b. Approval gate dry-run

Once Thelxi is in `user_roles` (role `management`), test the real flow:

```bash
# Fire the preview — single email to Thelxi with Approve/Reject buttons
curl -X POST https://<project-ref>.supabase.co/functions/v1/send-flash-email \
  -H "Authorization: Bearer $PIPELINE_SECRET" \
  -H "Content-Type: application/json" \
  -d '{"date":"2026-04-22"}'   # mode defaults to preview
```

Check `thelxi.smyrnaki@daioshotels.com` inbox — should see the flash with
Approve/Reject buttons. Click Approve. The browser lands on a confirmation
page saying "Sent to N recipients". Everyone else should now have the flash
in their inbox.

If Thelxi clicks Reject instead, nothing goes out. You can verify with:

```sql
select * from flash_email_approvals where report_date = '2026-04-22';
select status, count(*) from email_deliveries where report_date = '2026-04-22' group by status;
```

### 6c. Dry run of fanout (list recipients without sending)

```bash
curl -X POST https://<project-ref>.supabase.co/functions/v1/send-flash-email \
  -H "Authorization: Bearer $PIPELINE_SECRET" \
  -H "Content-Type: application/json" \
  -d '{"mode":"direct","date":"2026-04-22","dry_run":true}'
```

Response shows how many recipients would be emailed and what role mapping
each got — inspect before flipping off `dry_run`.

## Part 7 — Schedule it on Railway (06:00 Europe/Athens)

Two simple options:

### Option A (recommended) — second service in the same project

1. Railway → **New** → **Empty service** → name it `email-dispatcher`.
2. Settings → **Source** → connect to the same `DDaios80/dailyflash` repo
   (same build will reuse the cached Docker layer).
3. Settings → **Deploy**:
   - **Start Command**: `python -u src/email_dispatcher.py`
   - **Cron Schedule**: `0 6 * * *`
   - **Restart policy**: Never
4. Variables — copy from `dailyflash` service, plus add:
   | Key | Value |
   |---|---|
   | `SEND_FLASH_EMAIL_URL` | `https://<project-ref>.supabase.co/functions/v1/send-flash-email` |
   | `TZ` | `Europe/Athens` |

   Only `SEND_FLASH_EMAIL_URL`, `PIPELINE_SECRET`, and `TZ` are strictly
   required — the other pipeline vars are harmless to include.
5. Deploy. Railway's cron engine respects the service's `TZ` env var, so
   `0 6 * * *` fires at 06:00 Athens regardless of DST.

### Option B — shell command only

If you prefer zero Python, skip `email_dispatcher.py` and set the start
command to:

```bash
curl -fsS -X POST "$SEND_FLASH_EMAIL_URL" \
  -H "Authorization: Bearer $PIPELINE_SECRET" \
  -H "Content-Type: application/json" \
  -d '{}'
```

Same result, one fewer process. Pick one.

## Part 8 — First live run

Monday–Sunday, 06:00 Athens. Check:

1. Railway `email-dispatcher` → Cron Runs → last run succeeded
2. Lovable SQL: `select * from email_deliveries where report_date = current_date - 1 order by sent_at desc limit 20;`
3. Your own inbox

## Troubleshooting

**"no flash_reports row for 2026-04-22"**: the 23:00 UTC pipeline the night
before failed. Check that service's logs, not this one.

**"Resend 403: ..."**: domain not verified yet, or API key missing the
`emails:send` scope. Resend → Domains → check status; API Keys → rotate.

**Recipients list empty**: `user_roles` is empty. Run
`select set_user_role(...)` for each user, or bulk-insert.

**Email landed in spam**: verify DKIM + SPF records; set DMARC to
`p=quarantine` once deliverability is confirmed.

**"approver ... not in user_roles"**: Thelxi's account hasn't been created
in Lovable Auth yet, or her role isn't assigned. Create the user, then:
`select set_user_role('thelxi.smyrnaki@daioshotels.com','management');`

**Thelxi missed the email / didn't click by noon**: the flash is still in
`flash_email_approvals` as pending. Two recovery paths:
1. She clicks the link whenever she sees it — still works (no expiry).
2. Admin forces send via `mode=direct` — bypasses approval entirely.

**Need to force-approve manually** (e.g. Thelxi on leave):
```sql
-- Grab the token
select approval_token from flash_email_approvals where report_date = current_date;
-- Then an admin hits the approve URL in a browser with that token.
```
Or, if you want to skip the approval gate entirely for a specific day, call
`mode=direct` via curl. That bypasses `flash_email_approvals` altogether.

## Phased PDF attachment (v2 — not yet shipped)

Right now the email is HTML-only with a link to the live dashboard. To add a
PDF attachment:

1. Build a server-renderable one-pager view in Lovable at
   `/dashboard/<date>/pdf?role=<role>` that returns `Content-Type: application/pdf`.
   (Or any Lovable page + a PDF rendering library — react-pdf, Puppeteer
   via a separate service, etc.)
2. Update `edge-functions/send-flash-email/index.ts`:
   - Fetch the PDF for each role
   - Base64-encode and pass via Resend's `attachments` field:
     ```ts
     body: JSON.stringify({
       from, to, subject, html,
       attachments: [{
         filename: `daily-flash-${reportDate}.pdf`,
         content: base64PdfContent,
       }],
     })
     ```
3. Deploy the updated edge function.

Separate session, one hour of work once the Lovable PDF view exists.

## Cost

- Resend: **free tier** is 100 emails/day, 3000/month. At 60 recipients × 30 days = 1800/month. Under the free ceiling. If you add more users or kick off retries, the Pro plan ($20/mo, 50k emails) is the step up.
- Railway second service (cron ~30s/day): essentially free — folds into the existing Hobby tier.
