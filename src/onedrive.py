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
from datetime import date
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


def _download_item(item: dict, headers: dict, target_dir: Path) -> Path:
    """Download a Graph item (xlsx) to target_dir and return the local path."""
    dl = item.get("@microsoft.graph.downloadUrl") or f"{_GRAPH}/me/drive/items/{item['id']}/content"
    target_dir.mkdir(parents=True, exist_ok=True)
    local_path = target_dir / item["name"]
    rr = requests.get(dl, headers=headers, timeout=180)
    if rr.status_code >= 400:
        raise GraphError(f"download failed ({rr.status_code}): {rr.text[:500]}")
    local_path.write_bytes(rr.content)
    return local_path


def _list_xlsx(folder_path: str, headers: dict) -> Optional[list[dict]]:
    """List xlsx items in a folder, sorted by lastModifiedDateTime desc.
    Returns None if folder doesn't exist."""
    list_url = (
        f"{_GRAPH}/me/drive/root:/{folder_path}:/children"
        "?$orderby=lastModifiedDateTime desc&$top=50"
        "&$select=id,name,lastModifiedDateTime,@microsoft.graph.downloadUrl"
    )
    r = requests.get(list_url, headers=headers, timeout=30)
    if r.status_code == 404:
        return None
    if r.status_code >= 400:
        raise GraphError(f"folder listing failed ({r.status_code}): {r.text[:500]}")
    items = r.json().get("value", [])
    return [i for i in items if i.get("name", "").lower().endswith(".xlsx")]


def _list_and_download_latest(folder_path: str, target_dir: Path) -> Optional[Path]:
    """Internal — list folder_path, download the most-recently-modified .xlsx.
    Returns None if folder doesn't exist or contains no xlsx. Raises GraphError
    on auth / network errors."""
    cfg = _config()
    token = _refresh_access_token(cfg)
    headers = {"Authorization": f"Bearer {token}"}
    xlsx = _list_xlsx(folder_path, headers)
    if xlsx is None or not xlsx:
        return None
    return _download_item(xlsx[0], headers, target_dir)


def _match_date_in_name(name: str, d: date) -> bool:
    """Does the filename contain a date stamp matching `d`?

    Accepts:
      - DD.MM.YYYY / DD-MM-YYYY / YYYY-MM-DD (year-bearing)
      - DD.MM / DD-MM (bare day-month — current OneDrive filename style for
        e.g. 'eur_birthday30.04.xlsx', 'Daily Flash 28.04.xlsx')
    """
    patterns = [
        f"{d.day:02d}.{d.month:02d}.{d.year:04d}",
        f"{d.day:02d}-{d.month:02d}-{d.year:04d}",
        f"{d.year:04d}-{d.month:02d}-{d.day:02d}",
        f"{d.day:02d}.{d.month:02d}",
        f"{d.day:02d}-{d.month:02d}",
    ]
    n = name.lower()
    return any(p.lower() in n for p in patterns)


def _fetch_dated_xlsx_with_fallback(
    folder_path: str,
    target_date: date,
    target_dir: Path,
) -> tuple[Optional[Path], bool]:
    """List folder_path, try to find an xlsx whose filename contains
    target_date (DD.MM.YYYY, DD-MM-YYYY, or YYYY-MM-DD). On hit → download
    and return (path, is_stale=False). On miss → download latest-modified
    and return (path, is_stale=True). On empty folder / missing folder →
    (None, False).
    """
    cfg = _config()
    token = _refresh_access_token(cfg)
    headers = {"Authorization": f"Bearer {token}"}
    xlsx = _list_xlsx(folder_path, headers)
    if xlsx is None or not xlsx:
        return (None, False)
    for item in xlsx:
        if _match_date_in_name(item.get("name", ""), target_date):
            return (_download_item(item, headers, target_dir), False)
    # Fall back to latest-modified
    return (_download_item(xlsx[0], headers, target_dir), True)


def fetch_latest_xlsx(target_dir: Path) -> Path:
    """Legacy: download the most-recently-modified .xlsx. Prefer
    fetch_daily_flash_for_date() for new code — it's date-aware.
    """
    cfg = _config()
    path = _list_and_download_latest(cfg["folder"], target_dir)
    if path:
        return path
    path = _list_and_download_latest(f"{cfg['folder']}/Daily Flash", target_dir)
    if path:
        return path
    raise GraphError(
        f"no .xlsx found in OneDrive folder '{cfg['folder']}' "
        f"or '{cfg['folder']}/Daily Flash'"
    )


