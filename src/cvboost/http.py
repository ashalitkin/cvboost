"""The only place that talks to the network."""

from __future__ import annotations

import ipaddress
import logging
import random
import socket
import threading
import time
import zlib
from collections.abc import Callable
from urllib.parse import urlsplit

import httpx

# httpx logs every request URL at INFO, query string included: an API key sent as a query
# parameter (Adzuna, ITJobs.pt) would land in the log. WARNING keeps errors, drops the URLs.
for _name in ("httpx", "httpcore"):
    logging.getLogger(_name).setLevel(logging.WARNING)

USER_AGENT = "cvboost/0.1"
MAX_BODY_BYTES = 10 * 1024 * 1024
MAX_ATTEMPTS = 3
REQUEST_DEADLINE_S = 120.0
RETRY_STATUSES = {429, 500, 502, 503, 504}
BACKOFF_S = (2.0, 4.0)
MAX_RETRY_AFTER_S = 60.0
MAX_REDIRECTS = 5
MAX_POST_RESPONSE_BYTES = 256 * 1024
_NAT64 = ipaddress.ip_network("64:ff9b::/96")
_V4_COMPATIBLE = ipaddress.ip_network("::/96")  # deprecated IPv4-compatible: ::10.0.0.1
ACCEPT_ENCODING = "gzip, deflate"  # the only encodings we decode (with an output limit)


class FetchError(Exception):
    def __init__(self, message: str, *, url: str, status: int | None, attempts: int):
        super().__init__(message)
        self.url = url
        self.status = status
        self.attempts = attempts


class ResponseTooLarge(FetchError):
    """The response body went over the caller's size limit."""


class UnsafeURLError(FetchError):
    """A request or redirect target violates the URL policy (scheme, host, port, address)."""


def _system_resolve(host: str) -> list[str]:
    infos = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    return list(dict.fromkeys(str(info[4][0]) for info in infos))


def _is_public(addr: str) -> bool:
    try:
        ip = ipaddress.ip_address(addr.split("%", 1)[0])
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    if isinstance(ip, ipaddress.IPv6Address) and (ip in _NAT64 or ip in _V4_COMPATIBLE):
        return False
    return ip.is_global and not ip.is_multicast


def _call_with_deadline[T](fn: Callable[[], T], seconds: float, url: str) -> T:
    """Run `fn` in a helper thread and give up after `seconds` of real time. A stuck request
    cannot be interrupted from outside, so its daemon thread is left to die on its own socket
    timeout; the caller gets a FetchError at once."""
    box: dict[str, object] = {}

    def run() -> None:
        try:
            box["value"] = fn()
        except BaseException as exc:  # handed back to the calling thread
            box["error"] = exc

    thread = threading.Thread(target=run, daemon=True, name="http-get")
    thread.start()
    thread.join(seconds)
    if thread.is_alive():
        bare = url.split("?", 1)[0][:200]
        raise FetchError(
            f"request not complete within {seconds:g}s", url=bare, status=None, attempts=1
        )
    if "error" in box:
        raise box["error"]  # type: ignore[misc]
    return box["value"]  # type: ignore[return-value]


_UNBOUNDABLE = {"br", "zstd", "compress", "x-compress", "x-br"}


def _content_encoding(resp: httpx.Response, url: str) -> str | None:
    """ "gzip" / "deflate" / None (read as is). Refused: encodings whose output we cannot bound
    (br, zstd, compress) and chains. Unknown or malformed values (`utf-8`, `none`, empty) are
    read as identity, under the same size limit."""
    raw = resp.headers.get("content-encoding", "").strip().lower()
    tokens = [t.strip() for t in raw.split(",") if t.strip()]
    if len(tokens) > 1 or (tokens and tokens[0] in _UNBOUNDABLE):
        raise FetchError(
            f"unsupported Content-Encoding {raw[:20]!r}",
            url=url,
            status=resp.status_code,
            attempts=1,
        )
    if tokens and tokens[0] in ("gzip", "x-gzip"):
        return "gzip"
    if tokens and tokens[0] == "deflate":
        return "deflate"
    return None


