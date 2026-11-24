"""The ATS company list (`config/companies.yaml`) and `cvboost companies discover`.

A company is {ats, slug, name, source}. The slug goes into the URL of a fixed platform (a path or a
sub-domain label), so it is validated against ^[a-z0-9-]{1,64}$ everywhere it enters the system
(file, discovery, URL builder). Discovery is offline over the jobs DB (opened read-only) plus one
live download of the remoteintech/remote-jobs repository archive.
"""

from __future__ import annotations

import io
import logging
import os
import re
import sqlite3
import tarfile
from collections import Counter
from pathlib import Path
from typing import Literal
from urllib.parse import parse_qs, urlsplit

import yaml
from pydantic import BaseModel, ConfigDict, field_validator

from cvboost.http import HttpClient

log = logging.getLogger("cvboost.companies")

ATS = Literal[
    "greenhouse", "lever", "ashby", "workable", "recruitee", "teamtailor", "personio", "gem"
]
ATS_NAMES: tuple[str, ...] = (
    "greenhouse", "lever", "ashby", "workable", "recruitee", "teamtailor", "personio", "gem",
)  # fmt: skip
SLUG_RE = re.compile(r"^[a-z0-9-]{1,64}$")

# remoteintech/remote-jobs moved to an Eleventy site: one profile per company in src/companies/.
# The repository archive is one request to GitHub's official download host (no per-file crawl).
REMOTE_JOBS_ARCHIVE = "https://codeload.github.com/remoteintech/remote-jobs/tar.gz/refs/heads/main"
ARCHIVE_HOSTS = frozenset({"codeload.github.com"})
MAX_PROFILE_BYTES = 200 * 1024
MAX_PROFILES = 5000
MAX_ARCHIVE_UNPACKED = 40 * 1024 * 1024  # sum of the sizes of ALL members, not only profiles
MAX_ARCHIVE_MEMBERS = 20000  # ALL members
MAX_ARCHIVE_COMPRESSED = 5 * 1024 * 1024  # the download is ~2.6 MB
MAX_NEW_FROM_DESCRIPTIONS = 20  # new slugs a single discover run takes from `db:description`

# Hosts of the public job pages; the first path segment is the company slug.
_PAGE_HOSTS: dict[str, str] = {
    "boards.greenhouse.io": "greenhouse",
    "job-boards.greenhouse.io": "greenhouse",
    "jobs.lever.co": "lever",
    "jobs.ashbyhq.com": "ashby",
}
# Segments that are not companies (Greenhouse embeds, API paths).
_NOT_SLUGS = frozenset({"embed", "api", "v1", "jobs", "www", "static"})
# One flat character class and a bounded repeat: linear time, no nested quantifiers.
_PAGE_URL = re.compile(
    r"https?://(boards\.greenhouse\.io|job-boards\.greenhouse\.io|jobs\.lever\.co|"
    r"jobs\.ashbyhq\.com)/([A-Za-z0-9_.%-]{1,80})",
    re.IGNORECASE,
)
_FRONT = re.compile(r"\A---\r?\n(.*?)\r?\n---", re.DOTALL)


