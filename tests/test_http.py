from __future__ import annotations

import httpx
import pytest

from cvboost.http import (
    MAX_BODY_BYTES,
    REQUEST_DEADLINE_S,
    FetchError,
    HttpClient,
    UnsafeURLError,
)
from tests.conftest import PUBLIC_RESOLVER

ALLOWED = frozenset({"example.com", "a.example.com", "b.example.com"})


class Clock:
    def __init__(self):
        self.t = 100.0
        self.sleeps: list[float] = []

    def now(self) -> float:
        return self.t

    def sleep(self, s: float) -> None:
        self.sleeps.append(s)
        self.t += s


def make(handler):
    clock = Clock()
    client = HttpClient(
        httpx.MockTransport(handler), sleep=clock.sleep, clock=clock.now, resolver=PUBLIC_RESOLVER
    )
    return client, clock


def test_user_agent_is_exact():
    seen = []

    def handler(req):
        seen.append(req.headers["user-agent"])
        return httpx.Response(200, content=b"ok")

    client, _ = make(handler)
    assert (
        client.get("https://example.com/a", min_delay_s=0, allowed_hosts=ALLOWED).content == b"ok"
    )
    assert seen == ["cvboost/0.1"]


def test_per_domain_delay():
    client, clock = make(lambda req: httpx.Response(200, content=b"x"))
    client.get("https://a.example.com/1", min_delay_s=30, allowed_hosts=ALLOWED)
    client.get(
        "https://b.example.com/1", min_delay_s=30, allowed_hosts=ALLOWED
    )  # other host: no wait
    assert clock.sleeps == []
    client.get("https://a.example.com/2", min_delay_s=30, allowed_hosts=ALLOWED)
    assert clock.sleeps == [pytest.approx(30)]


def test_retry_then_success_uses_retry_after():
    calls = []

    def handler(req):
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(429, headers={"Retry-After": "7"})
        return httpx.Response(200, content=b"ok")

    client, clock = make(handler)
    assert client.get("https://example.com/", min_delay_s=0, allowed_hosts=ALLOWED).content == b"ok"
    assert len(calls) == 2
    assert 7 in clock.sleeps


def test_retry_after_is_capped():
    calls = []

    def handler(req):
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(503, headers={"Retry-After": "3600"})
        return httpx.Response(200, content=b"ok")

    client, clock = make(handler)
    client.get("https://example.com/", min_delay_s=0, allowed_hosts=ALLOWED)
    assert 60 in clock.sleeps


def test_500_gives_three_attempts_then_fetch_error():
    calls = []

    def handler(req):
        calls.append(1)
        return httpx.Response(500)

    client, clock = make(handler)
    with pytest.raises(FetchError) as ei:
        client.get("https://example.com/", min_delay_s=0, allowed_hosts=ALLOWED)
    assert len(calls) == 3
    assert ei.value.status == 500 and ei.value.attempts == 3
    assert len(clock.sleeps) == 2


def test_timeout_is_retried_and_wrapped():
    calls = []

    def handler(req):
        calls.append(1)
        raise httpx.ReadTimeout("slow", request=req)

    client, _ = make(handler)
    with pytest.raises(FetchError) as ei:
        client.get("https://example.com/", min_delay_s=0, allowed_hosts=ALLOWED)
    assert len(calls) == 3
    assert isinstance(ei.value.__cause__, httpx.ReadTimeout)
    assert ei.value.status is None


def test_403_is_not_retried():
    calls = []

    def handler(req):
        calls.append(1)
        return httpx.Response(403, content=b"cloudflare")

    client, clock = make(handler)
    with pytest.raises(FetchError) as ei:
        client.get("https://example.com/", min_delay_s=0, allowed_hosts=ALLOWED)
    assert len(calls) == 1 and ei.value.status == 403
    assert clock.sleeps == []


def test_oversized_body_is_aborted():
    def handler(req):
        return httpx.Response(200, content=b"x" * (MAX_BODY_BYTES + 1))

    client, _ = make(handler)
    with pytest.raises(FetchError, match="larger than"):
        client.get("https://example.com/", min_delay_s=0, allowed_hosts=ALLOWED)


def test_gzip_body_is_decoded():
    import gzip

    def handler(req):
        return httpx.Response(
            200, content=gzip.compress(b"hello"), headers={"Content-Encoding": "gzip"}
        )

    client, _ = make(handler)
    assert (
        client.get("https://example.com/", min_delay_s=0, allowed_hosts=ALLOWED).content == b"hello"
    )


