"""Issue #30: employment-type filter, seen/unseen, "N new", mark-all, sorting."""

from __future__ import annotations

import re
from datetime import timedelta

from tests.conftest import NOW
from tests.web.conftest import Ids, csrf, db, llm_verdict, verdict
from tests.web.test_decisions import post

HX = {"HX-Request": "true", "HX-Target": "rows"}


def titles(client, url: str, htmx: bool = False) -> list[str]:
    r = client.get(url, headers=HX if htmx else {})
    assert r.status_code == 200
    return re.findall(r'<h2><a href="/jobs/\d+">([^<]+)</a></h2>', r.text)


def count_line(client, url: str) -> str:
    return re.search(r"<strong class=\"newcount\">([^<]+)</strong>", client.get(url).text).group(1)


def seen_rows(settings) -> set[int]:
    return {r[0] for r in db(settings).conn.execute("SELECT canonical_id FROM job_views")}


# --- employment type ---


def test_type_filter_uses_own_type_then_haiku_and_fractional_is_part_time(
    client, seeded: Ids, settings
):
    c = db(settings).conn
    c.execute("UPDATE jobs SET employment_type='full_time' WHERE id=?", (seeded.a_remoteok,))
    c.execute("UPDATE jobs SET employment_type='fractional' WHERE id=?", (seeded.mixed_alive,))
    # Beta: no type, no Haiku verdict -> unknown; Evil: Haiku says contract (conftest verdict)
    assert titles(client, "/inbox?type=full_time") == ["Senior Data Engineer"]
    assert titles(client, "/inbox?type=part_time") == ["Mixed Group Role"]
    assert titles(client, "/inbox?type=contract") == ["&lt;script&gt;t&lt;/script&gt;"]
    assert titles(client, "/inbox?type=unknown") == ["Analytics Engineer"]
    both = titles(client, "/inbox?type=full_time&type=part_time")
    assert sorted(both) == ["Mixed Group Role", "Senior Data Engineer"]
    assert len(titles(client, "/inbox")) == 4  # no type chosen: everything
    assert len(titles(client, "/inbox?type=bogus")) == 4  # unknown values are ignored


def test_type_fallback_to_haiku_when_source_has_no_type(client, seeded: Ids, settings):
    llm_verdict(db(settings), seeded.b, verdict(employment="freelance", fit=50))
    assert titles(client, "/inbox?type=freelance") == ["Analytics Engineer"]
    assert "type-freelance" in client.get("/inbox").text  # the badge on the card


def test_type_dropdown_keeps_state_from_the_url(client, seeded: Ids):
    page = client.get("/inbox?type=contract&unseen=1&sort=rate").text
    assert re.search(r'<select name="type">', page)
    assert re.search(r'<option value="contract" selected>', page)
    assert not re.search(r'<option value="full_time" selected>', page)
    assert re.search(r'name="unseen" value="1" checked', page)
    assert re.search(r'<option value="rate" selected>', page)
    assert 'hx-push-url="true"' in page and 'hx-trigger="change, submit"' in page


# --- seen / unseen ---


def test_everything_starts_unseen_and_opening_the_page_marks_seen(client, seeded: Ids, settings):
    assert count_line(client, "/inbox") == "4 new"
    assert client.get(f"/jobs/{seeded.b}").status_code == 200
    assert seen_rows(settings) == {seeded.b}
    assert count_line(client, "/inbox") == "3 new"
    page = client.get("/inbox").text

    def cls(cid):
        return re.search(rf'<article class="([^"]*)" id="job-{cid}"', page).group(1)

    beta, acme = cls(seeded.b), cls(seeded.a_remoteok)
    assert "unseen" not in beta and "unseen" in acme


def test_seen_is_per_group_and_opening_a_duplicate_redirects_to_the_group(
    client, seeded: Ids, settings
):
    r = client.get(f"/jobs/{seeded.a_remotive}")  # non-canonical member -> 307
    assert r.status_code == 307
    assert seen_rows(settings) == set()
    client.get(f"/jobs/{seeded.a_remoteok}")
    assert seen_rows(settings) == {seeded.a_remoteok}


