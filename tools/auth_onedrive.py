"""One-time interactive OAuth to capture an MS Graph refresh token.

Run locally (Mac/laptop with browser), once:
    export MSGRAPH_CLIENT_ID=<your-azure-app-client-id>
    export MSGRAPH_TENANT_ID=<your-tenant-id>  # or 'common'
    python tools/auth_onedrive.py

Flow: prints a short code + URL. Open URL on any device, log in as
d.daios@daioshotels.com (or whoever owns the OneDrive folder), enter the code,
grant the Files.Read + offline_access permissions. The refresh token prints
at the end — paste it into Railway as MSGRAPH_REFRESH_TOKEN.

Refresh tokens are long-lived (90 days rolling by default, but auto-refresh
whenever Railway actually uses them — so effectively perpetual as long as
the pipeline runs at least once every 90 days).
"""
from __future__ import annotations

import os
import sys
import time
import requests


SCOPES = ["Files.Read", "offline_access"]


def main() -> int:
    client_id = os.environ.get("MSGRAPH_CLIENT_ID")
    tenant_id = os.environ.get("MSGRAPH_TENANT_ID") or "common"
    if not client_id:
        print("ERROR: set MSGRAPH_CLIENT_ID env var first.", file=sys.stderr)
        print("  export MSGRAPH_CLIENT_ID=<your-azure-app-client-id>", file=sys.stderr)
        return 1

    # Step 1: initiate device-code flow
    dc_url = f"https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/devicecode"
    r = requests.post(dc_url, data={
        "client_id": client_id,
        "scope": " ".join(SCOPES),
    }, timeout=20)
    if r.status_code != 200:
        print(f"ERROR: device code request failed ({r.status_code}): {r.text}", file=sys.stderr)
        return 1
    dc = r.json()
    print("\n" + "=" * 60)
    print(dc["message"])
    print("=" * 60 + "\n")

    # Step 2: poll token endpoint
    token_url = f"https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token"
    interval = dc.get("interval", 5)
    deadline = time.time() + dc.get("expires_in", 900)
    while time.time() < deadline:
        time.sleep(interval)
        rr = requests.post(token_url, data={
            "client_id": client_id,
            "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
            "device_code": dc["device_code"],
        }, timeout=20)
        data = rr.json()
        if "access_token" in data:
            print("\n=== SUCCESS ===")
            print("\nPaste this into Railway env as MSGRAPH_REFRESH_TOKEN:\n")
            print(data.get("refresh_token"))
            print()
            print("Also set (if not already):")
            print(f"  MSGRAPH_CLIENT_ID={client_id}")
            print(f"  MSGRAPH_TENANT_ID={tenant_id}")
            print("  MSGRAPH_ONEDRIVE_FOLDER=DailyFlash  (or your folder path)")
            return 0
        err = data.get("error")
        if err == "authorization_pending":
            continue
        if err == "slow_down":
            interval += 5
            continue
        print(f"\nauth failed: {err}: {data.get('error_description')}", file=sys.stderr)
        return 1
    print("timed out waiting for user auth", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
