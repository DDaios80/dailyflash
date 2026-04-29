"""Diagnostic: list what's actually visible in the OneDrive folders the
daily pipeline expects. Use when files have been moved/renamed and you
need to see what's there now.

Run from repo root with .env loaded:
    python tools/check_onedrive.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

# Make src/ importable
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from dotenv import load_dotenv  # type: ignore

load_dotenv(Path(__file__).parent.parent / ".env")

import requests  # noqa: E402

from onedrive import _config, _refresh_access_token, GraphError  # noqa: E402

_GRAPH = "https://graph.microsoft.com/v1.0"


def list_folder(folder_path: str, headers: dict) -> tuple[int, list[dict]]:
    """Returns (status_code, items). 404 means folder doesn't exist."""
    url = f"{_GRAPH}/me/drive/root:/{folder_path}:/children?$top=200"
    r = requests.get(url, headers=headers, timeout=30)
    if r.status_code == 404:
        return 404, []
    if r.status_code >= 400:
        return r.status_code, []
    return 200, (r.json().get("value") or [])


def main() -> int:
    cfg = _config()
    base = cfg["folder"]
    print(f"=== OneDrive diagnostic ===")
    print(f"Base folder (MSGRAPH_ONEDRIVE_FOLDER): {base!r}")
    print()

    headers = {"Authorization": f"Bearer {_refresh_access_token(cfg)}"}

    paths_to_check = [
        ("Base folder", base),
        ("Daily Flash subfolder", f"{base}/Daily Flash"),
        ("Birthdays subfolder", f"{base}/Birthdays"),
    ]

    # First — list OneDrive root so we can see where things actually live now.
    print("-- OneDrive root: /")
    root_url = f"{_GRAPH}/me/drive/root/children?$top=200"
    rr = requests.get(root_url, headers=headers, timeout=30)
    if rr.status_code == 200:
        items = rr.json().get("value") or []
        folders = [i for i in items if i.get("folder")]
        files = [i for i in items if not i.get("folder")]
        print(f"   {len(folders)} folders, {len(files)} files at root")
        if folders:
            print(f"   folders:")
            for it in sorted(folders, key=lambda i: i.get("name") or "")[:30]:
                print(f"     / {it.get('name')!r}")
    else:
        print(f"   [{rr.status_code}] couldn't list root: {rr.text[:200]}")
    print()

    any_broken = False

    for label, path in paths_to_check:
        print(f"-- {label}: /{path}")
        status, items = list_folder(path, headers)

        if status == 404:
            print(f"   [404] folder does not exist")
            any_broken = True
        elif status >= 400:
            print(f"   [{status}] error listing folder")
            any_broken = True
        else:
            xlsx = [i for i in items if (i.get("name") or "").lower().endswith(".xlsx")]
            folders = [i for i in items if i.get("folder")]
            other = [i for i in items if not i.get("folder") and not (i.get("name") or "").lower().endswith(".xlsx")]
            print(f"   [200] {len(xlsx)} xlsx, {len(folders)} subfolders, {len(other)} other items")

            if xlsx:
                print(f"   recent xlsx (latest 5):")
                xlsx_sorted = sorted(
                    xlsx,
                    key=lambda i: i.get("lastModifiedDateTime") or "",
                    reverse=True,
                )
                for it in xlsx_sorted[:5]:
                    print(f"     - {it.get('name')!r}  (modified {it.get('lastModifiedDateTime')})")
            else:
                print(f"   WARNING: no xlsx files in this folder")
                if "Birthdays" in path or "Daily Flash" in path:
                    any_broken = True

            if folders:
                print(f"   subfolders found:")
                for it in folders[:10]:
                    print(f"     / {it.get('name')!r}")
        print()

    if any_broken:
        print("RESULT: at least one expected path is broken.")
        print("Fix options:")
        print("  1. Move files back to the expected paths above, OR")
        print("  2. Update MSGRAPH_ONEDRIVE_FOLDER on Railway if base folder renamed, OR")
        print("  3. Update src/onedrive.py path constants (lines 178, 184, 236) and redeploy")
        return 1

    print("RESULT: all expected paths exist with at least one xlsx. Pipeline should work.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
