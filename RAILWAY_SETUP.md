# Railway deployment — Path A (OneDrive preserved)

End state: Railway runs the daily pipeline at 21:00 UTC (≈ 23:00 Athens winter / 00:00 summer). Reads the xlsx from your OneDrive `DailyFlash` folder via Microsoft Graph API. No laptop needed.

## Part 1 — Register an Azure AD app (~10 min)

You need a public-client app with `Files.Read` delegated permission. Admin consent is **not** required.

1. Go to https://portal.azure.com → search **App registrations** → **New registration**.
2. Fill in:
   - **Name**: `Daios Daily Flash Pipeline`
   - **Supported account types**: *Accounts in this organizational directory only* (Single tenant — Daios Hotels / Hellas Holiday Hotels SA)
   - **Redirect URI**: leave blank
3. Click **Register**.
4. On the app's Overview page, copy:
   - **Application (client) ID** → this is `MSGRAPH_CLIENT_ID`
   - **Directory (tenant) ID** → this is `MSGRAPH_TENANT_ID`
5. Go to **Authentication** (left sidebar):
   - Under *Advanced settings*, toggle **"Allow public client flows"** to **Yes**.
   - Click **Save**.
6. Go to **API permissions**:
   - Click **Add a permission** → **Microsoft Graph** → **Delegated permissions**.
   - Search and check: `Files.Read` and `offline_access`.
   - Click **Add permissions**.
   - (No admin consent button needed — these are user-delegated.)

## Part 2 — Capture a refresh token (~2 min)

Do this on your laptop once:

```bash
cd ~/daily-flash
source .venv/bin/activate
export MSGRAPH_CLIENT_ID=<client-id-from-step-4>
export MSGRAPH_TENANT_ID=<tenant-id-from-step-4>
python tools/auth_onedrive.py
```

The script prints a URL and a short code. Open the URL on any device, log in as `d.daios@daioshotels.com`, enter the code, grant the permissions. The refresh token prints at the end — **save it**, you'll paste it into Railway.

## Part 3 — Push to GitHub

If you don't already have a repo:

```bash
cd ~/daily-flash
git init
git add .
git commit -m "Daily Flash pipeline — initial"
# create a new GitHub repo via https://github.com/new (private recommended)
git remote add origin git@github.com:your-username/daily-flash.git
git branch -M main
git push -u origin main
```

**Crucially, make sure `.env` is gitignored** — it contains secrets. Add `.env` to `.gitignore` if not already (see check below).

## Part 4 — Railway project (~5 min)

1. Go to https://railway.com → **New Project** → **Deploy from GitHub repo** → pick your `daily-flash` repo.
2. Railway detects the Dockerfile and starts building.
3. Once built, go to the service → **Settings** → **Deploy**:
   - **Cron schedule**: `0 21 * * *` (if Railway doesn't auto-pick from `railway.toml`)
   - **Restart policy**: Never
4. **Variables** tab — add all of these:

```
SUPABASE_URL=https://iylnwafwrvzwkhhskazu.supabase.co
SUPABASE_SERVICE_ROLE_KEY=<from .env>
SUPABASE_ANON_KEY=<from .env>
ANTHROPIC_API_KEY=<from .env>
INGEST_FLASH_REPORT_URL=https://wgbghdbfmapuqbfeiygb.supabase.co/functions/v1/ingest-flash-report
PIPELINE_SECRET=<from .env>
MSGRAPH_CLIENT_ID=<from Part 1>
MSGRAPH_TENANT_ID=<from Part 1>
MSGRAPH_REFRESH_TOKEN=<from Part 2>
MSGRAPH_ONEDRIVE_FOLDER=DailyFlash
DAILY_FLASH_MODEL=claude-opus-4-7
DAILY_FLASH_CONCURRENCY=3
WEATHER_LAT=35.2401
WEATHER_LON=25.7500
WEATHER_TZ=Europe/Athens
TZ=Europe/Athens
```

5. Click **Deploy**.

## Part 5 — Test the first scheduled run

Option 1 — trigger manually from Railway UI:
- Service → click **Deploy** dropdown → **Trigger deploy** (runs once).

Option 2 — wait for 21:00 UTC.

Check the **Logs** tab. Expected output (~2-3 minutes):

```
[cron] pulled from OneDrive via Graph API: Daily Flash 22.04.2026.xlsx
[cron] pipeline — date=2026-04-22, file=/tmp/daily-flash/Daily Flash 22.04.2026.xlsx
=== DAILY PIPELINE (bridge) — report_date=2026-04-22 ===
[1/5] Parsed ... reservations
[2/5] Extracting from ... in-scope reservations... N/N ok
[3/5] A-lister: ... subjects
[5/5] Response: "ok": true, upload_id: ...
=== DONE ===
```

Then check `/dashboard?date=2026-04-22` in Lovable — should show the populated flash.

## Part 6 — Retire the local launchd agent

Once Railway is confirmed working for 2+ nights:

```bash
launchctl unload ~/Library/LaunchAgents/com.daioscove.dailyflash.plist
rm ~/Library/LaunchAgents/com.daioscove.dailyflash.plist
# Optional: remove the pmset wake schedule
# sudo pmset repeat cancel
```

## Troubleshooting

**Railway build fails on Python deps**: increase memory tier (cheapest paid plan is fine for ~90s/day runs).

**"OneDrive fetch failed: token refresh failed (400)"**: refresh token expired or revoked. Run `tools/auth_onedrive.py` again locally and paste the new token into Railway.

**"No .xlsx found in OneDrive folder 'DailyFlash'"**: Maria/Thelxi forgot to upload, or uploaded outside the target folder. Logs will tell you which.

**Pipeline runs but dashboard shows stale data**: check that `INGEST_FLASH_REPORT_URL` and `PIPELINE_SECRET` in Railway match the Lovable edge function and secret.

## Cost estimate

| Component | Monthly |
|---|---|
| Railway (cron job, ~5 min/day at ~128 MB) | ~$5 (Hobby plan) |
| Anthropic API (extraction + A-lister) | ~$30 avg, ~$100 peak (see earlier estimate) |
| Supabase (Lovable Cloud tier) | already included |
| Total new infra | **~$5/month** |
