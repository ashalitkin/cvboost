from __future__ import annotations

from urllib.parse import urlsplit

import pytest

from cvboost import connectors


@pytest.mark.parametrize("name", sorted(connectors.REGISTRY))
def test_allowed_hosts_cover_every_request_url(name):
    c = connectors.REGISTRY[name]()
    if c.kind == "email":  # a mailbox, not HTTP: no requests, no hosts
        assert not c.requests() and not c.allowed_hosts
        return
    assert c.allowed_hosts and isinstance(c.allowed_hosts, frozenset)
    if c.kind != "ats":  # an ATS connector has requests only once companies are picked
        assert c.requests()
    for url in c.requests():
        parts = urlsplit(url)
        assert parts.scheme == "https"
        assert parts.hostname in c.allowed_hosts
