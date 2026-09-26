"""Microsoft Graph client — fetch today's arrivals xlsx from OneDrive.

Uses OAuth2 refresh-token flow (delegated permissions). No tenant admin
consent required. The refresh token is captured once via `tools/auth_onedrive.py`
and stored in Railway env as MSGRAPH_REFRESH_TOKEN.

2026-07-22 — SELF-RENEWING TOKEN. Azure rotates the refresh token on every
use and expires any single token string 90 days after issuance, even if
presented daily (proven by the 2026-07-20 outage: the static env token from
2026-04-21 died mid-July while in nightly use). We now persist the newest
rotated token in app_settings (key: msgraph_refresh_token) via the supa
client and prefer it over the env var, so the 90-day clock resets on every
run. The env var remains the bootstrap + fallback: if the persisted token is
dead (e.g. after a manual re-auth), the env token is tried and its rotation
re-seeds the DB. Persistence is best-effort and never fails the pipeline.

Permissions needed on the Azure AD app:
    Files.Read (delegated)
    offline_access (for refresh token)

Env vars:
    MSGRAPH_CLIENT_ID          Azure AD app (public client) ID
    MSGRAPH_TENANT_ID          Tenant ID or 'common' (default 'common')
    MSGRAPH_REFRESH_TOKEN      Captured via auth_onedrive.py
    MSGRAPH_ONEDRIVE_FOLDER    Path relative to root (default 'DailyFlash')

2026-09-25 — APP MODE (off the personal account). When MSGRAPH_APP_CLIENT_ID,
MSGRAPH_APP_CLIENT_SECRET and MSGRAPH_DRIVE_ID are all set, the module signs
in as the app itself (client credentials, Graph application permission
Sites.Selected, granted read on one SharePoint site) and reads that site's
document library instead of /me/drive. MSGRAPH_TENANT_ID must then be the
tenant GUID, and MSGRAPH_ONEDRIVE_FOLDER is the folder path inside the
library. Without those three vars the delegated refresh-token flow above is
used unchanged.

2026-09-26 — SWITCH-OVER + ONE-WEEK SAFETY NET. In app mode the folder inside the
site library is MSGRAPH_SITE_FOLDER (default "Daily Flash"); MSGRAPH_ONEDRIVE_FOLDER
keeps naming the old personal-OneDrive folder. Until FALLBACK_UNTIL, when the
delegated credentials are still present, the daily xlsx and birthdays fall back
to the personal OneDrive if the site has no file for the date, and the PDF
listings include both places (ingest dedups server-side). After that date the
fallback switches itself off; then remove MSGRAPH_REFRESH_TOKEN.
"""
from __future__ import annotations

import json
import os
from contextlib import contextmanager
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Optional

import requests


SCOPES = ["Files.Read", "offline_access"]
_GRAPH = "https://graph.microsoft.com/v1.0"

# app_settings key holding the newest rotated refresh token (see module
# docstring). Read/written via the service-role supa client only.
_DB_TOKEN_KEY = "msgraph_refresh_token"


class GraphError(RuntimeError):
    pass


FALLBACK_UNTIL = date(2026, 10, 3)
_FORCE_LEGACY = False


def _app_creds() -> bool:
    return all(os.environ.get(k) for k in
               ("MSGRAPH_APP_CLIENT_ID", "MSGRAPH_APP_CLIENT_SECRET", "MSGRAPH_DRIVE_ID"))


def _app_mode() -> bool:
    return _app_creds() and not _FORCE_LEGACY


def _fallback_active() -> bool:
    """Transition week: app mode is on, but the personal OneDrive can still be read."""
    return (_app_creds() and date.today() <= FALLBACK_UNTIL
            and bool(os.environ.get("MSGRAPH_CLIENT_ID") and os.environ.get("MSGRAPH_REFRESH_TOKEN")))


@contextmanager
def _legacy():
    """Temporarily read the personal OneDrive (delegated flow) while in app mode."""
    global _FORCE_LEGACY
    prev, _FORCE_LEGACY = _FORCE_LEGACY, True
    try:
        yield
    finally:
        _FORCE_LEGACY = prev


def configured() -> bool:
    """True when either Graph mode has its credentials (cron.py gate)."""
    return _app_mode() or bool(
        os.environ.get("MSGRAPH_CLIENT_ID") and os.environ.get("MSGRAPH_REFRESH_TOKEN"))


