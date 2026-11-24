from __future__ import annotations

import base64
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from cvboost import cli
from cvboost.config import Settings
from cvboost.web.app import create_app
from tests.conftest import NOW
from tests.web.conftest import Ids


def test_headers_on_pages_and_errors(client, seeded: Ids):
    for path in ("/inbox", "/usage", "/jobs/99999", "/healthz", f"/jobs/{seeded.b}"):
        r = client.get(path)
        csp = r.headers["content-security-policy"]
        assert (
            "unsafe-inline" not in csp and "unsafe-eval" not in csp and "default-src 'none'" in csp
        )
        for h in (
            "x-content-type-options",
            "referrer-policy",
            "x-frame-options",
            "cross-origin-opener-policy",
            "permissions-policy",
            "x-robots-tag",
        ):
            assert h in r.headers, (path, h)
    assert client.get("/inbox").headers["cache-control"] == "no-store"
    assert client.post("/inbox").status_code in (403, 405)


def test_trusted_host(client):
    assert client.get("/inbox", headers={"Host": "evil.example"}).status_code == 400


def auth_client(settings: Settings, seeded) -> TestClient:
    s = replace(settings, web_user="owner", web_password="pw-test")
    return TestClient(create_app(s, clock=lambda: NOW), follow_redirects=False)


def test_basic_auth(settings, seeded):
    c = auth_client(settings, seeded)
    r = c.get("/inbox")
    assert r.status_code == 401 and "Basic" in r.headers["www-authenticate"]
    assert "content-security-policy" in r.headers
    good = base64.b64encode(b"owner:pw-test").decode()
    bad = base64.b64encode(b"owner:nope").decode()
    assert c.get("/inbox", headers={"Authorization": f"Basic {good}"}).status_code == 200
    assert c.get("/inbox", headers={"Authorization": f"Basic {bad}"}).status_code == 401
    assert c.get("/inbox", headers={"Authorization": "Basic !!!"}).status_code == 401
    assert c.get("/healthz").status_code == 200


def test_half_configured_auth_rejected(settings, seeded, monkeypatch):
    with pytest.raises(ValueError):
        create_app(replace(settings, web_user="owner"))
    monkeypatch.setenv("CVBOOST_WEB_USER", "owner")
    monkeypatch.delenv("CVBOOST_WEB_PASSWORD", raising=False)
    with pytest.raises(ValueError):
        Settings.from_env()


def test_cli_refuses_open_listen(monkeypatch, tmp_path, capsys):
    started = []
    monkeypatch.setattr("uvicorn.run", lambda *a, **k: started.append((a, k)))
    monkeypatch.setenv("CVBOOST_DB", str(tmp_path / "c.sqlite"))
    for k in ("CVBOOST_WEB_USER", "CVBOOST_WEB_PASSWORD", "CVBOOST_WEB_TRUST_NETWORK"):
        monkeypatch.delenv(k, raising=False)
    assert cli.main(["web", "--host", "0.0.0.0"]) == 2
    assert not started
    monkeypatch.setenv("CVBOOST_WEB_USER", "o")
    assert cli.main(["web", "--host", "127.0.0.1"]) == 2  # only one of the pair
    monkeypatch.delenv("CVBOOST_WEB_USER")
    monkeypatch.setenv("CVBOOST_WEB_TRUST_NETWORK", "1")
    assert cli.main(["web", "--host", "0.0.0.0"]) == 0
    assert started and started[0][1]["workers"] == 1 and started[0][1]["proxy_headers"] is False
    started.clear()
    monkeypatch.delenv("CVBOOST_WEB_TRUST_NETWORK")
    assert cli.main(["web"]) == 0 and started[0][1]["host"] == "127.0.0.1"
