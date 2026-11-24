from __future__ import annotations

from tests.web.conftest import Ids, csrf, db

POSTS = [
    ("/jobs/{a}/decision", {"action": "interested"}),
    ("/jobs/{a}/status", {"status": "applied"}),
    ("/applications/{a}/status", {"status": "applied"}),
    ("/sources/remotive/enabled", {"enabled": "0"}),
    ("/sources/remotive/interval", {"seconds": ""}),
    ("/jobs/{a}/kit/resume", {"resume_md": "# x"}),
    ("/jobs/{a}/kit/approve", {}),
    ("/jobs/{a}/kit/cover", {"cover_md": "x"}),
    ("/jobs/{a}/kit/cover/pdf", {}),
    ("/jobs/{a}/kit/cover/make", {}),
    ("/jobs/{a}/kit/cover/approve", {}),
]


def _count(settings):
    s = db(settings)
    return tuple(
        s.conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        for t in ("decisions", "applications")
    ) + (s.conn.execute("SELECT enabled FROM sources WHERE name='remotive'").fetchone()[0],)


def test_every_post_needs_token(client, seeded: Ids, settings):
    before = _count(settings)
    client.get("/inbox")  # gets the cookie
    for path, data in POSTS:
        p = path.format(a=seeded.a_remoteok)
        assert client.post(p, data=data).status_code == 403, p
        assert client.post(p, data={**data, "csrf_token": "x.y"}).status_code == 403, p
    assert _count(settings) == before


def test_origin_and_fetch_site(client, seeded: Ids, settings):
    tok = csrf(client)
    path = f"/jobs/{seeded.a_remoteok}/decision"
    data = {"action": "interested", "csrf_token": tok}
    assert (
        client.post(path, data=data, headers={"Origin": "https://evil.example"}).status_code == 403
    )
    assert client.post(path, data=data, headers={"Origin": "null"}).status_code == 403
    assert client.post(path, data=data, headers={"Sec-Fetch-Site": "cross-site"}).status_code == 403
    assert db(settings).conn.execute("SELECT COUNT(*) FROM decisions").fetchone()[0] == 0
    ok = client.post(
        path, data=data, headers={"Origin": "http://testserver", "Sec-Fetch-Site": "same-origin"}
    )
    assert ok.status_code == 303


def test_token_in_header_and_forged_cookie(client, seeded: Ids):
    tok = csrf(client)
    path = f"/jobs/{seeded.a_remoteok}/decision"
    assert (
        client.post(path, data={"action": "interested"}, headers={"X-CSRF-Token": tok}).status_code
        == 303
    )
    # a token signed with another key is refused even if cookie and field match
    client.cookies.set("cvb_csrf", "abc.deadbeef")
    assert (
        client.post(path, data={"action": "interested", "csrf_token": "abc.deadbeef"}).status_code
        == 403
    )


def test_native_form_post_with_null_origin(client, seeded: Ids, settings):
    """Chrome sends `Origin: null` on a native form POST; allowed only for a same-origin fetch
    that carries a valid token."""
    tok = csrf(client)
    path = f"/jobs/{seeded.a_remoteok}/decision"
    data = {"action": "interested", "csrf_token": tok}
    ok = client.post(path, data=data, headers={"Origin": "null", "Sec-Fetch-Site": "same-origin"})
    assert ok.status_code == 303
    for site in ("cross-site", "same-site", "none"):
        bad = client.post(path, data=data, headers={"Origin": "null", "Sec-Fetch-Site": site})
        assert bad.status_code == 403, site
    bad = client.post(
        path,
        data={"action": "interested", "csrf_token": "a.b"},
        headers={"Origin": "null", "Sec-Fetch-Site": "same-origin"},
    )
    assert bad.status_code == 403


def test_referrer_policy_is_same_origin(client):
    assert client.get("/inbox").headers["referrer-policy"] == "same-origin"


def test_every_unsafe_route_has_csrf_guard(client):
    from fastapi.routing import APIRoute

    from cvboost.web.security import csrf_guard

    unsafe = [
        r for r in client.app.routes if isinstance(r, APIRoute) and set(r.methods) - {"GET", "HEAD"}
    ]
    assert len(unsafe) >= 5
    for r in unsafe:
        assert any(d.call is csrf_guard for d in r.dependant.dependencies), r.path