def _drive() -> str:
    """Graph base URL of the drive we read: the site library in app mode,
    the signed-in user's OneDrive otherwise."""
    if _app_mode():
        return f"{_GRAPH}/drives/{os.environ['MSGRAPH_DRIVE_ID']}"
    return f"{_GRAPH}/me/drive"


_app_token: dict = {}


def _load_persisted_token() -> Optional[str]:
    """Best-effort read of the newest rotated refresh token. None on any
    failure — callers fall back to the env token."""
    try:
        from supa import client as _supa_client
        row = (
            _supa_client()
            .table("app_settings")
            .select("value")
            .eq("key", _DB_TOKEN_KEY)
            .maybe_single()
            .execute()
        )
        val = ((getattr(row, "data", None) or {}).get("value") or "").strip()
        return val or None
    except Exception as e:
        print(f"[graph-token] persisted-token read failed (using env): "
              f"{type(e).__name__}: {e}")
        return None


def _persist_rotated_token(new_rt: str) -> None:
    """Best-effort upsert of the rotated refresh token. Loud on failure
    (same rationale as the Phase 47 heartbeat) but never raises."""
    try:
        from supa import client as _supa_client
        _supa_client().table("app_settings").upsert(
            {"key": _DB_TOKEN_KEY, "value": new_rt},
            on_conflict="key",
        ).execute()
        print(f"[graph-token] rotated refresh token persisted "
              f"({len(new_rt)} chars)")
    except Exception as e:
        print(f"[graph-token] FAILED to persist rotated token (non-fatal, "
              f"but the 90-day clock is NOT reset): {type(e).__name__}: {e}")


def _config() -> dict:
    if _app_mode():
        return {"app": True, "folder": os.environ.get("MSGRAPH_SITE_FOLDER") or "Daily Flash"}
    cid = os.environ.get("MSGRAPH_CLIENT_ID") or ""
    tid = os.environ.get("MSGRAPH_TENANT_ID") or "common"
    env_rt = os.environ.get("MSGRAPH_REFRESH_TOKEN") or ""
    folder = os.environ.get("MSGRAPH_ONEDRIVE_FOLDER") or "DailyFlash"
    if not cid or not env_rt:
        raise GraphError(
            "MSGRAPH_CLIENT_ID and MSGRAPH_REFRESH_TOKEN must be set in env"
        )
    # Prefer the newest rotated token (self-renewing); env is bootstrap +
    # fallback when the persisted one is missing or dead.
    db_rt = _load_persisted_token()
    rt = db_rt or env_rt
    fallback = env_rt if (db_rt and db_rt != env_rt) else ""
    return {
        "client_id": cid, "tenant_id": tid, "folder": folder,
        "refresh_token": rt, "fallback_refresh_token": fallback,
    }


def _token_request(cfg: dict, refresh_token: str) -> requests.Response:
    url = f"https://login.microsoftonline.com/{cfg['tenant_id']}/oauth2/v2.0/token"
    return requests.post(
        url,
        data={
            "client_id": cfg["client_id"],
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "scope": " ".join(SCOPES),
        },
        timeout=30,
    )


def _refresh_access_token(cfg: dict) -> str:
    """Exchange refresh_token → access_token via MS OAuth 2.0 endpoint.

    Tries the primary (persisted) token first; if Azure rejects it and an
    env fallback exists, retries once with that (covers a dead DB token
    after a manual re-auth). Persists the rotated refresh token returned
    by whichever attempt succeeds, so the 90-day clock keeps resetting.

    In app mode: client-credentials token, cached until 5 min before expiry."""
    if cfg.get("app"):
        now = datetime.now(timezone.utc).timestamp()
        if _app_token.get("exp", 0) > now + 300:
            return _app_token["token"]
        tid = os.environ.get("MSGRAPH_TENANT_ID") or ""
        r = requests.post(
            f"https://login.microsoftonline.com/{tid}/oauth2/v2.0/token",
            data={"client_id": os.environ["MSGRAPH_APP_CLIENT_ID"],
                  "client_secret": os.environ["MSGRAPH_APP_CLIENT_SECRET"],
                  "grant_type": "client_credentials",
                  "scope": "https://graph.microsoft.com/.default"},
            timeout=30,
        )
        if r.status_code >= 400:
            raise GraphError(f"app token request failed ({r.status_code}): {r.text[:500]}")
        data = r.json()
        _app_token.update(token=data["access_token"], exp=now + int(data.get("expires_in", 3600)))
        return data["access_token"]
    used_rt = cfg["refresh_token"]
    r = _token_request(cfg, used_rt)
    if r.status_code >= 400 and cfg.get("fallback_refresh_token"):
        print(f"[graph-token] persisted token rejected "
              f"({r.status_code}) — retrying with env token")
        used_rt = cfg["fallback_refresh_token"]
        r = _token_request(cfg, used_rt)
    if r.status_code >= 400:
        raise GraphError(f"token refresh failed ({r.status_code}): {r.text[:500]}")
    data = r.json()
    if "access_token" not in data:
        raise GraphError(f"no access_token in response: {data}")
    new_rt = (data.get("refresh_token") or "").strip()
    if new_rt and new_rt != used_rt:
        _persist_rotated_token(new_rt)
    return data["access_token"]