def _decoded_chunks(resp: httpx.Response, url: str, max_bytes: int, too_large: type[FetchError]):
    """Body chunks after decompression, never more than `max_bytes` in total (the compressed
    stream is read raw, so a decompression bomb is stopped on the way out, not after it)."""
    encoding = _content_encoding(resp, url)
    if resp.is_stream_consumed:  # an in-memory response (test transports): already decoded
        if len(resp.content) > max_bytes:
            raise too_large(
                f"response larger than {max_bytes} bytes",
                url=url,
                status=resp.status_code,
                attempts=1,
            )
        yield resp.content
        return

    def big() -> FetchError:
        return too_large(
            f"response larger than {max_bytes} bytes", url=url, status=resp.status_code, attempts=1
        )

    if encoding is None:
        size = 0
        for chunk in resp.iter_raw():
            size += len(chunk)
            if size > max_bytes:
                raise big()
            yield chunk
        return
    wbits = zlib.MAX_WBITS | 16 if encoding == "gzip" else zlib.MAX_WBITS
    inflater = zlib.decompressobj(wbits)
    out = raw_in = 0
    first = True
    for chunk in resp.iter_raw():
        raw_in += len(chunk)
        if raw_in > max_bytes:
            raise big()
        data = chunk
        while data:
            if inflater.eof:  # gzip may hold several members: the next one is read with the
                # same output budget; trailing zero padding is ignored
                if encoding != "gzip" or not data.strip(b"\0"):
                    break
                inflater = zlib.decompressobj(wbits)
            try:
                piece = inflater.decompress(data, max_bytes - out + 1)
            except zlib.error as exc:
                if first and encoding == "deflate":  # some servers send raw deflate
                    inflater = zlib.decompressobj(-zlib.MAX_WBITS)
                    first = False
                    continue
                raise FetchError(
                    "corrupt compressed body", url=url, status=resp.status_code, attempts=1
                ) from exc
            first = False
            out += len(piece)
            if out > max_bytes:
                raise big()
            if piece:
                yield piece
            data = inflater.unused_data if inflater.eof else inflater.unconsumed_tail