class Tracking(httpx.SyncByteStream):
    def __init__(self, chunks=1000, size=1024):
        self.chunks, self.size, self.consumed = chunks, size, 0

    def __iter__(self):
        for _ in range(self.chunks):
            self.consumed += 1
            yield b"x" * self.size


def test_error_body_is_not_read():
    streams = []

    def handler(req):
        st = Tracking()
        streams.append(st)
        return httpx.Response(403, stream=st)

    client, _ = make(handler)
    with pytest.raises(FetchError):
        client.get("https://example.com/", min_delay_s=0, allowed_hosts=ALLOWED)
    assert streams[0].consumed == 0


def test_slow_body_hits_overall_deadline():
    t = [0.0]

    def clock():
        t[0] += 50.0  # every clock() call advances 50s
        return t[0]

    def handler(req):
        return httpx.Response(200, stream=Tracking(chunks=100))

    client = HttpClient(
        httpx.MockTransport(handler), sleep=lambda s: None, clock=clock, resolver=PUBLIC_RESOLVER
    )
    with pytest.raises(FetchError, match="not complete within") as ei:
        client.get("https://example.com/", min_delay_s=0, allowed_hosts=ALLOWED)
    assert ei.value.attempts == 1
    assert REQUEST_DEADLINE_S == 120.0


# --- SEC-03: manual redirects, host allowlist, public-IP check ---------------------------

OK_HOST = frozenset({"jobs.example.com"})


def redirecting(location, status=302, log=None):
    def handler(req):
        if log is not None:
            log.append(str(req.url))
        if len(log or [1]) <= 1:
            return httpx.Response(status, headers={"Location": location})
        return httpx.Response(200, content=b"final")

    return handler


def resolver_for(mapping):
    def resolve(host):
        return mapping[host]

    return resolve


def make_with(handler, resolver=PUBLIC_RESOLVER):
    clock = Clock()
    client = HttpClient(
        httpx.MockTransport(handler), sleep=clock.sleep, clock=clock.now, resolver=resolver
    )
    return client, clock


def test_redirect_to_foreign_host_is_refused_without_a_second_request():
    log = []
    client, _ = make_with(redirecting("https://evil.example.net/", log=log))
    with pytest.raises(UnsafeURLError) as ei:
        client.get("https://jobs.example.com/a", min_delay_s=0, allowed_hosts=OK_HOST)
    assert isinstance(ei.value, FetchError)
    assert ei.value.status == 302 and ei.value.attempts == 1
    assert log == ["https://jobs.example.com/a"]


def test_redirect_to_http_is_refused():
    log = []
    client, _ = make_with(redirecting("http://jobs.example.com/a", status=301, log=log))
    with pytest.raises(UnsafeURLError):
        client.get("https://jobs.example.com/a", min_delay_s=0, allowed_hosts=OK_HOST)
    assert len(log) == 1


@pytest.mark.parametrize(
    "addr",
    [
        "10.0.0.5",
        "127.0.0.1",
        "169.254.169.254",
        "100.64.0.1",
        "::1",
        "fe80::1",
        "fc00::1",
        "::ffff:127.0.0.1",
        "64:ff9b::7f00:1",
        "224.0.0.1",
    ],
)
def test_host_resolving_to_non_public_address_is_refused(addr):
    log = []
    client, _ = make_with(
        lambda req: log.append(1) or httpx.Response(200, content=b"x"),
        resolver_for({"jobs.example.com": [addr]}),
    )
    with pytest.raises(UnsafeURLError):
        client.get("https://jobs.example.com/a", min_delay_s=0, allowed_hosts=OK_HOST)
    assert log == []


def test_one_private_address_among_public_ones_is_refused():
    client, _ = make_with(
        lambda req: httpx.Response(200, content=b"x"),
        resolver_for({"jobs.example.com": ["93.184.215.14", "10.0.0.5"]}),
    )
    with pytest.raises(UnsafeURLError):
        client.get("https://jobs.example.com/a", min_delay_s=0, allowed_hosts=OK_HOST)


