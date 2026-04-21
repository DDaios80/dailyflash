"""Microsoft Graph client — fetch today's arrivals xlsx from OneDrive.

Uses OAuth2 refresh-token flow (delegated permissions). No tenant admin
consent required. The refresh token is captured once via `tools/auth_onedrive.py`
and stored in Railway env as MSGRAPH_REFRESH_TOKEN.

Permissions needed on the Azure AD app:
    Files.Read (delegated)
    offline_access (for refresh token)

Env vars:
    MSGRAPH_CLIENT_ID          Azure AD app (public client) ID
    MSGRAPH_TENANT_ID          Tenant ID or 'common' (default 'common')
    MSGRAPH_REFRESH_TOKEN      Captured via auth_onedrive.py
    MSGRAPH_ONEDRIVE_FOLDER    Path relative to root (default 'DailyFlash')
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

import requests


SCOPES = ["Files.Read", "offline_access"]
_GRAPH = "https://graph.microsoft.com/v1.0"


class GraphError(RuntimeError):
    pass


def _config() -> dict:
    cid = os.environ.get("MSGRAPH_CLIENT_ID") or ""
    tid = os.environ.get("MSGRAPH_TENANT_ID") or "common"
    rt = os.environ.get("MSGRAPH_REFRESH_TOKEN") or ""
    folder = os.environ.get("MSGRAPH_ONEDRIVE_FOLDER") or "DailyFlash"
    if not cid or not rt:
        raise GraphError(
            "MSGRAPH_CLIENT_ID and MSGRAPH_REFRESH_TOKEN must be set in env"
        )
    return {"client_id": cid, "tenant_id": tid, "refresh_token": rt, "folder": folder}


def _refresh_access_token(cfg: dict) -> str:
    """Exchange refresh_token → access_token via MS OAuth 2.0 endpoint."""
    url = f"https://login.microsoftonline.com/{cfg['tenant_id']}/oauth2/v2.0/token"
    r = requests.post(
        url,
        data={
            "client_id": cfg["client_id"],
            "grant_type": "refresh_token",
            "refresh_token": cfg["refresh_token"],
            "scope": " ".join(SCOPES),
        },
        timeout=30,
    )
    if r.status_code >= 400:
        raise GraphError(f"token refresh failed ({r.status_code}): {r.text[:500]}")
    data = r.json()
    if "access_token" not in data:
        raise GraphError(f"no access_token in response: {data}")
    return data["access_token"]


def fetch_latest_xlsx(target_dir: Path) -> Path:
    """Download the most-recently-modified .xlsx in the DailyFlash folder.

    Returns the local path to the downloaded file.
    """
    cfg = _config()
    token = _refresh_access_token(cfg)
    headers = {"Authorization": f"Bearer {token}"}

    list_url = (
        f"{_GRAPH}/me/drive/root:/{cfg['folder']}:/children"
        "?$orderby=lastModifiedDateTime desc&$top=25"
        "&$select=id,name,lastModifiedDateTime,@microsoft.graph.downloadUrl"
    )
    r = requests.get(list_url, headers=headers, timeout=30)
    if r.status_code >= 400:
        raise GraphError(f"folder listing failed ({r.status_code}): {r.text[:500]}")
    items = r.json().get("value", [])
    xlsx = [i for i in items if i.get("name", "").lower().endswith(".xlsx")]
    if not xlsx:
        raise GraphError(
            f"no .xlsx found in OneDrive folder '{cfg['folder']}' (listed {len(items)} items)"
        )
    latest = xlsx[0]  # already sorted desc by lastModifiedDateTime

    dl = latest.get("@microsoft.graph.downloadUrl")
    if not dl:
        dl = f"{_GRAPH}/me/drive/items/{latest['id']}/content"

    target_dir.mkdir(parents=True, exist_ok=True)
    local_path = target_dir / latest["name"]

    # Pre-authenticated download URL doesn't need the token; include it anyway
    # in case we fell back to the /content endpoint.
    rr = requests.get(dl, headers=headers, timeout=180)
    if rr.status_code >= 400:
        raise GraphError(f"download failed ({rr.status_code}): {rr.text[:500]}")
    local_path.write_bytes(rr.content)
    return local_path


def fetch_xlsx_by_name(name: str, target_dir: Path) -> Path:
    """Download a specific xlsx by filename, case-insensitive."""
    cfg = _config()
    token = _refresh_access_token(cfg)
    headers = {"Authorization": f"Bearer {token}"}

    list_url = f"{_GRAPH}/me/drive/root:/{cfg['folder']}:/children?$top=200"
    r = requests.get(list_url, headers=headers, timeout=30)
    r.raise_for_status()
    items = r.json().get("value", [])
    match = next((i for i in items if i.get("name", "").lower() == name.lower()), None)
    if not match:
        raise GraphError(f"xlsx '{name}' not found in OneDrive folder '{cfg['folder']}'")

    dl = match.get("@microsoft.graph.downloadUrl") or f"{_GRAPH}/me/drive/items/{match['id']}/content"
    target_dir.mkdir(parents=True, exist_ok=True)
    local_path = target_dir / match["name"]
    rr = requests.get(dl, headers=headers, timeout=180)
    rr.raise_for_status()
    local_path.write_bytes(rr.content)
    return local_path
