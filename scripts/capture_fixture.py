"""One-off, manual: capture a trimmed real response of a source into tests/fixtures/.

This is the ONLY code in the repo that is meant to hit the live network, and only when the
developer runs it by hand. Same HttpClient as production: UA cvboost/0.1, per-domain delay.

    uv run python scripts/capture_fixture.py --source remoteok --items 5
    uv run python scripts/capture_fixture.py --source hn_hiring --items 15
    uv run python scripts/capture_fixture.py --source wwr --categories \
        remote-back-end-programming-jobs remote-sales-and-marketing-jobs
    uv run python scripts/capture_fixture.py --source recruitee --slug kodify --items 6
    uv run python scripts/capture_fixture.py --source fractionaljobs --items 8

ATS platforms take ONE company (`--slug`): the file is tests/fixtures/<ats>/<slug>.json|xml, cut to
`--items` postings, those with a title the prefilter keeps first, the rest filled up in order.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from urllib.parse import urlsplit

from defusedxml import ElementTree as ET

from cvboost import connectors
from cvboost.connectors.ats import ATSConnector, title_matches
from cvboost.connectors.fractionaljobs import FractionalJobsConnector
from cvboost.connectors.hn_hiring import HNHiringConnector, first_line
from cvboost.connectors.wwr import WWRConnector
from cvboost.http import FetchError, HttpClient

FIXTURES = Path(__file__).resolve().parent.parent / "tests" / "fixtures"
MAX_BYTES = 150 * 1024


def trim_json(source: str, body: bytes, items: int) -> bytes:
    data = json.loads(body)
    if source == "remotive":
        data["jobs"] = data["jobs"][:items]
    elif source == "remoteok":
        data = data[:1] + [d for d in data[1:] if d.get("id")][:items]
    elif source == "himalayas":
        data["jobs"] = data["jobs"][:items]
    elif source == "jobicy":
        data["jobs"] = data["jobs"][:items]  # nextCursor is kept as returned
    elif source == "workingnomads":
        data = data[:items]
    elif source == "arbeitnow":
        data["data"] = data["data"][:items]  # links and meta are kept
    else:
        raise SystemExit(f"no JSON trimmer for {source}")
    return _dump(data)


def _dump(data: object) -> bytes:
    return (json.dumps(data, ensure_ascii=False, indent=1) + "\n").encode()


def trim_hn_search(body: bytes) -> bytes:
    data = json.loads(body)
    data["hits"] = [_strip_highlight(h) for h in data["hits"][:4]]  # both threads of the month
    return _dump(data)


def _strip_highlight(hit: dict) -> dict:
    return {k: v for k, v in hit.items() if k != "_highlightResult"}


def trim_hn_comments(body: bytes, items: int) -> bytes:
    """First `items` top-level postings whose first line has `|`, plus two replies."""
    data = json.loads(body)
    top, replies = [], []
    for hit in data["hits"]:
        if hit.get("parent_id") == hit.get("story_id"):
            if (
                len(top) < items
                and "|" in first_line(hit.get("comment_text") or "")
                and (hit.get("comment_text") or "").strip()
            ):
                top.append(_strip_highlight(hit))
        elif len(replies) < 2:
            replies.append(_strip_highlight(hit))
    data["hits"] = top + replies
    return _dump(data)


def capture_hn(http: HttpClient, connector: HNHiringConnector, items: int) -> int:
    out_dir = FIXTURES / "hn_hiring"
    out_dir.mkdir(parents=True, exist_ok=True)
    kw = {"min_delay_s": connector.min_delay_s, "allowed_hosts": connector.allowed_hosts}
    search = http.get(connector.base_url, **kw)
    story_id = connector.pick_thread(search.content)
    if story_id is None:
        print("STOP: no current 'Who is hiring?' thread in the search result")
        return 1
    comments = http.get(connector.comments_url(story_id, 0), **kw)
    for name, data in (
        ("search", trim_hn_search(search.content)),
        ("comments", trim_hn_comments(comments.content, items)),
    ):
        if len(data) > MAX_BYTES:
            print(f"WARNING: {name} is {len(data)} bytes (> {MAX_BYTES})")
        path = out_dir / f"{name}.json"
        path.write_bytes(data)
        print(f"{path.relative_to(FIXTURES.parent.parent)} ({len(data)} bytes)")
    return 0


def pick(rows: list, title_of, items: int) -> list:
    """`items` rows, the ones with a title the prefilter keeps first, original order kept."""
    keep = [i for i, r in enumerate(rows) if title_matches(title_of(r))][: max(items // 2, 1)]
    for i in range(len(rows)):
        if len(keep) >= items:
            break
        if i not in keep:
            keep.append(i)
    return [rows[i] for i in sorted(keep)]


def trim_ats_json(connector: ATSConnector, body: bytes, items: int) -> bytes:
    data = json.loads(body)
    title = connector.posting_title
    if isinstance(data, list):  # gem
        data = pick(data, lambda r: title(r), items)
    else:
        key = "offers" if "offers" in data else "jobs"  # recruitee / workable
        data[key] = pick(data[key], lambda r: title(r), items)
    return _dump(data)


def trim_ats_xml(connector: ATSConnector, body: bytes, items: int) -> bytes:
    """Teamtailor `<item>` / Personio `<position>` blocks: text surgery keeps the real markup."""
    text = body.decode("utf-8")
    tag = "item" if "<item>" in text else "position"
    head, *rest = text.split(f"<{tag}>")
    blocks = [f"<{tag}>" + b for b in rest]
    last = blocks[-1].split(f"</{tag}>")
    tail = last[-1]
    blocks[-1] = f"</{tag}>".join(last[:-1]) + f"</{tag}>"
    title_tag = "title" if tag == "item" else "name"

    def title_of(block: str) -> str:
        a = block.find(f"<{title_tag}>")
        b = block.find(f"</{title_tag}>")
        return block[a + len(title_tag) + 2 : b] if a >= 0 <= b else ""

    chosen = pick(blocks, title_of, items)
    out = (head + "".join(chosen) + tail).encode()
    ET.fromstring(out)  # must still be well-formed
    return out


def trim_rss(body: bytes, items: int) -> bytes:
    text = body.decode("utf-8")
    parts = text.split("</item>")
    kept = "</item>".join(parts[:items]) + "</item>" + parts[-1] if len(parts) > items + 1 else text
    ET.fromstring(kept.encode())  # must still be well-formed
    return kept.encode()


def capture_ats(connector: ATSConnector, slug: str | None, items: int, out_dir: Path) -> int:
    if not slug:
        print("STOP: an ATS platform needs --slug <company>")
        return 1
    url = connector.url_for(slug)  # validates the slug
    http = HttpClient()
    try:
        resp = http.get(
            url, min_delay_s=connector.min_delay_s, allowed_hosts=connector.allowed_hosts_for(slug)
        )
    except FetchError as exc:
        print(f"STOP: {url} -> {exc} (status {exc.status}); not working around it")
        return 1
    finally:
        http.close()
    xml = resp.content.lstrip().startswith(b"<")
    data = (
        trim_ats_xml(connector, resp.content, items)
        if xml
        else trim_ats_json(connector, resp.content, items)
    )
    if len(data) > MAX_BYTES:
        print(f"WARNING: {slug} is {len(data)} bytes (> {MAX_BYTES})")
    path = out_dir / f"{slug}.{'xml' if xml else 'json'}"
    path.write_bytes(data)
    print(f"{url} -> {path.relative_to(FIXTURES.parent.parent)} ({len(data)} bytes)")
    return 0


def capture_fractionaljobs(http: HttpClient, connector: FractionalJobsConnector, items: int) -> int:
    """home.html = `items` job cards (relevant functions first); detail.html = one detail page."""
    from selectolax.lexbor import LexborHTMLParser

    out_dir = FIXTURES / "fractionaljobs"
    out_dir.mkdir(parents=True, exist_ok=True)
    kw = {"min_delay_s": connector.min_delay_s, "allowed_hosts": connector.allowed_hosts}
    home = http.get(connector.base_url, **kw)
    cards = LexborHTMLParser(home.text).css("div.job-item")
    raws = connector.parse(home.content, connector.base_url)
    order = sorted(
        range(len(raws)),
        key=lambda i: raws[i].payload["function"] not in ("Engineering", "Marketing", "Other"),
    )
    chosen = sorted(order[:items])
    page = (
        '<html><body><div class="jobs-collection-list">'
        + "".join(cards[i].html for i in chosen)
        + "</div></body></html>\n"
    )
    (out_dir / "home.html").write_text(page)
    slug = raws[chosen[0]].payload["slug"]
    detail = http.get(f"{connector.base_url}jobs/{slug}", **kw)
    body = FractionalJobsConnector.parse_detail(detail.content)
    (out_dir / "detail.html").write_text(f"<html><body>{body}</body></html>\n")
    print(f"fractionaljobs: {len(chosen)} cards, detail of {slug}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", required=True)
    ap.add_argument("--items", type=int, default=5)
    ap.add_argument("--categories", nargs="*")
    ap.add_argument("--slug", help="ATS platforms: the one company to capture")
    args = ap.parse_args()

    if args.source == "wwr" and args.categories:
        connector = WWRConnector(args.categories)
    else:
        connector = connectors.REGISTRY[args.source]()
    out_dir = FIXTURES / args.source
    out_dir.mkdir(parents=True, exist_ok=True)
    if isinstance(connector, ATSConnector):
        return capture_ats(connector, args.slug, args.items, out_dir)
    connector.max_pages = 1  # one live request per source (HN: two)
    http = HttpClient()
    try:
        if isinstance(connector, FractionalJobsConnector):
            try:
                return capture_fractionaljobs(http, connector, args.items)
            except FetchError as exc:
                print(f"STOP: {exc.url} -> {exc} (status {exc.status}); not working around it")
                return 1
        if isinstance(connector, HNHiringConnector):
            try:
                return capture_hn(http, connector, args.items)
            except FetchError as exc:
                print(f"STOP: {exc.url} -> {exc} (status {exc.status}); not working around it")
                return 1
        urls = connector.requests() if args.source == "wwr" else connector.requests()[:1]
        for url in urls:
            try:
                resp = http.get(
                    url,
                    min_delay_s=connector.min_delay_s,
                    allowed_hosts=connector.allowed_hosts,
                )
            except FetchError as exc:
                print(f"STOP: {url} -> {exc} (status {exc.status}); not working around it")
                return 1
            if connector.kind == "rss":
                data, ext = trim_rss(resp.content, args.items), "xml"
                name = Path(urlsplit(url).path).stem
            else:
                data, ext = trim_json(args.source, resp.content, args.items), "json"
                name = "response"
            if len(data) > MAX_BYTES:
                print(f"WARNING: {name} is {len(data)} bytes (> {MAX_BYTES})")
            path = out_dir / f"{name}.{ext}"
            path.write_bytes(data)
            print(f"{url} -> {path.relative_to(FIXTURES.parent.parent)} ({len(data)} bytes)")
    finally:
        http.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