def test_redirect_target_resolving_to_private_ip_is_refused():
    log = []
    client, _ = make_with(
        redirecting("https://other.example.com/x", log=log),
        resolver_for({"jobs.example.com": ["93.184.215.14"], "other.example.com": ["10.1.1.1"]}),
    )
    with pytest.raises(UnsafeURLError):
        client.get(
            "https://jobs.example.com/a",
            min_delay_s=0,
            allowed_hosts=frozenset({"jobs.example.com", "other.example.com"}),
        )
    assert len(log) == 1


@pytest.mark.parametrize(
    "url",
    [
        "http://jobs.example.com/a",
        "https://other.example.com/a",
        "https://user:pw@jobs.example.com/a",
        "https://jobs.example.com:8443/a",
        "https://203.0.113.9/a",
    ],
)
def test_initial_url_outside_policy_sends_no_request(url):
    log = []
    client, _ = make_with(lambda req: log.append(1) or httpx.Response(200, content=b"x"))
    with pytest.raises(UnsafeURLError):
        client.get(url, min_delay_s=0, allowed_hosts=OK_HOST)
    assert log == []


def test_redirect_with_userinfo_or_port_is_refused():
    for target in ("https://u@jobs.example.com/x", "https://jobs.example.com:8443/x"):
        client, _ = make_with(redirecting(target, log=[]))
        with pytest.raises(UnsafeURLError):
            client.get("https://jobs.example.com/a", min_delay_s=0, allowed_hosts=OK_HOST)


def test_explicit_443_is_allowed():
    client, _ = make_with(lambda req: httpx.Response(200, content=b"ok"))
    assert (
        client.get("https://jobs.example.com:443/a", min_delay_s=0, allowed_hosts=OK_HOST).content
        == b"ok"
    )


def test_relative_and_in_allowlist_redirects_work():
    log = []
    client, _ = make_with(redirecting("/final", log=log))
    resp = client.get("https://jobs.example.com/a", min_delay_s=0, allowed_hosts=OK_HOST)
    assert resp.content == b"final"
    assert log == ["https://jobs.example.com/a", "https://jobs.example.com/final"]


def test_politeness_delay_applies_to_the_redirect_target_host():
    log = []
    client, clock = make_with(redirecting("https://b.example.com/x", log=log))
    allowed = frozenset({"a.example.com", "b.example.com"})
    # b was hit a moment ago; the redirect from a to b must wait for b
    client._last_request["b.example.com"] = clock.now()
    assert (
        client.get("https://a.example.com/a", min_delay_s=30, allowed_hosts=allowed).content
        == b"final"
    )
    assert clock.sleeps == [pytest.approx(30)]


def test_six_redirects_in_a_row_fail():
    log = []

    def handler(req):
        log.append(1)
        return httpx.Response(302, headers={"Location": "/next"})

    client, _ = make_with(handler)
    with pytest.raises(FetchError, match="too many redirects"):
        client.get("https://jobs.example.com/a", min_delay_s=0, allowed_hosts=OK_HOST)
    assert len(log) == 6


def test_five_redirects_are_fine():
    log = []

    def handler(req):
        log.append(1)
        if len(log) <= 5:
            return httpx.Response(302, headers={"Location": "/next"})
        return httpx.Response(200, content=b"done")

    client, _ = make_with(handler)
    assert client.get("https://jobs.example.com/a", min_delay_s=0, allowed_hosts=OK_HOST).content


def test_redirect_body_is_not_read():
    streams = []

    def handler(req):
        if streams:
            return httpx.Response(200, content=b"ok")
        st = Tracking()
        streams.append(st)
        return httpx.Response(302, headers={"Location": "/x"}, stream=st)

    client, _ = make_with(handler)
    client.get("https://jobs.example.com/a", min_delay_s=0, allowed_hosts=OK_HOST)
    assert streams[0].consumed == 0


def test_resolver_oserror_is_retried_then_fetch_error():
    calls = []

    def resolve(host):
        calls.append(host)
        raise OSError("no such host")

    client, _ = make_with(lambda req: httpx.Response(200, content=b"x"), resolve)
    with pytest.raises(FetchError) as ei:
        client.get("https://jobs.example.com/a", min_delay_s=0, allowed_hosts=OK_HOST)
    assert not isinstance(ei.value, UnsafeURLError)
    assert len(calls) == 3 and ei.value.attempts == 3


