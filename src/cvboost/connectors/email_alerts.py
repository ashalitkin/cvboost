"""E-mail job alerts from a dedicated mailbox (IMAP, read-only): LinkedIn, Dice, Indeed,
freelancermap; anything else is marked `needs_llm` for the Haiku fallback (17b).

Not a network connector in the HTTP sense: `requests()`/`parse()` are empty and `fetch_partial`
ignores the HTTP client. The job source of a posting is `email:<platform>` (declared in
`job_sources`), so the same job from LinkedIn mail and from the LinkedIn API-less boards dedupes
by (company, title) like any other source.

Safety: mail text is untrusted data; links are never opened, only matched against id patterns
and rebuilt (`mail.canonical`); the mailbox is only read (EXAMINE, BODY.PEEK); the password
never leaves `ImapCreds` and is redacted from errors. A run reads at most MAX_MESSAGES new
messages above the UID cursor (MAX(uid) in email_messages for the current UIDVALIDITY), skips
bodies over MAX_BYTES, and records every message exactly once in `email_messages`.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Callable, Collection
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING, ClassVar

import cvboost.crm.handler  # noqa: F401  (registers the CRM handler for LinkedIn messaging mail)
from cvboost.connectors.base import Connector, FetchOutcome, RequestFailure
from cvboost.mail.imap import ImapCreds, ImapMailbox, Mailbox, MailError
from cvboost.mail.message import ParsedEmail, parse_messages
from cvboost.mail.parsers.base import ParsedJob
from cvboost.mail.parsers.registry import PARSER_VERSION, classify
from cvboost.models import UNDISCLOSED_COMPANY, Job, RawPosting, SourceKind

if TYPE_CHECKING:
    from cvboost.storage.db import Store

log = logging.getLogger("cvboost.mail")

MAX_MESSAGES = 50
MAX_BYTES = 2 * 1024 * 1024
DAYS_BACK = 3

MailboxFactory = Callable[[ImapCreds], Mailbox]


def msgid_hash(p: ParsedEmail) -> str:
    key = p.message_id or f"{p.received_at}|{p.from_addr}|{p.subject}"
    return hashlib.sha256(key.encode("utf-8", "replace")).hexdigest()


class EmailAlertsConnector(Connector):
    name: ClassVar[str] = "email_alerts"
    kind: ClassVar[SourceKind] = "email"
    base_url: ClassVar[str] = "imaps://imap.gmail.com"
    interval: ClassVar[timedelta] = timedelta(minutes=10)
    min_delay_s: ClassVar[float] = 0.0
    allowed_hosts: ClassVar[frozenset[str]] = frozenset()  # no HTTP at all
    tos_note: ClassVar[str] = (
        "Own mailbox, read-only IMAP. Platform alert mails are parsed deterministically; links "
        "are never opened. Unknown senders go to a capped LLM fallback."
    )
    attribution: ClassVar[str] = "via e-mail alerts"
    job_sources: ClassVar[tuple[str, ...]] = (
        "email:linkedin",
        "email:dice",
        "email:indeed",
        "email:freelancermap",
        "email:recruiter",
    )
    job_source_hosts: ClassVar[dict[str, frozenset[str]]] = {
        "email:linkedin": frozenset({"linkedin.com"}),
        "email:dice": frozenset({"dice.com"}),
        "email:indeed": frozenset({"indeed.com"}),
        "email:freelancermap": frozenset({"freelancermap.com", "freelancermap.de"}),
        "email:recruiter": frozenset({"linkedin.com", "dice.com", "indeed.com"}),
    }
    # a posting without a platform link points at the original mail in the owner's Gmail
    job_source_hosts = {k: v | {"mail.google.com"} for k, v in job_source_hosts.items()}

    def __init__(
        self,
        creds: ImapCreds | None = None,
        *,
        forwarders: Collection[str] = (),
        mailbox_factory: MailboxFactory | None = None,
        days_back: int = DAYS_BACK,
        max_messages: int = MAX_MESSAGES,
    ):
        self._creds = creds
        self._forwarders = frozenset(f.lower() for f in forwarders)
        self._factory: MailboxFactory = mailbox_factory or ImapMailbox
        self._days_back = days_back
        self._max_messages = max_messages
        self._store: Store | None = None
        self._now: datetime | None = None
        self._rows: list[dict] = []

    def __repr__(self) -> str:  # never shows credentials
        return f"EmailAlertsConnector(configured={self.configured()})"

    def configured(self) -> bool:
        return self._creds is not None

    def requests(self) -> list[str]:
        return []

    def parse(self, body: bytes, url: str) -> list[RawPosting]:
        return []

    # --- run ----------------------------------------------------------------------------------

    def before_run(self, store: Store, now: datetime) -> None:
        self._store, self._now, self._rows = store, now, []

    def fetch_partial(self, http: object = None, limit: int | None = None) -> FetchOutcome:
        store, now, creds = self._store, self._now, self._creds
        if store is None or now is None or creds is None:
            raise MailError("mailbox not configured")
        cap = min(self._max_messages, limit) if limit else self._max_messages
        raws: list[RawPosting] = []
        failures: list[RequestFailure] = []
        seen_jobs: set[tuple[str, str]] = set()
        known = store.known_msgid_hashes()
        mailbox = self._factory(creds)
        try:
            uidv = mailbox.open()
            cursor = store.email_cursor(creds.mailbox, uidv)
            since: date = now.date() - timedelta(days=self._days_back)
            uids = [u for u in mailbox.search_uids(cursor + 1, since) if u > cursor][:cap]
            sizes = mailbox.sizes(uids) if uids else {}
            for uid in uids:
                base = {
                    "mailbox": creds.mailbox,
                    "uidvalidity": uidv,
                    "uid": uid,
                    "part": 0,
                }
                if sizes.get(uid, MAX_BYTES + 1) > MAX_BYTES:  # unknown size = too large
                    self._rows.append(_row(base, f"uid:{uidv}:{uid}", status="too_large"))
                    continue
                try:
                    data = mailbox.fetch(uid)
                except MailError:
                    failures.append(RequestFailure(f"uid:{uid}", "MailError"))
                    break  # connection trouble: retry these UIDs next run (no row, cursor stays)
                if len(data) > MAX_BYTES:
                    self._rows.append(_row(base, f"uid:{uidv}:{uid}", status="too_large"))
                    continue
                try:
                    self._handle(store, now, data, base, known, seen_jobs, raws)
                except Exception as exc:  # one broken message must not stop the others
                    failures.append(RequestFailure(f"uid:{uid}", type(exc).__name__))
                    self._rows.append(
                        _row(
                            base, f"uid:{uidv}:{uid}", status="parse_error", note=type(exc).__name__
                        )
                    )
        finally:
            mailbox.close()
        if failures and not raws and not self._rows:
            raise MailError(f"all messages failed ({failures[0].error_type})")
        return FetchOutcome(raws, failures, len(failures) + len(self._rows))

    def _handle(
        self,
        store: Store,
        now: datetime,
        data: bytes,
        base: dict,
        known: set[str],
        seen_jobs: set[tuple[str, str]],
        raws: list[RawPosting],
    ) -> None:
        for part, parsed in enumerate(parse_messages(data, forwarders=self._forwarders)):
            key = {**base, "part": part}
            h = msgid_hash(parsed)
            common = {
                "message_id": parsed.message_id,
                "sender_domain": parsed.from_domain[:100] or None,
                "received_at": parsed.received_at.isoformat() if parsed.received_at else None,
            }
            if h in known:
                self._rows.append(_row(key, h, status="duplicate", **common))
                continue
            known.add(h)
            res = classify(parsed, store, now)
            found = 0
            for job in res.jobs:
                ident = (f"email:{job.platform}", job.source_id)
                if ident in seen_jobs:
                    continue
                seen_jobs.add(ident)
                found += 1
                raws.append(_raw(job, h, parsed))
            self._rows.append(
                _row(
                    key,
                    h,
                    status=res.status,
                    platform=res.platform,
                    jobs_found=found,
                    llm_input=res.llm_input,
                    links=res.job_links,
                    note=res.note,
                    **common,
                )
            )

    def after_run(self, store: Store, now: datetime) -> None:
        rows, self._rows = self._rows, []
        if rows:
            store.record_email_messages(rows, store.latest_run_id(self.name), now)

    # --- normalise ----------------------------------------------------------------------------

    def normalise(self, raw: RawPosting, fetched_at: datetime) -> Job:
        d = raw.payload
        desc = d.get("description") or " ".join(
            x
            for x in (
                f"{d['title']} at {d['company']}.",
                f"Location: {d['location']}." if d.get("location") else "",
                f"Salary: {d['salary_text']}." if d.get("salary_text") else "",
                f"From a {d['platform']} e-mail alert; the full description is on the platform.",
            )
            if x
        )
        posted = d.get("posted_at") or d.get("received_at")
        return Job(
            source=raw.source,
            source_id=raw.source_id,
            url=d["url"],
            company=d["company"],
            title=d["title"],
            description_text=desc,
            location_text=d.get("location"),
            remote=d.get("remote"),
            employment_type=d.get("employment_type"),
            salary_text=d.get("salary_text"),
            posted_at=datetime.fromisoformat(posted) if posted else None,
            fetched_at=fetched_at,
            raw={
                "platform": d["platform"],
                "job_id": raw.source_id,
                "msgid_hash": d["msgid_hash"],
                "parser_version": PARSER_VERSION,
            },
        )


def _raw(job: ParsedJob, h: str, parsed: ParsedEmail) -> RawPosting:
    return RawPosting(
        source=f"email:{job.platform}",
        source_id=job.source_id,
        payload={
            "platform": job.platform,
            "url": job.url,
            "title": job.title,
            "company": job.company or UNDISCLOSED_COMPANY,
            "location": job.location,
            "salary_text": job.salary_text,
            "description": job.description,
            "remote": job.remote,
            "employment_type": job.employment_type,
            "posted_at": job.posted_at.isoformat() if job.posted_at else None,
            "received_at": parsed.received_at.isoformat() if parsed.received_at else None,
            "msgid_hash": h,
        },
    )


def _row(
    key: dict,
    h: str,
    *,
    status: str,
    message_id: str | None = None,
    sender_domain: str | None = None,
    received_at: str | None = None,
    platform: str | None = None,
    jobs_found: int = 0,
    llm_input: str | None = None,
    links: list[str] | None = None,
    note: str | None = None,
) -> dict:
    hh = h if len(h) == 64 else hashlib.sha256(h.encode()).hexdigest()
    return {
        **key,
        "msgid_hash": hh,
        "message_id": message_id[:300] if message_id else None,
        "sender_domain": sender_domain,
        "platform": platform,
        "received_at": received_at,
        "status": status,
        "jobs_found": jobs_found,
        "llm_input": llm_input,
        "links_json": json.dumps(links) if links else None,
        "note": (note or None) and note[:200],
    }


__all__ = ["EmailAlertsConnector"]
