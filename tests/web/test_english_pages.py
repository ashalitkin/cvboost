"""#70b: every core page and HTMX partial is English; Cyrillic may only be stored data."""

from __future__ import annotations

import re

import pytest

from cvboost.crm import store as S
from cvboost.crm.types import ParsedEvent
from cvboost.storage.db import migrate
from tests.conftest import NOW
from tests.web import conftest as C
from tests.web.conftest import Ids, csrf, db
from tests.web.test_crm import add_event
from tests.web.test_decisions import post

_RAW = re.compile("[\u0400-\u04ff]")
# the private outbound plugin adds its own nav link to every page; its UI is #70c
_OUTBOUND_NAV = re.compile(r'<a href="/outbound"[^>]*>[^<]*</a>')


class _Cyr:
    @staticmethod
    def findall(text: str) -> list[str]:
        return _RAW.findall(_OUTBOUND_NAV.sub("", text))

    @staticmethod
    def search(text: str):
        return _RAW.search(_OUTBOUND_NAV.sub("", text))


CYRILLIC = _Cyr()
_real_verdict = C.verdict


@pytest.fixture(autouse=True)
def english_data(monkeypatch):
    """Runs before `seeded`: the LLM summaries in the fixture DB are English too."""

    def verdict(**kw):
        kw.setdefault("summary_ru", "Contract, remote.")
        return _real_verdict(**kw)

    monkeypatch.setattr(C, "verdict", verdict)


def assert_english(resp, what=""):
    assert resp.status_code == 200, (what, resp.status_code)
    found = CYRILLIC.findall(resp.text)
    assert found == [], f"{what}: Cyrillic {''.join(found)[:40]!r}"


def htmx(client, method, path, **kw):
    headers = {"HX-Request": "true", **kw.pop("headers", {})}
    if method == "post":
        kw["data"] = {"csrf_token": csrf(client), **kw.pop("data", {})}
    return getattr(client, method)(path, headers=headers, **kw)


def test_html_lang_is_english(client, seeded: Ids):
    assert '<html lang="en">' in client.get("/inbox").text


@pytest.mark.parametrize(
    "path",
    [
        "/inbox",
        "/inbox?decision=all",
        "/inbox?eligible=rejected",
        "/inbox?sort=new&unseen=1",
        "/screened",
        "/applications",
        "/sources",
        "/usage?days=30",
        "/stats",
        "/stats?period=7d",
        "/crm",
        "/crm?archive=1",
    ],
)
def test_list_pages_have_no_cyrillic(client, seeded: Ids, path):
    assert_english(client.get(path), path)


def test_job_pages_and_kit_blocks(client, seeded: Ids, settings):
    for cid in (seeded.a_remoteok, seeded.b, seeded.xss, seeded.mixed_alive):
        assert_english(client.get(f"/jobs/{cid}"), f"/jobs/{cid}")
    post(client, f"/jobs/{seeded.b}/decision", action="prepare_cv")
    assert_english(client.get(f"/jobs/{seeded.b}"), "job with a CV request")
    assert_english(client.get("/applications"), "applications with a card")
    db(settings).conn.execute("UPDATE cv_requests SET status='in_progress'")
    assert_english(client.get(f"/jobs/{seeded.b}"), "job with a kit in progress")


def test_error_pages_are_english(client, seeded: Ids):
    r = client.get("/jobs/99999")
    assert r.status_code == 404 and CYRILLIC.search(r.text) is None
    assert "Job not found." in r.text
    bad = client.post(f"/jobs/{seeded.b}/decision", data={"action": "interested"})
    assert bad.status_code == 403 and CYRILLIC.search(bad.text) is None
    assert "Invalid CSRF token" in bad.text


def test_validation_errors_are_english(client, seeded: Ids):
    cid = seeded.b
    cases = [
        (f"/jobs/{cid}/decision", {"action": "explode"}, "Unknown action."),
        (f"/jobs/{cid}/decision", {"action": "rejected"}, "A reason is required for No."),
        (f"/jobs/{cid}/status", {"status": "nope"}, "Unknown status."),
        (f"/applications/{cid}/withdraw", {"reason": ""}, "A reason is required for Withdrawn."),
    ]
    for path, data, text in cases:
        r = post(client, path, **data)
        assert r.status_code == 400 and text in r.text, (path, r.text[:200])
        assert CYRILLIC.search(r.text) is None