def test_every_panel_decision_marks_seen(client, seeded: Ids, settings):
    post(client, f"/jobs/{seeded.b}/decision", action="interested")
    post(client, f"/jobs/{seeded.a_remoteok}/decision", action="rejected", reason="stack")
    post(client, f"/jobs/{seeded.xss}/decision", action="prepare_cv")
    assert seen_rows(settings) == {seeded.b, seeded.a_remoteok, seeded.xss}


def test_unknown_page_does_not_create_a_view(client, seeded: Ids, settings):
    assert client.get("/jobs/99999").status_code == 404
    assert seen_rows(settings) == set()


def test_unseen_only_filter_and_counter_follow_the_filters(client, seeded: Ids, settings):
    client.get(f"/jobs/{seeded.b}")
    assert sorted(titles(client, "/inbox?unseen=1")) == sorted(
        t for t in titles(client, "/inbox") if t != "Analytics Engineer"
    )
    # the counter counts under the other filters, not globally
    assert count_line(client, "/inbox") == "3 new"
    assert count_line(client, "/inbox?type=unknown") == "1 new"  # Beta (seen) + Mixed
    assert count_line(client, "/inbox?type=contract") == "2 new"  # Acme + Evil
    assert count_line(client, "/inbox?fit_min=85") == "1 new"


def test_mark_all_shown_marks_exactly_the_shown_rows_and_keeps_the_url(
    client, seeded: Ids, settings
):
    assert "Mark all shown as seen" in client.get("/inbox").text
    qs = "fit_min=80&sort=fit"
    r = post(client, "/seen/mark", page="inbox", qs=qs, cids=[seeded.a_remoteok])
    assert r.status_code == 303 and r.headers["location"] == f"/inbox?{qs}"
    assert seen_rows(settings) == {seeded.a_remoteok}
    assert count_line(client, "/inbox") == "3 new"


def test_mark_all_button_posts_the_ids_on_screen(client, seeded: Ids):
    page = client.get("/inbox?fit_min=85").text
    ids = re.findall(r'name="cids" value="(\d+)"', page)
    assert ids == [str(seeded.a_remoteok)]
    assert 'name="qs" value="fit_min=85"' in page


def test_mark_all_needs_csrf_and_a_known_page_and_ignores_junk_ids(client, seeded: Ids, settings):
    assert client.post("/seen/mark", data={"page": "inbox"}).status_code == 403
    assert post(client, "/seen/mark", page="evil").status_code == 400
    r = post(client, "/seen/mark", page="screened", qs="x=1\r\nSet-Cookie: a=b", cids=[424242])
    assert r.status_code == 303 and "\r" not in r.headers["location"]
    assert r.headers["location"].startswith("/screened?")
    assert seen_rows(settings) == set()  # unknown id: nothing written, no FK error


def test_mark_all_on_a_duplicate_member_id_is_ignored(client, seeded: Ids, settings):
    post(client, "/seen/mark", page="inbox", cids=[seeded.a_remotive])  # not a group id
    assert seen_rows(settings) == set()


def test_job_view_is_idempotent_and_keeps_the_first_time(client, seeded: Ids, settings):
    client.get(f"/jobs/{seeded.b}")
    first = db(settings).conn.execute("SELECT seen_at FROM job_views").fetchone()[0]
    client.get(f"/jobs/{seeded.b}")
    rows = db(settings).conn.execute("SELECT seen_at FROM job_views").fetchall()
    assert len(rows) == 1 and rows[0][0] == first


def test_htmx_row_after_decision_is_not_marked_unseen(client, seeded: Ids):
    tok = csrf(client)
    r = client.post(
        f"/jobs/{seeded.b}/decision",
        data={"action": "interested", "csrf_token": tok},
        headers={"HX-Request": "true"},
    )
    assert r.status_code == 200 and "unseen" not in r.text and "Analytics Engineer" in r.text


# --- sorting ---


def stamp(settings, ids: dict[int, dict]):
    c = db(settings).conn
    for jid, f in ids.items():
        for col, val in f.items():
            c.execute(f"UPDATE jobs SET {col}=? WHERE id=?", (val, jid))