class HttpClient:
    def __init__(
        self,
        transport: httpx.BaseTransport | None = None,
        *,
        resolver: Callable[[str], list[str]] = _system_resolve,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ):
        self._client = httpx.Client(
            transport=transport,
            headers={"User-Agent": USER_AGENT, "Accept-Encoding": ACCEPT_ENCODING},
            timeout=httpx.Timeout(20.0, connect=10.0),
            follow_redirects=False,
            trust_env=False,  # no proxies or CA overrides from the environment
        )
        self._resolver = resolver
        self._sleep = sleep
        self._clock = clock
        self._last_request: dict[str, float] = {}

    def close(self) -> None:
        self._client.close()

    def _wait_for_host(self, host: str, min_delay_s: float) -> None:
        last = self._last_request.get(host)
        if last is not None:
            wait = last + min_delay_s - self._clock()
            if wait > 0:
                self._sleep(wait)
        self._last_request[host] = self._clock()

    def _check_url(
        self, url: httpx.URL, allowed_hosts: frozenset[str], redirect_status: int | None
    ) -> None:
        def unsafe(why: str) -> UnsafeURLError:
            bare = str(url.copy_with(query=None, fragment=None))  # a query may hold an API key
            return UnsafeURLError(
                f"refusing {why}: {bare}", url=bare, status=redirect_status, attempts=1
            )

        if url.scheme != "https":
            raise unsafe("non-https URL")
        if url.port not in (None, 443):
            raise unsafe("non-standard port")
        if url.userinfo:
            raise unsafe("URL with credentials")
        host = url.host.lower().rstrip(".")
        if host not in allowed_hosts:
            raise unsafe("host outside the connector allowlist")
        try:
            addrs = self._resolver(host)
        except OSError as exc:
            raise httpx.ConnectError(f"cannot resolve {host}: {exc}") from exc
        if not addrs:
            raise httpx.ConnectError(f"cannot resolve {host}: no addresses")
        if not all(_is_public(a) for a in addrs):
            raise unsafe("host resolving to a non-public address")

    def _read(
        self,
        url: str,
        params: dict[str, str] | None,
        allowed_hosts: frozenset[str],
        min_delay_s: float,
        max_bytes: int = MAX_BODY_BYTES,
        headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        """One GET following redirects by hand. Every hop, same host or not, is URL-checked and
        waits `min_delay_s` since the last request to its host."""
        try:
            request = self._client.build_request("GET", url, params=params, headers=headers)
        except httpx.InvalidURL as exc:
            raise FetchError(f"invalid URL: {exc}", url=url[:200], status=None, attempts=1) from exc
        redirect_status: int | None = None
        for hop in range(MAX_REDIRECTS + 1):
            self._check_url(request.url, allowed_hosts, redirect_status)
            if hop:  # the first hop is paced by get()
                self._wait_for_host(request.url.host, min_delay_s)
            try:
                resp = self._client.send(request, stream=True)
            except httpx.InvalidURL as exc:
                raise FetchError(
                    f"invalid redirect target: {exc}", url=url, status=redirect_status, attempts=1
                ) from exc
            except httpx.RemoteProtocolError as exc:
                if "location header" in str(exc).lower():  # malformed Location: not retryable
                    raise FetchError(
                        f"invalid redirect target: {exc}",
                        url=url,
                        status=redirect_status,
                        attempts=1,
                    ) from exc
                raise
            try:
                if resp.is_redirect:
                    # never read a redirect body
                    redirect_status = resp.status_code
                    nxt = resp.next_request
                    if nxt is None:
                        raise FetchError(
                            "redirect without target", url=url, status=resp.status_code, attempts=1
                        )
                    request = nxt
                    continue
                return self._finish(resp, url, max_bytes)
            finally:
                resp.close()
        raise FetchError("too many redirects", url=url, status=redirect_status, attempts=1)

    def _finish(
        self, resp: httpx.Response, url: str, max_bytes: int = MAX_BODY_BYTES
    ) -> httpx.Response:
        if 300 <= resp.status_code < 400:
            raise FetchError(
                f"HTTP {resp.status_code} without Location",
                url=url,
                status=resp.status_code,
                attempts=1,
            )
        if resp.status_code >= 400:
            # never read an error body: keep only status and headers
            return httpx.Response(
                resp.status_code, headers=resp.headers.multi_items(), request=resp.request
            )
        chunks: list[bytes] = []
        deadline = self._clock() + REQUEST_DEADLINE_S
        for chunk in _decoded_chunks(resp, url, max_bytes, ResponseTooLarge):
            if self._clock() > deadline:
                raise FetchError(
                    f"response not complete within {REQUEST_DEADLINE_S:g}s",
                    url=url,
                    status=resp.status_code,
                    attempts=1,
                )
            chunks.append(chunk)
        headers = [
            (k, v)
            for k, v in resp.headers.multi_items()
            if k.lower() not in {"content-encoding", "content-length", "transfer-encoding"}
        ]
        return httpx.Response(
            resp.status_code, headers=headers, content=b"".join(chunks), request=resp.request
        )

    def get(
        self,
        url: str,
        *,
        min_delay_s: float,
        allowed_hosts: frozenset[str],
        params: dict[str, str] | None = None,
        max_bytes: int = MAX_BODY_BYTES,
        headers: dict[str, str] | None = None,
        max_attempts: int = MAX_ATTEMPTS,
        deadline_s: float = REQUEST_DEADLINE_S,
    ) -> httpx.Response:
        """GET with optional extra headers (e.g. Authorization). Callers that put secrets in
        `params` or `headers` must scrub the errors they let escape (connectors/keyed.py).

        `deadline_s` is wall-clock time for one attempt, redirects and waits included. It also
        covers a server that drips response headers (the per-read timeouts never fire)."""
        host = (urlsplit(url).hostname or "").lower()
        last_status: int | None = None
        last_exc: Exception | None = None
        for attempt in range(1, max_attempts + 1):
            self._wait_for_host(host, min_delay_s)
            retry_after: float | None = None
            try:
                resp = _call_with_deadline(
                    lambda: self._read(url, params, allowed_hosts, min_delay_s, max_bytes, headers),
                    deadline_s,
                    url,
                )
            except FetchError as exc:
                exc.attempts = attempt
                raise
            except httpx.TransportError as exc:
                last_exc, last_status = exc, None
            else:
                if resp.status_code < 400:
                    return resp
                last_status = resp.status_code
                if resp.status_code not in RETRY_STATUSES:
                    raise FetchError(
                        f"HTTP {resp.status_code}", url=url, status=last_status, attempts=attempt
                    )
                last_exc = None
                ra = resp.headers.get("Retry-After", "")
                if ra.isdigit():
                    retry_after = min(float(ra), MAX_RETRY_AFTER_S)
            if attempt < max_attempts:
                pause = BACKOFF_S[attempt - 1] * random.uniform(0.9, 1.1)
                self._sleep(retry_after if retry_after is not None else pause)
        detail = type(last_exc).__name__ if last_exc else f"HTTP {last_status}"
        err = FetchError(
            f"{detail} after {max_attempts} attempts",
            url=url,
            status=last_status,
            attempts=max_attempts,
        )
        if last_exc is not None:
            raise err from last_exc
        raise err

    def post_json(
        self,
        url: str,
        *,
        payload: dict | None = None,
        allowed_hosts: frozenset[str],
        min_delay_s: float,
        timeout_s: float,
        max_bytes: int = MAX_POST_RESPONSE_BYTES,
        headers: dict[str, str] | None = None,
        form: dict[str, str] | None = None,
    ) -> httpx.Response:
        """One POST with a JSON body (or an urlencoded `form`), no retries (the caller decides:
        sends are not idempotent).

        Same URL policy as `get`; a redirect is an error. Unlike `get`, the body of a 4xx/5xx
        response is read (up to 256 KB): APIs put the reason there. Errors carry the full URL, so
        callers whose URL holds a secret must not let them escape (see notify/telegram.py).
        """
        host = (urlsplit(url).hostname or "").lower()
        self._wait_for_host(host, min_delay_s)
        if (payload is None) == (form is None):
            raise ValueError("exactly one of payload and form")
        request = self._client.build_request(
            "POST",
            url,
            json=payload,
            data=form,
            headers=headers,
            timeout=httpx.Timeout(timeout_s, connect=10.0),
        )
        self._check_url(request.url, allowed_hosts, None)
        resp = self._client.send(request, stream=True)
        try:
            if resp.is_redirect:
                raise FetchError(
                    "unexpected redirect", url=url, status=resp.status_code, attempts=1
                )
            chunks: list[bytes] = []
            deadline = self._clock() + timeout_s + 10
            try:
                for chunk in _decoded_chunks(resp, url, max_bytes, ResponseTooLarge):
                    if self._clock() > deadline:
                        raise FetchError(
                            "response too slow", url=url, status=resp.status_code, attempts=1
                        )
                    chunks.append(chunk)
            except ResponseTooLarge as exc:
                raise ResponseTooLarge(
                    "response too large", url=url, status=resp.status_code, attempts=1
                ) from exc
            headers = [
                (k, v)
                for k, v in resp.headers.multi_items()
                if k.lower() not in {"content-encoding", "content-length", "transfer-encoding"}
            ]
            return httpx.Response(
                resp.status_code, headers=headers, content=b"".join(chunks), request=resp.request
            )
        finally:
            resp.close()