def test_unsafe_url_is_not_retried():
    calls = []

    def resolve(host):
        calls.append(host)
        return ["127.0.0.1"]

    client, clock = make_with(lambda req: httpx.Response(200, content=b"x"), resolve)
    with pytest.raises(UnsafeURLError) as ei:
        client.get("https://jobs.example.com/a", min_delay_s=0, allowed_hosts=OK_HOST)
    assert len(calls) == 1 and ei.value.attempts == 1
    assert clock.sleeps == []


def test_redirect_without_location_is_an_error():
    client, _ = make_with(lambda req: httpx.Response(301))
    with pytest.raises(FetchError, match="without"):
        client.get("https://jobs.example.com/a", min_delay_s=0, allowed_hosts=OK_HOST)


def test_host_match_is_exact_and_case_insensitive():
    client, _ = make_with(lambda req: httpx.Response(200, content=b"ok"))
    assert client.get("https://JOBS.example.com./a", min_delay_s=0, allowed_hosts=OK_HOST)
    with pytest.raises(UnsafeURLError):
        client.get("https://x.jobs.example.com/a", min_delay_s=0, allowed_hosts=OK_HOST)


def test_default_resolver_uses_getaddrinfo(monkeypatch):
    # the autouse fixture makes getaddrinfo raise; the default resolver must go through it
    client = HttpClient(httpx.MockTransport(lambda req: httpx.Response(200)), sleep=lambda s: None)
    with pytest.raises(RuntimeError, match="real DNS"):
        client.get("https://jobs.example.com/a", min_delay_s=0, allowed_hosts=OK_HOST)


def test_malformed_location_is_a_fetch_error_without_retries():
    calls = []

    def handler(req):
        calls.append(1)
        return httpx.Response(302, headers={"Location": "https://jobs.example.com/\x00"})

    client, clock = make_with(handler)
    with pytest.raises(FetchError, match="invalid redirect target") as ei:
        client.get("https://jobs.example.com/a", min_delay_s=0, allowed_hosts=OK_HOST)
    assert len(calls) == 1 and ei.value.attempts == 1 and clock.sleeps == []