def test_sort_modes_apply_after_the_filters(client, seeded: Ids, settings):
    t = lambda d: (NOW - timedelta(days=d)).isoformat()  # noqa: E731
    stamp(
        settings,
        {
            seeded.a_remoteok: dict(
                first_seen_at=t(5),
                posted_at=t(1),
                salary_max=50,
                salary_min=40,
                salary_period="hour",
                salary_currency="USD",
            ),
            seeded.b: dict(
                first_seen_at=t(0.1),
                posted_at=t(9),
                salary_max=900,
                salary_min=800,
                salary_period="day",
                salary_currency="USD",
            ),
            seeded.xss: dict(first_seen_at=t(3), posted_at=t(2)),
            seeded.mixed_alive: dict(
                first_seen_at=t(1),
                posted_at=t(0.5),
                salary_max=150000,
                salary_min=120000,
                salary_period="year",
                salary_currency="USD",
            ),
        },
    )
    sr, an, mg = "Senior Data Engineer", "Analytics Engineer", "Mixed Group Role"
    fit = titles(client, "/inbox")
    assert fit[0] == sr  # fit 85 first, then default order
    new = titles(client, "/inbox?sort=new")
    assert new[:2] == [an, mg] and new[-1] == sr
    posted = titles(client, "/inbox?sort=posted")
    assert posted[0] == mg and posted[1] == sr and posted[-1] == an
    rate = titles(client, "/inbox?sort=rate")  # b 112/h, mixed 78/h, a 50/h, xss unknown
    assert rate[:3] == [an, mg, sr] and len(rate) == 4
    assert titles(client, "/inbox?sort=bogus") == fit  # unknown value -> default
    # after the filters: the type filter shrinks the list, then the sort orders what is left
    assert titles(client, "/inbox?sort=rate&type=unknown") == [an, mg]
    assert titles(client, "/inbox?sort=new&unseen=1&fit_min=85") == [sr]


def test_htmx_returns_only_the_rows_with_the_new_state(client, seeded: Ids):
    part = client.get("/inbox?sort=new&type=contract", headers=HX).text
    assert "<html" not in part and 'class="newcount"' in part


# --- Screened out by AI ---


def screen_two(settings, seeded):
    s = db(settings)
    llm_verdict(s, seeded.b, verdict(eligible=False, fit=41, employment="freelance"))
    llm_verdict(s, seeded.mixed_alive, verdict(eligible=False, fit=60, employment="contract"))


def test_screened_has_type_unseen_sort_and_mark_all(client, seeded: Ids, settings):
    screen_two(settings, seeded)
    page = client.get("/screened").text
    assert 'hx-get="/screened"' in page and '<option value="contract">' in page
    assert count_line(client, "/screened") == "2 new"
    assert titles(client, "/screened") == ["Mixed Group Role", "Analytics Engineer"]  # fit 60, 41
    assert titles(client, "/screened?sort=posted") != []
    # Haiku said freelance -> the filter works on this page too (Haiku fallback), via htmx
    assert titles(client, "/screened?type=freelance", htmx=True) == ["Analytics Engineer"]
    client.get(f"/jobs/{seeded.b}")
    assert titles(client, "/screened?unseen=1") == ["Mixed Group Role"]
    assert count_line(client, "/screened") == "1 new"
    r = post(client, "/seen/mark", page="screened", qs="sort=fit", cids=[seeded.mixed_alive])
    assert r.headers["location"] == "/screened?sort=fit"
    assert count_line(client, "/screened") == "0 new"
    assert "Mark all shown" not in client.get("/screened").text  # nothing unseen left


def test_screened_override_marks_seen(client, seeded: Ids, settings):
    screen_two(settings, seeded)
    post(client, f"/jobs/{seeded.b}/decision", action="override_in", note="fits")
    assert seeded.b in seen_rows(settings)


def test_new_count_links_to_the_unseen_filter_keeping_other_filters(client, seeded: Ids):
    page = client.get("/inbox?type=unknown&sort=rate").text
    m = re.search(r'<a class="newlink" href="([^"]+)"', page)
    assert m, "N новых must be a link"
    href = m.group(1).replace("&amp;", "&")
    assert "unseen=1" in href and "type=unknown" in href and "sort=rate" in href
    on = client.get("/inbox?unseen=1&type=unknown").text
    assert 'class="newlink"' not in on and "show all" in on
    assert re.search(r'href="[^"]*type=unknown[^"]*">show all', on)
    assert "unseen=1" not in re.search(r'href="([^"]*)">show all', on).group(1)
