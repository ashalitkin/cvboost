from __future__ import annotations

import httpx

from cvboost.collect import collect
from cvboost.connectors.remotive import RemotiveConnector
from cvboost.http import HttpClient
from tests.conftest import NOW, PUBLIC_RESOLVER
from tests.web.conftest import Ids, csrf, db


def post(client, path, **data):
    return client.post(path, data={"csrf_token": csrf(client), **data})


def test_disable_source_makes_collect_skip_it(client, seeded: Ids, settings):
    assert post(client, "/sources/remotive/enabled", enabled="0").status_code == 303
    called = []

    def handler(req):
        called.append(req)
        return httpx.Response(500)

    http = HttpClient(httpx.MockTransport(handler), sleep=lambda s: None, resolver=PUBLIC_RESOLVER)
    res = collect(db(settings), http, [RemotiveConnector()], limit=None, force=True, now=NOW)
    assert res[0].status == "skipped" and res[0].skip_reason == "disabled" and not called
    before = client.get("/sources").text.count("disabled")
    post(client, "/sources/remotive/enabled", enabled="1")
    # other sources may be off by default (mastodon); only remotive's row changes
    assert client.get("/sources").text.count("disabled") == before - 1


def test_interval_rules(client, settings):
    floor = int(RemotiveConnector.interval.total_seconds())
    assert post(client, "/sources/remotive/interval", seconds=str(floor - 1)).status_code == 400
    assert post(client, "/sources/remotive/interval", seconds="x").status_code == 400
    assert post(client, "/sources/remotive/interval", seconds=str(8 * 86400)).status_code == 400
    assert post(client, "/sources/remotive/interval", seconds=str(floor * 2)).status_code == 303
    row = (
        db(settings)
        .conn.execute("SELECT interval_override_s FROM sources WHERE name='remotive'")
        .fetchone()
    )
    assert row[0] == floor * 2
    post(client, "/sources/remotive/interval", seconds="")
    row = (
        db(settings)
        .conn.execute("SELECT interval_override_s FROM sources WHERE name='remotive'")
        .fetchone()
    )
    assert row[0] is None


def test_unknown_source_404(client):
    assert post(client, "/sources/nope/enabled", enabled="0").status_code == 404
    assert post(client, "/sources/nope/interval", seconds="").status_code == 404
    assert post(client, "/sources/remotive/enabled", enabled="2").status_code == 400
