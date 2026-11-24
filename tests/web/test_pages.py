from __future__ import annotations

from tests.web.conftest import Ids


def test_each_page_ok(client, seeded: Ids):
    r = client.get("/")
    assert r.status_code == 307 and r.headers["location"] == "/inbox"
    inbox = client.get("/inbox")
    assert inbox.status_code == 200
    assert "Senior Data Engineer" in inbox.text and "Acme" in inbox.text
    assert "Analytics Engineer" in inbox.text and "via Remote OK" in inbox.text
    job = client.get(f"/jobs/{seeded.a_remoteok}")
    assert job.status_code == 200 and "Senior Data Engineer" in job.text
    assert "Контракт, удалённо." in job.text and "via Remotive" in job.text
    apps = client.get("/applications")
    assert apps.status_code == 200 and "Applications" in apps.text
    src = client.get("/sources")
    assert src.status_code == 200 and "remotive" in src.text and "himalayas" in src.text
    usage = client.get("/usage?days=30")
    assert usage.status_code == 200 and "haiku" in usage.text
    assert client.get("/healthz").text == "ok"


def test_group_member_redirects_to_canonical_and_unknown_is_404(client, seeded: Ids):
    r = client.get(f"/jobs/{seeded.a_remotive}")
    assert r.status_code == 307 and r.headers["location"] == f"/jobs/{seeded.a_remoteok}"
    assert client.get("/jobs/99999").status_code == 404


def test_inbox_dedupes_group_and_filters(client, seeded: Ids):
    text = client.get("/inbox").text
    assert text.count("Senior Data Engineer</a>") == 1
    only = client.get("/inbox?fit_min=80").text
    assert "Acme" in only and "Beta" not in only
    assert "Beta" in client.get("/inbox?source=remotive").text
    assert "Beta" not in client.get("/inbox?eligible=yes").text


def test_usage_shows_cap_and_rows(client):
    t = client.get("/usage?days=90").text
    assert "haiku" in t and "2026-11-06" in t and "0.010" in t