def test_htmx_partials_are_english(client, seeded: Ids, settings):
    rows = htmx(client, "get", "/inbox", headers={"HX-Target": "rows"})
    assert_english(rows, "inbox rows partial")
    rows = htmx(client, "get", "/screened", headers={"HX-Target": "rows"})
    assert_english(rows, "screened rows partial")
    # decisions from a row: the partial is the re-rendered row or empty
    r = htmx(client, "post", f"/jobs/{seeded.a_remoteok}/decision", data={"action": "interested"})
    assert r.status_code == 200 and CYRILLIC.search(r.text) is None
    assert "★ Interested" in r.text
    r = htmx(client, "post", f"/jobs/{seeded.a_remoteok}/decision", data={"action": "undo"})
    assert r.status_code == 200 and CYRILLIC.search(r.text) is None


def test_post_flows_render_english_pages(client, seeded: Ids, settings):
    cid = seeded.b
    assert post(client, f"/jobs/{cid}/decision", action="prepare_cv").status_code == 303
    r = post(client, f"/applications/{cid}/withdraw", reason="stack", note="needs paid account")
    assert r.status_code == 303
    page = client.get("/applications")
    assert_english(page, "applications after withdraw")
    assert "Withdrawn" in page.text and "needs paid account" in page.text
    assert post(client, f"/applications/{cid}/restore").status_code == 303
    r = post(client, f"/jobs/{cid}/status", status="applied", note="sent")
    assert r.status_code == 303
    page = client.get("/applications")
    assert_english(page, "applications after status change")
    assert "Applied" in page.text
    assert_english(client.get(f"/jobs/{cid}"), "job after status change")


def test_option_values_are_still_codes(client, seeded: Ids):
    page = client.get("/inbox").text
    assert '<option value="stack">Stack</option>' in page
    assert '<option value="rate">Rate</option>' in page
    assert '<option value="location">Location</option>' in page
    assert '<option value="other">Other</option>' in page
    job = client.get(f"/jobs/{seeded.b}").text
    assert 'name="status"' in job
    for code in ("applied", "replied", "interview", "rejected", "offer"):
        assert f'value="{code}"' in job


def test_crm_pages_are_english(settings):
    from fastapi.testclient import TestClient

    from cvboost.web.app import create_app

    c = TestClient(create_app(settings, clock=lambda: NOW), follow_redirects=False)
    cid = add_event(settings, "Alice Sample", kind="message", snippet="hello")
    assert_english(c.get("/crm"), "/crm")
    assert_english(c.get(f"/crm/{cid}"), "/crm/<id>")
    assert c.get("/crm/99999").status_code == 404
    assert "Contact not found." in c.get("/crm/99999").text


def test_unnamed_linkedin_contact_has_an_english_name(settings):
    st = db(settings)
    migrate(st.conn)
    with st.transaction():
        S.record_event(st.conn, ParsedEvent(name="", kind="message", snippet="x"), NOW, "m1", NOW)
    assert st.conn.execute("SELECT name FROM crm_contacts").fetchone()[0] == "LinkedIn (unparsed)"


def test_old_russian_stored_data_is_shown_as_escaped_data(client, seeded: Ids, settings):
    """Notes and summaries written before #70 stay Russian: shown as data, escaped, no error."""
    cid = seeded.b
    post(client, f"/jobs/{cid}/decision", action="prepare_cv")
    r = post(client, f"/jobs/{cid}/status", status="applied", note="Откликнулся <b>вчера</b>")
    assert r.status_code == 303
    job = client.get(f"/jobs/{cid}")
    assert job.status_code == 200
    assert "Откликнулся &lt;b&gt;вчера&lt;/b&gt;" in job.text and "<b>вчера</b>" not in job.text
    apps = client.get("/applications")
    assert apps.status_code == 200 and "Откликнулся &lt;b&gt;вчера&lt;/b&gt;" in apps.text
