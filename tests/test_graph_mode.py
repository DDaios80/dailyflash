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