class Company(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ats: ATS
    slug: str
    name: str = ""
    source: str = ""

    @field_validator("slug")
    @classmethod
    def _slug(cls, v: str) -> str:
        if not SLUG_RE.fullmatch(v):  # fullmatch: `$` alone would accept a trailing newline
            raise ValueError("slug must match ^[a-z0-9-]{1,64}$")
        return v

    @field_validator("name", "source")
    @classmethod
    def _text(cls, v: str) -> str:
        return re.sub(r"\s+", " ", v).strip()[:200]


def valid_slug(slug: object) -> bool:
    return isinstance(slug, str) and SLUG_RE.fullmatch(slug) is not None


def ats_slug_from_url(url: str | None) -> tuple[str, str] | None:
    """(ats, slug) for a public job-page URL of Greenhouse, Lever or Ashby, else None."""
    if not url:
        return None
    try:
        parts = urlsplit(url.strip())
        host = (parts.hostname or "").lower()
    except ValueError:
        return None
    ats = _PAGE_HOSTS.get(host)
    if ats is None:
        return None
    seg = parts.path.lstrip("/").split("/", 1)[0].strip().lower()
    if seg == "embed":  # boards.greenhouse.io/embed/job_board?for=<slug>
        seg = (parse_qs(parts.query).get("for") or [""])[0].strip().lower()
    if seg in _NOT_SLUGS or not valid_slug(seg):
        return None
    return ats, seg


def find_ats_urls(text: str | None) -> list[tuple[str, str]]:
    """Every (ats, slug) whose job-page URL occurs in free text."""
    found: list[tuple[str, str]] = []
    for m in _PAGE_URL.finditer(text or ""):
        hit = ats_slug_from_url(m.group(0))
        if hit and hit not in found:
            found.append(hit)
    return found


# ---- the yaml file --------------------------------------------------------------------------


def load_companies(path: Path | str) -> tuple[list[Company], list[str]]:
    """(valid companies, deduplicated by (ats, slug); descriptions of rejected entries).

    A missing file is an empty list. A bad entry is rejected and reported, never fatal: one typo
    must not stop the worker.
    """
    p = Path(path)
    if not p.exists():
        return [], []
    try:
        data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except (yaml.YAMLError, OSError) as exc:
        raise ValueError(f"bad companies file {p}: {exc}") from exc
    items = data.get("companies") if isinstance(data, dict) else None
    if not isinstance(items, list):
        raise ValueError(f"bad companies file {p}: expected a `companies:` list")
    ok: dict[tuple[str, str], Company] = {}
    rejected: list[str] = []
    for i, item in enumerate(items):
        try:
            c = Company.model_validate(item)
        except ValueError:
            rejected.append(f"entry {i + 1}: {str(item)[:80]!r}")
            continue
        ok.setdefault((c.ats, c.slug), c)
    return list(ok.values()), rejected


HEADER = (
    "# ATS companies polled by the ATS connectors (greenhouse, lever, ashby, workable,\n"
    "# recruitee, teamtailor, personio, gem).\n"
    "# Fields: ats, slug (^[a-z0-9-]{1,64}$, goes only into the platform URL), name, source.\n"
    "# `cvboost companies discover` adds entries. A company answering 404/410 twice\n"
    "# (6 h apart) is marked inactive in the DB (ats_companies); it is\n"
    "# re-checked every 30 days, and\n"
    "# `cvboost companies reactivate <ats> <slug>` polls it again at once.\n"
)


def write_companies(path: Path | str, companies: list[Company]) -> None:
    p = Path(path)
    rows = sorted(companies, key=lambda c: (c.ats, c.slug))
    body = yaml.safe_dump(
        {"companies": [c.model_dump() for c in rows]},
        sort_keys=False,
        allow_unicode=True,
        width=100,
    )
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(HEADER + body, encoding="utf-8")
    os.replace(tmp, p)


def merge(existing: list[Company], found: list[Company]) -> list[Company]:
    """Existing entries win (their name and source are kept); new (ats, slug) are appended."""
    out = {(c.ats, c.slug): c for c in existing}
    for c in found:
        out.setdefault((c.ats, c.slug), c)
    return list(out.values())


# ---- discovery ---------------------------------------------------------------------------------


def _make(ats: str, slug: str, name: str, source: str) -> Company | None:
    try:
        return Company(ats=ats, slug=slug, name=name or slug, source=source)  # type: ignore[arg-type]
    except ValueError:
        return None


def discover_from_db(db_path: Path | str) -> list[Company]:
    """Reverse search over already collected jobs: url, apply_url and the posting text.

    The DB is opened read-only (`mode=ro`): safe next to a running worker, never migrates.
    """
    uri = f"file:{Path(db_path).resolve()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=5)
    try:
        found: dict[tuple[str, str], Company] = {}
        rows = conn.execute("SELECT company, url, apply_url, description_html FROM jobs")
        for company, url, apply_url, html in rows:
            for src, text in (("url", url), ("apply_url", apply_url)):
                hit = ats_slug_from_url(text)
                c = _make(*hit, company or "", f"db:{src}") if hit else None
                if c:
                    found.setdefault((c.ats, c.slug), c)
            for ats, slug in find_ats_urls(html):
                c = _make(ats, slug, company or "", "db:description")
                if c:
                    found.setdefault((ats, slug), c)
        return list(found.values())
    finally:
        conn.close()


def fetch_remote_jobs_archive(http: HttpClient) -> bytes:
    """One GET of the repository archive (about 2.6 MB)."""
    return http.get(
        REMOTE_JOBS_ARCHIVE,
        min_delay_s=1.0,
        allowed_hosts=ARCHIVE_HOSTS,
        max_bytes=MAX_ARCHIVE_COMPRESSED,
    ).content


def parse_remote_jobs_archive(data: bytes) -> list[Company]:
    """Companies on Greenhouse/Lever/Ashby named in the profiles of remoteintech/remote-jobs.

    The archive is read in memory, never extracted; sizes are capped (decompression bomb) and
    only `src/companies/*.md` is looked at.
    """
    found: dict[tuple[str, str], Company] = {}
    seen_profiles = 0
    members = 0
    unpacked = 0
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        for member in tar:
            members += 1
            unpacked += max(member.size, 0)
            if members > MAX_ARCHIVE_MEMBERS or unpacked > MAX_ARCHIVE_UNPACKED:
                log.warning("remote-jobs archive over the size limits: stopped reading")
                break
            parts = member.name.split("/")
            if not (
                member.isfile()
                and len(parts) == 4
                and parts[1:3] == ["src", "companies"]
                and parts[3].endswith(".md")
            ):
                continue
            seen_profiles += 1
            if seen_profiles > MAX_PROFILES or member.size > MAX_PROFILE_BYTES:
                continue
            f = tar.extractfile(member)
            if f is None:
                continue
            text = f.read(MAX_PROFILE_BYTES).decode("utf-8", errors="replace")
            name = parts[3][:-3]
            m = _FRONT.match(text)
            if m:
                try:
                    front = yaml.safe_load(m.group(1))
                except yaml.YAMLError:
                    front = None
                if isinstance(front, dict) and isinstance(front.get("title"), str):
                    name = front["title"]
            for ats, slug in find_ats_urls(text):
                c = _make(ats, slug, name, "remoteintech")
                if c:
                    found.setdefault((ats, slug), c)
    return list(found.values())


def cap_description_finds(
    found: list[Company], existing: list[Company], cap: int = MAX_NEW_FROM_DESCRIPTIONS
) -> tuple[list[Company], int, int]:
    """Limit NEW companies found only in job descriptions (low confidence: any text can name
    any board). Returns (kept, new_from_descriptions, dropped); known companies are never cut."""
    known = {(c.ats, c.slug) for c in existing}
    kept: list[Company] = []
    new = dropped = 0
    for c in found:
        if c.source == "db:description" and (c.ats, c.slug) not in known:
            new += 1
            if new > cap:
                dropped += 1
                continue
        kept.append(c)
    return kept, new, dropped


def per_ats(companies: list[Company]) -> dict[str, int]:
    n = Counter(c.ats for c in companies)
    return {a: n.get(a, 0) for a in ATS_NAMES}