def test_environment_proxies_are_ignored(monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.example.com:3128")
    client = HttpClient(
        httpx.MockTransport(lambda r: httpx.Response(200)), resolver=PUBLIC_RESOLVER
    )
    assert client._client.trust_env is False


# --- post_json (Telegram, issue #5) ---


def _post_client(handler, resolver=None):
    return HttpClient(
        httpx.MockTransport(handler), sleep=lambda s: None, resolver=resolver or PUBLIC_RESOLVER
    )


HOSTS = frozenset({"api.example.com"})


def test_post_json_sends_json_and_reads_error_bodies():
    seen = {}

    def handler(req):
        seen["body"], seen["method"] = req.content, req.method
        return httpx.Response(429, json={"ok": False, "parameters": {"retry_after": 3}})

    r = _post_client(handler).post_json(
        "https://api.example.com/x",
        payload={"a": 1},
        allowed_hosts=HOSTS,
        min_delay_s=0,
        timeout_s=5,
    )
    assert seen["method"] == "POST" and seen["body"] == b'{"a":1}'
    assert r.status_code == 429 and r.json()["parameters"]["retry_after"] == 3


def test_post_json_one_attempt_no_retry_on_500():
    calls = []

    def handler(req):
        calls.append(1)
        return httpx.Response(500)

    r = _post_client(handler).post_json(
        "https://api.example.com/x", payload={}, allowed_hosts=HOSTS, min_delay_s=0, timeout_s=5
    )
    assert r.status_code == 500 and len(calls) == 1


@pytest.mark.parametrize(
    "url",
    [
        "http://api.example.com/x",
        "https://other.example.com/x",
        "https://api.example.com:8443/x",
        "https://u:p@api.example.com/x",
    ],
)
def test_post_json_enforces_the_url_policy(url):
    from cvboost.http import UnsafeURLError

    with pytest.raises(UnsafeURLError):
        _post_client(lambda r: httpx.Response(200)).post_json(
            url, payload={}, allowed_hosts=HOSTS, min_delay_s=0, timeout_s=5
        )


def test_post_json_refuses_private_addresses_and_redirects_and_big_bodies():
    from cvboost.http import FetchError, UnsafeURLError

    with pytest.raises(UnsafeURLError):
        _post_client(lambda r: httpx.Response(200), resolver=lambda h: ["10.0.0.5"]).post_json(
            "https://api.example.com/x", payload={}, allowed_hosts=HOSTS, min_delay_s=0, timeout_s=5
        )
    with pytest.raises(FetchError, match="redirect"):
        _post_client(
            lambda r: httpx.Response(302, headers={"Location": "https://api.example.com/y"})
        ).post_json(
            "https://api.example.com/x", payload={}, allowed_hosts=HOSTS, min_delay_s=0, timeout_s=5
        )
    with pytest.raises(FetchError, match="too large"):
        _post_client(lambda r: httpx.Response(200, content=b"x" * (300 * 1024))).post_json(
            "https://api.example.com/x", payload={}, allowed_hosts=HOSTS, min_delay_s=0, timeout_s=5
        )


# --- #46b round 2: bounded decompression, deadline, InvalidURL, ::/96 ----------------------


class RawStream(httpx.SyncByteStream):
    """A body that goes over the wire as-is (so Content-Encoding is NOT decoded by the mock)."""

    def __init__(self, data: bytes, step: int = 4096):
        self.data, self.step, self.read = data, step, 0

    def __iter__(self):
        for i in range(0, len(self.data), self.step):
            self.read += 1
            yield self.data[i : i + self.step]


def encoded(data: bytes, encoding: str, stream_holder: list | None = None):
    def handler(req):
        st = RawStream(data)
        if stream_holder is not None:
            stream_holder.append(st)
        return httpx.Response(200, headers={"Content-Encoding": encoding}, stream=st)

    return handler


def test_default_accept_encoding_is_gzip_deflate_only():
    seen = []

    def handler(req):
        seen.append(req.headers["accept-encoding"])
        return httpx.Response(200, content=b"ok")

    client, _ = make(handler)
    client.get("https://example.com/", min_delay_s=0, allowed_hosts=ALLOWED)
    client.get(
        "https://example.com/",
        min_delay_s=0,
        allowed_hosts=ALLOWED,
        headers={"Accept-Encoding": "identity"},
    )
    assert seen == ["gzip, deflate", "identity"]


def test_gzip_bomb_is_stopped_at_the_limit():
    import gzip
    import time

    bomb = gzip.compress(b"\0" * (200 * 1024 * 1024), 9)  # ~200 KB on the wire, 200 MB inflated
    holder: list = []
    client, _ = make(encoded(bomb, "gzip", holder))
    start = time.monotonic()
    with pytest.raises(FetchError, match="larger than 1000000 bytes"):
        client.get(
            "https://example.com/", min_delay_s=0, allowed_hosts=ALLOWED, max_bytes=1_000_000
        )
    assert time.monotonic() - start < 5


def test_deflate_bomb_is_stopped_and_raw_deflate_is_understood():
    import zlib

    bomb = zlib.compress(b"\0" * (100 * 1024 * 1024), 9)
    client, _ = make(encoded(bomb, "deflate"))
    with pytest.raises(FetchError, match="larger than"):
        client.get("https://example.com/", min_delay_s=0, allowed_hosts=ALLOWED, max_bytes=100_000)
    raw = zlib.compressobj(wbits=-15)
    body = raw.compress(b"hello raw deflate") + raw.flush()
    client, _ = make(encoded(body, "deflate"))
    resp = client.get("https://example.com/", min_delay_s=0, allowed_hosts=ALLOWED)
    assert resp.content == b"hello raw deflate"


def test_streamed_gzip_is_decoded():
    import gzip

    payload = b"abc" * 100_000
    client, _ = make(encoded(gzip.compress(payload), "gzip"))
    assert client.get("https://example.com/", min_delay_s=0, allowed_hosts=ALLOWED).content == (
        payload
    )


@pytest.mark.parametrize(
    "encoding", ["br", "zstd", "compress", "x-compress", "gzip, gzip", "gzip, br", "identity, gzip"]
)
def test_unexpected_content_encoding_is_refused(encoding):
    client, _ = make(encoded(b"whatever", encoding))
    with pytest.raises(FetchError, match="Content-Encoding"):
        client.get("https://example.com/", min_delay_s=0, allowed_hosts=ALLOWED)


def test_corrupt_gzip_is_a_fetch_error():
    client, _ = make(encoded(b"not gzip at all", "gzip"))
    with pytest.raises(FetchError, match="corrupt"):
        client.get("https://example.com/", min_delay_s=0, allowed_hosts=ALLOWED)


def test_post_json_inflation_is_bounded_too():
    import gzip

    from cvboost.http import ResponseTooLarge

    bomb = gzip.compress(b"\0" * (50 * 1024 * 1024), 9)
    client = HttpClient(
        httpx.MockTransport(encoded(bomb, "gzip")),
        sleep=lambda s: None,
        resolver=PUBLIC_RESOLVER,
    )
    with pytest.raises(ResponseTooLarge):
        client.post_json(
            "https://example.com/x",
            payload={},
            allowed_hosts=ALLOWED,
            min_delay_s=0,
            timeout_s=5,
        )


def test_invalid_url_is_a_fetch_error_not_an_httpx_exception():
    client, _ = make(lambda req: httpx.Response(200, content=b"x"))
    with pytest.raises(FetchError, match="invalid URL"):
        client.get("https://example.com/a\x00b", min_delay_s=0, allowed_hosts=ALLOWED)


def test_invalid_redirect_location_is_a_fetch_error():
    client, _ = make(lambda req: httpx.Response(302, headers={"Location": "https://exa\x00mple/"}))
    with pytest.raises(FetchError):
        client.get("https://example.com/a", min_delay_s=0, allowed_hosts=ALLOWED)


def test_a_request_that_never_finishes_hits_the_overall_deadline():
    import threading
    import time

    release = threading.Event()

    def handler(req):
        release.wait(10)  # a server that never sends its headers
        return httpx.Response(200, content=b"late")

    client, _ = make(handler)
    start = time.monotonic()
    try:
        with pytest.raises(FetchError, match="not complete within") as ei:
            client.get(
                "https://example.com/",
                min_delay_s=0,
                allowed_hosts=ALLOWED,
                deadline_s=0.3,
                max_attempts=1,
            )
        assert ei.value.attempts == 1
        assert time.monotonic() - start < 3
    finally:
        release.set()


def test_exceptions_from_the_helper_thread_reach_the_caller_unchanged():
    client, _ = make(lambda req: httpx.Response(302, headers={"Location": "https://evil.net/"}))
    with pytest.raises(UnsafeURLError):
        client.get("https://example.com/", min_delay_s=0, allowed_hosts=ALLOWED)


@pytest.mark.parametrize("addr", ["::10.0.0.1", "::127.0.0.1", "::169.254.169.254", "::"])
def test_ipv4_compatible_ipv6_is_not_public(addr):
    from cvboost.http import _is_public

    assert _is_public(addr) is False


@pytest.mark.parametrize("encoding", ["utf-8", "none", "bogus", "", "  ", "x-weird"])
def test_unknown_content_encoding_is_read_as_identity_under_the_limit(encoding):
    client, _ = make(encoded(b"plain body", encoding))
    resp = client.get("https://example.com/", min_delay_s=0, allowed_hosts=ALLOWED)
    assert resp.content == b"plain body"
    client, _ = make(encoded(b"x" * 5000, encoding))
    with pytest.raises(FetchError, match="larger than 1000 bytes"):
        client.get("https://example.com/", min_delay_s=0, allowed_hosts=ALLOWED, max_bytes=1000)


def test_multi_member_gzip_is_read_to_the_end():
    import gzip

    body = gzip.compress(b"first part, ") + gzip.compress(b"second part, ") + gzip.compress(b"end")
    for step in (3, 4096):  # members split across chunks too
        holder = RawStream(body, step=step)
        client, _ = make(
            lambda req, h=holder: httpx.Response(
                200, headers={"Content-Encoding": "gzip"}, stream=h
            )
        )
        resp = client.get("https://example.com/", min_delay_s=0, allowed_hosts=ALLOWED)
        assert resp.content == b"first part, second part, end"


def test_multi_member_gzip_shares_one_output_limit():
    import gzip

    member = gzip.compress(b"\0" * 600_000, 9)
    client, _ = make(encoded(member * 50, "gzip"))  # 30 MB inflated in total
    with pytest.raises(FetchError, match="larger than 1000000 bytes"):
        client.get(
            "https://example.com/", min_delay_s=0, allowed_hosts=ALLOWED, max_bytes=1_000_000
        )


def test_gzip_trailing_zero_padding_is_ignored():
    import gzip

    client, _ = make(encoded(gzip.compress(b"data") + b"\0\0\0\0", "gzip"))
    assert client.get("https://example.com/", min_delay_s=0, allowed_hosts=ALLOWED).content == (
        b"data"
    )
