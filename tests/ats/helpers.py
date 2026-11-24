from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import httpx
import yaml

from cvboost.http import HttpClient
from tests.conftest import FIXTURES, PUBLIC_RESOLVER

HOSTS = {
    "greenhouse": "boards-api.greenhouse.io",
    "lever": "api.lever.co",
    "ashby": "api.ashbyhq.com",
}


def write_companies(path: Path, rows: list[tuple[str, str, str]]) -> Path:
    path.write_text(
        yaml.safe_dump(
            {"companies": [{"ats": a, "slug": s, "name": n, "source": "test"} for a, s, n in rows]}
        ),
        encoding="utf-8",
    )
    return path


def ats_http(
    responses: dict[str, httpx.Response | Exception | Callable[[], httpx.Response]],
    seen: list[str] | None = None,
    sleeps: list[float] | None = None,
) -> HttpClient:
    """Mock transport keyed by the company slug (third path segment); default 404."""

    def handler(req: httpx.Request) -> httpx.Response:
        slug = req.url.path.strip("/").split("/")[2]
        if seen is not None:
            seen.append(f"{req.url.host}/{slug}")
        r = responses.get(slug, httpx.Response(404))
        if isinstance(r, Exception):
            raise r
        return r() if callable(r) else r

    return HttpClient(
        httpx.MockTransport(handler),
        resolver=PUBLIC_RESOLVER,
        sleep=(sleeps.append if sleeps is not None else (lambda s: None)),
    )


def fixture_response(ats: str, name: str) -> httpx.Response:
    return httpx.Response(200, content=(FIXTURES / ats / f"{name}.json").read_bytes())


def host_http(
    responses: dict[str, httpx.Response | Exception | Callable[[], httpx.Response]],
    seen: list[str] | None = None,
) -> HttpClient:
    """Mock transport keyed by the request HOST (sub-domain platforms); default 404."""

    def handler(req: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(f"{req.method} {req.url.host}{req.url.path}")
        r = responses.get(req.url.host, httpx.Response(404))
        if isinstance(r, Exception):
            raise r
        return r() if callable(r) else r

    return HttpClient(httpx.MockTransport(handler), resolver=PUBLIC_RESOLVER, sleep=lambda s: None)


def file_response(ats: str, name: str, ext: str) -> httpx.Response:
    return httpx.Response(200, content=(FIXTURES / ats / f"{name}.{ext}").read_bytes())