def _download_item(item: dict, headers: dict, target_dir: Path) -> Path:
    """Download a Graph item (xlsx) to target_dir and return the local path.

    Sets the local file's mtime to OneDrive's lastModifiedDateTime so
    change-detection (cron.py --if-new / --auto-quick) compares the SOURCE's
    modification time, not the moment we happened to download it."""
    dl = item.get("@microsoft.graph.downloadUrl") or f"{_drive()}/items/{item['id']}/content"
    target_dir.mkdir(parents=True, exist_ok=True)
    local_path = target_dir / item["name"]
    rr = requests.get(dl, headers=headers, timeout=180)
    if rr.status_code >= 400:
        raise GraphError(f"download failed ({rr.status_code}): {rr.text[:500]}")
    local_path.write_bytes(rr.content)
    lm = item.get("lastModifiedDateTime")
    if lm:
        try:
            ts = datetime.fromisoformat(lm.replace("Z", "+00:00")).timestamp()
            os.utime(local_path, (ts, ts))
        except Exception:
            pass  # best-effort; a wrong mtime only weakens change-detection
    return local_path


def _list_xlsx(folder_path: str, headers: dict) -> Optional[list[dict]]:
    """List xlsx items in a folder, sorted by lastModifiedDateTime desc.
    Returns None if folder doesn't exist."""
    list_url = (
        f"{_drive()}/root:/{folder_path}:/children"
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


def _fetch_daily_flash_for_date(
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

    list_url = f"{_drive()}/root:/{cfg['folder']}:/children?$top=200"
    r = requests.get(list_url, headers=headers, timeout=30)
    r.raise_for_status()
    items = r.json().get("value", [])
    match = next((i for i in items if i.get("name", "").lower() == name.lower()), None)
    if not match:
        raise GraphError(f"xlsx '{name}' not found in OneDrive folder '{cfg['folder']}'")

    dl = match.get("@microsoft.graph.downloadUrl") or f"{_drive()}/items/{match['id']}/content"
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


def _fetch_birthdays_for_date(
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

def _list_fam_trip_pdfs() -> list[dict]:
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
        f"{_drive()}/root:/{folder_path}:/children"
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


# ─── Phase 44 — Site inspection PDFs ──────────────────────────────────────

def _list_site_inspection_pdfs() -> list[dict]:
    """List PDFs in {folder}/SITE INSPECTIONS/. Mirrors list_fam_trip_pdfs.
    Skips non-PDF files (.msg/.eml Outlook exports are out of scope for now).
    Returns [] if folder doesn't exist or has no PDFs.
    """
    cfg = _config()
    token = _refresh_access_token(cfg)
    headers = {"Authorization": f"Bearer {token}"}
    folder_path = f"{cfg['folder']}/SITE INSPECTIONS"
    list_url = (
        f"{_drive()}/root:/{folder_path}:/children"
        "?$orderby=lastModifiedDateTime desc&$top=200"
        "&$select=id,name,lastModifiedDateTime,size,@microsoft.graph.downloadUrl"
    )
    r = requests.get(list_url, headers=headers, timeout=30)
    if r.status_code == 404:
        return []
    if r.status_code >= 400:
        raise GraphError(f"SITE INSPECTIONS folder listing failed ({r.status_code}): {r.text[:500]}")
    items = (r.json().get("value") or [])
    return [it for it in items if (it.get("name") or "").lower().endswith(".pdf")]


# ─── Phase 14b — Group PDFs ──────────────────────────────────────────────

def _list_group_pdfs() -> list[dict]:
    """List PDFs in {folder}/GROUPS/. Mirrors list_site_inspection_pdfs.
    Folder holds mixed group types: tour groups, weddings, corporate retreats,
    MICE/conferences, etc. The ingest edge function classifies type from
    filename + content.
    Returns [] if folder doesn't exist or has no PDFs.
    """
    cfg = _config()
    token = _refresh_access_token(cfg)
    headers = {"Authorization": f"Bearer {token}"}
    folder_path = f"{cfg['folder']}/GROUPS"
    list_url = (
        f"{_drive()}/root:/{folder_path}:/children"
        "?$orderby=lastModifiedDateTime desc&$top=200"
        "&$select=id,name,lastModifiedDateTime,size,@microsoft.graph.downloadUrl"
    )
    r = requests.get(list_url, headers=headers, timeout=30)
    if r.status_code == 404:
        return []
    if r.status_code >= 400:
        raise GraphError(f"GROUPS folder listing failed ({r.status_code}): {r.text[:500]}")
    items = (r.json().get("value") or [])
    return [it for it in items if (it.get("name") or "").lower().endswith(".pdf")]


def _download_pdf_bytes(item: dict) -> bytes:
    """Download a Graph PDF item directly to memory. The pipeline streams
    the bytes to the ingest edge function via base64 — no local disk write."""
    cfg = _config()
    token = _refresh_access_token(cfg)
    headers = {"Authorization": f"Bearer {token}"}
    dl = item.get("@microsoft.graph.downloadUrl") or f"{_drive()}/items/{item['id']}/content"
    rr = requests.get(dl, headers=headers, timeout=180)
    if rr.status_code >= 400:
        raise GraphError(f"PDF download failed ({rr.status_code}): {rr.text[:500]}")
    return rr.content


# ─── 2026-09-26 — public entry points with the transition-week fallback ────

def fetch_daily_flash_for_date(export_date: date, target_dir: Path) -> tuple[Path, bool]:
    """Site first; during the transition week, the personal OneDrive when the
    site has no file for `export_date` (see module docstring)."""
    site, err = None, None
    try:
        site = _fetch_daily_flash_for_date(export_date, target_dir)
    except GraphError as e:
        err = e
    if _fallback_active() and (site is None or site[1]):
        try:
            with _legacy():
                old = _fetch_daily_flash_for_date(export_date, target_dir)
            if site is None or not old[1]:
                print(f"[graph] transition fallback: using personal OneDrive file {old[0].name}")
                return old
        except GraphError as e:
            print(f"[graph] transition fallback failed: {e}")
    if site is None:
        raise err
    return site


def fetch_birthdays_for_date(export_date: date, target_dir: Path) -> tuple[Optional[Path], bool]:
    path, stale = _fetch_birthdays_for_date(export_date, target_dir)
    if _fallback_active() and (path is None or stale):
        try:
            with _legacy():
                old, old_stale = _fetch_birthdays_for_date(export_date, target_dir)
            if old and (path is None or not old_stale):
                print(f"[graph] transition fallback: birthdays from personal OneDrive {old.name}")
                return old, old_stale
        except GraphError as e:
            print(f"[graph] transition fallback (birthdays) failed: {e}")
    return path, stale


# PDF names that already existed on the personal OneDrive at the switch-over
# (2026-09-26). Copies of them on the site get new item ids, and the group /
# site-inspection ingest dedups by item id, so they are skipped for good.
_PRE_SWITCH = json.loads((Path(__file__).with_name("pre_switch_pdfs.json")).read_text(encoding="utf-8"))


def _both(lister) -> list[dict]:
    known = set(_PRE_SWITCH.get(lister.__name__.strip("_"), []))
    items = [it for it in lister() if it.get("name") not in known]
    if _fallback_active():
        try:
            with _legacy():
                items += [dict(it, _legacy=True) for it in lister()]
        except GraphError as e:
            print(f"[graph] transition fallback listing failed: {e}")
    return items


def list_fam_trip_pdfs() -> list[dict]:
    return _both(_list_fam_trip_pdfs)


def list_site_inspection_pdfs() -> list[dict]:
    return _both(_list_site_inspection_pdfs)


def list_group_pdfs() -> list[dict]:
    return _both(_list_group_pdfs)


def download_pdf_bytes(item: dict) -> bytes:
    if item.get("_legacy"):
        with _legacy():
            return _download_pdf_bytes(item)
    return _download_pdf_bytes(item)