def fetch_daily_flash_for_date(
    export_date: date, target_dir: Path,
) -> tuple[Path, bool]:
    """Download the Daily Flash xlsx whose filename matches `export_date`
    (DD.MM.YYYY). If no exact match, falls back to the latest-modified xlsx
    in the folder.

    Returns (local_path, is_stale). Caller should log a warning when is_stale
    is True — it means the operator hasn't uploaded today's export yet.

    Looks first in {folder}/Daily Flash, then falls back to the base folder
    for backward compatibility with the old flat layout.
    """
    cfg = _config()
    # Try "Daily Flash" subfolder (current layout)
    path, stale = _fetch_dated_xlsx_with_fallback(
        f"{cfg['folder']}/Daily Flash", export_date, target_dir,
    )
    if path:
        return (path, stale)
    # Fall back to base folder (old flat layout)
    path, stale = _fetch_dated_xlsx_with_fallback(
        cfg["folder"], export_date, target_dir,
    )
    if path:
        return (path, stale)
    raise GraphError(
        f"no .xlsx found in OneDrive folder '{cfg['folder']}' "
        f"or '{cfg['folder']}/Daily Flash'"
    )


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


def fetch_latest_xlsx_from_subfolder(subfolder: str, target_dir: Path) -> Optional[Path]:
    """Legacy: most-recently-modified .xlsx from a subfolder. Prefer
    fetch_birthdays_for_date() for the Birthdays subfolder.
    """
    cfg = _config()
    return _list_and_download_latest(
        f"{cfg['folder']}/{subfolder}".strip("/"), target_dir,
    )


def fetch_birthdays_for_date(
    export_date: date, target_dir: Path,
) -> tuple[Optional[Path], bool]:
    """Download the birthdays xlsx from DailyFlash/Birthdays/ matching
    `export_date`. Returns (path, is_stale). On empty/missing folder returns
    (None, False) — the birthdays file is optional."""
    cfg = _config()
    return _fetch_dated_xlsx_with_fallback(
        f"{cfg['folder']}/Birthdays", export_date, target_dir,
    )


# ─── Phase 28 — FAM trip PDFs ─────────────────────────────────────────────

def list_fam_trip_pdfs() -> list[dict]:
    """List PDFs in {folder}/FAM TRIPS/. Returns Graph item dicts with
    keys: id, name, lastModifiedDateTime, size, @microsoft.graph.downloadUrl.
    Skips non-PDF files (e.g. weekly xlsx report).
    Returns [] if folder doesn't exist or has no PDFs.
    """
    cfg = _config()
    token = _refresh_access_token(cfg)
    headers = {"Authorization": f"Bearer {token}"}
    folder_path = f"{cfg['folder']}/FAM TRIPS"
    list_url = (
        f"{_GRAPH}/me/drive/root:/{folder_path}:/children"
        "?$orderby=lastModifiedDateTime desc&$top=200"
        "&$select=id,name,lastModifiedDateTime,size,@microsoft.graph.downloadUrl"
    )
    r = requests.get(list_url, headers=headers, timeout=30)
    if r.status_code == 404:
        return []
    if r.status_code >= 400:
        raise GraphError(f"FAM TRIPS folder listing failed ({r.status_code}): {r.text[:500]}")
    items = (r.json().get("value") or [])
    return [it for it in items if (it.get("name") or "").lower().endswith(".pdf")]


def download_pdf_bytes(item: dict) -> bytes:
    """Download a Graph PDF item directly to memory. The pipeline streams
    the bytes to the ingest edge function via base64 — no local disk write."""
    cfg = _config()
    token = _refresh_access_token(cfg)
    headers = {"Authorization": f"Bearer {token}"}
    dl = item.get("@microsoft.graph.downloadUrl") or f"{_GRAPH}/me/drive/items/{item['id']}/content"
    rr = requests.get(dl, headers=headers, timeout=180)
    if rr.status_code >= 400:
        raise GraphError(f"PDF download failed ({rr.status_code}): {rr.text[:500]}")
    return rr.content
