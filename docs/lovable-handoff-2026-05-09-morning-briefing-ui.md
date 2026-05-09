# Lovable handoff — Morning Briefing Recipients (Phase 54)

Two changes the dashboard needs. Both follow the existing FAM Trip Recipients
and Site Inspection Recipients pattern — copy-and-adapt, no architectural
new ground.

## Step 1 — Apply the SQL migration

Run `db/phase54_morning_briefing_recipients.sql` in Lovable's SQL editor.
Adds an empty `app_settings.morning_briefing_recipients` row that the UI and
edge function will read from.

## Step 2 — Add admin UI panel

Paste this prompt into Lovable's chat:

> Add a new section to the `/admin` page called **"Morning Briefing
> Recipients"**. Place it directly below the existing "Site Inspection
> Recipients" section in the Distribution group.
>
> The UI should mirror the "Site Inspection Recipients" section exactly:
>
> - Header label: "DISTRIBUTION" / "📧 Morning Briefing Recipients"
> - "Currently N valid emails." subhead (counted from the parsed list)
> - Multiline textarea pre-filled from `app_settings.morning_briefing_recipients.value`
> - "Save recipients" button on the top right that upserts the textarea
>   contents back to that app_settings row
> - Help text below the textarea: "Email addresses that receive the
>   ExCom morning briefing each day at 8:00 Athens time. One per line or
>   comma-separated. Paste from Maria's existing distribution list."
>
> The row already exists in app_settings (key = `morning_briefing_recipients`,
> currently empty value). Use the same parsing helper as the other recipient
> sections (extracts emails between `<>` brackets and bare addresses,
> dedupes, lowercases).

## Step 3 — Update the morning briefing edge function

Find the edge function that sends the ExCom briefing at
`exec_briefing_send_time_athens` (likely named `send-exec-briefing`,
`send-morning-briefing`, or similar). Paste this prompt into Lovable's chat:

> The edge function that sends the morning ExCom briefing currently emails
> a small set of users (resolved from user_roles / committee_chair_user_id).
> We added a new bulk recipient list at
> `app_settings.morning_briefing_recipients`.
>
> Update the function to ALSO send the briefing to every email parsed from
> that bulk list. Merge with the existing recipient set, dedupe by email
> address (case-insensitive), and chunk to 50 per Resend call (matching the
> approve-fam-trip / approve-site-inspection pattern from Phase 46).
>
> Recipient parsing: same regex as Phase 46 — extract emails between `<>`
> brackets first, fall back to bare-email regex for unbracketed entries,
> lowercase, dedupe.
>
> Best-effort: if the new list is empty, send only to the original
> recipient set (no behaviour change). If Resend fails for the bulk chunk,
> the original recipients still got it — log the failure but don't block.

## Step 4 — Verify

Once both UI and edge function are live:

1. Paste a single test email into the new textarea, click Save.
2. Wait for tomorrow's 8:00 Athens send (or trigger manually if there's a
   "send now" button).
3. Verify the test address received the briefing alongside the regular
   ExCom recipients.

## What this enables

The admin team can now manage the morning briefing distribution list without
asking for a code change every time someone joins or leaves. Same UX as the
FAM Trip / Site Inspection recipient management already in production.

## Format reference

The textarea expects the same format pasted from existing template emails:

```
Name <email@daioshotels.com>; Name <email@daioshotels.com>; ...
```

or one per line:

```
Name <email@daioshotels.com>
Name <email@daioshotels.com>
```

or just bare emails:

```
email1@daioshotels.com, email2@daioshotels.com
```

The parser handles all three styles.
