"""Graph mode switch: delegated /me/drive by default, site library in app mode."""
import onedrive


def test_default_is_personal_onedrive(monkeypatch):
    for k in ("MSGRAPH_APP_CLIENT_ID", "MSGRAPH_APP_CLIENT_SECRET", "MSGRAPH_DRIVE_ID"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("MSGRAPH_CLIENT_ID", "cid")
    monkeypatch.setenv("MSGRAPH_REFRESH_TOKEN", "rt")
    assert onedrive._drive().endswith("/me/drive")
    assert onedrive.configured()


def test_app_mode_reads_site_library(monkeypatch):
    monkeypatch.delenv("MSGRAPH_REFRESH_TOKEN", raising=False)
    monkeypatch.setenv("MSGRAPH_APP_CLIENT_ID", "app")
    monkeypatch.setenv("MSGRAPH_APP_CLIENT_SECRET", "secret")
    monkeypatch.setenv("MSGRAPH_DRIVE_ID", "b!drive")
    monkeypatch.setenv("MSGRAPH_TENANT_ID", "tenant")
    assert onedrive._drive() == "https://graph.microsoft.com/v1.0/drives/b!drive"
    assert onedrive.configured()
    assert onedrive._config()["app"] is True

    calls = []

    class R:
        status_code = 200
        def json(self):
            return {"access_token": "tok", "expires_in": 3600}

    def fake_post(url, data, timeout):
        calls.append((url, data["grant_type"], data["scope"]))
        return R()

    monkeypatch.setattr(onedrive.requests, "post", fake_post)
    onedrive._app_token.clear()
    assert onedrive._refresh_access_token(onedrive._config()) == "tok"
    assert onedrive._refresh_access_token(onedrive._config()) == "tok"  # cached
    assert calls == [("https://login.microsoftonline.com/tenant/oauth2/v2.0/token",
                      "client_credentials", "https://graph.microsoft.com/.default")]


def _app_env(monkeypatch, legacy=True):
    monkeypatch.setenv("MSGRAPH_APP_CLIENT_ID", "app")
    monkeypatch.setenv("MSGRAPH_APP_CLIENT_SECRET", "secret")
    monkeypatch.setenv("MSGRAPH_DRIVE_ID", "b!drive")
    monkeypatch.setenv("MSGRAPH_TENANT_ID", "tenant")
    if legacy:
        monkeypatch.setenv("MSGRAPH_CLIENT_ID", "cid")
        monkeypatch.setenv("MSGRAPH_REFRESH_TOKEN", "rt")
    else:
        monkeypatch.delenv("MSGRAPH_REFRESH_TOKEN", raising=False)


def test_site_folder_default_and_legacy_folder(monkeypatch):
    _app_env(monkeypatch)
    monkeypatch.setenv("MSGRAPH_ONEDRIVE_FOLDER", "Daios Cove/DailyFlash")
    monkeypatch.delenv("MSGRAPH_SITE_FOLDER", raising=False)
    monkeypatch.setattr(onedrive, "_load_persisted_token", lambda: None)
    assert onedrive._config()["folder"] == "Daily Flash"
    with onedrive._legacy():
        assert onedrive._drive().endswith("/me/drive")
        assert onedrive._config()["folder"] == "Daios Cove/DailyFlash"
    assert "/drives/b!drive" in onedrive._drive()


def _fake_fetch(site, old):
    from pathlib import Path
    def f(d, target):
        res = old if onedrive._FORCE_LEGACY else site
        if isinstance(res, Exception):
            raise res
        return (Path(res[0]), res[1])
    return f


def test_fallback_used_when_site_has_no_file_for_the_date(monkeypatch, tmp_path):
    from datetime import date
    _app_env(monkeypatch)
    monkeypatch.setattr(onedrive, "FALLBACK_UNTIL", date(2099, 1, 1))
    monkeypatch.setattr(onedrive, "_fetch_daily_flash_for_date",
                        _fake_fetch(("site-old.xlsx", True), ("Daily Flash 27.09.2026.xlsx", False)))
    p, stale = onedrive.fetch_daily_flash_for_date(date(2026, 9, 27), tmp_path)
    assert (p.name, stale) == ("Daily Flash 27.09.2026.xlsx", False)


def test_site_wins_when_it_has_the_file(monkeypatch, tmp_path):
    from datetime import date
    _app_env(monkeypatch)
    monkeypatch.setattr(onedrive, "FALLBACK_UNTIL", date(2099, 1, 1))
    monkeypatch.setattr(onedrive, "_fetch_daily_flash_for_date",
                        _fake_fetch(("site.xlsx", False), ("old.xlsx", False)))
    p, stale = onedrive.fetch_daily_flash_for_date(date(2026, 9, 27), tmp_path)
    assert p.name == "site.xlsx"


def test_empty_site_falls_back_and_expired_fallback_raises(monkeypatch, tmp_path):
    from datetime import date
    _app_env(monkeypatch)
    monkeypatch.setattr(onedrive, "_fetch_daily_flash_for_date",
                        _fake_fetch(onedrive.GraphError("no .xlsx"), ("old.xlsx", False)))
    monkeypatch.setattr(onedrive, "FALLBACK_UNTIL", date(2099, 1, 1))
    assert onedrive.fetch_daily_flash_for_date(date(2026, 9, 27), tmp_path)[0].name == "old.xlsx"
    monkeypatch.setattr(onedrive, "FALLBACK_UNTIL", date(2000, 1, 1))
    import pytest
    with pytest.raises(onedrive.GraphError):
        onedrive.fetch_daily_flash_for_date(date(2026, 9, 27), tmp_path)


def test_pdf_listing_merges_both_places_during_transition(monkeypatch):
    from datetime import date
    _app_env(monkeypatch)
    monkeypatch.setattr(onedrive, "FALLBACK_UNTIL", date(2099, 1, 1))
    def _list_group_pdfs():
        if onedrive._FORCE_LEGACY:
            return [{"id": "old1", "name": "a.pdf"}]
        return [{"id": "new1", "name": "b.pdf"}, {"id": "copy", "name": "a.pdf"}]
    monkeypatch.setattr(onedrive, "_list_group_pdfs", _list_group_pdfs)
    monkeypatch.setattr(onedrive, "_PRE_SWITCH", {"list_group_pdfs": ["a.pdf"]})
    items = onedrive.list_group_pdfs()
    # the site copy of a pre-switch PDF is skipped; the OneDrive original is listed during the week
    assert [(i["id"], bool(i.get("_legacy"))) for i in items] == [("new1", False), ("old1", True)]
