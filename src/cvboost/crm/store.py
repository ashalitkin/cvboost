"""CRM persistence. Plain functions over a connection; the caller owns the transaction."""

from __future__ import annotations

import hashlib
import re
import sqlite3
from datetime import UTC, date, datetime, timedelta
from urllib.parse import unquote, urlsplit

from cvboost.crm.parse import safe_linkedin_url
from cvboost.crm.types import (
    COLUMNS,
    CONTACT_KINDS,
    CONTEXT_MAX,
    NOTE_MAX,
    SNIPPET_MAX,
    STEP_MAX,
    ParsedEvent,
)
from cvboost.storage.db import iso

UNKNOWN_NAME = "LinkedIn (unparsed)"
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _utc(dt: datetime) -> datetime:
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


def name_key(name: str) -> str:
    return " ".join(name.casefold().split())


_SLUG = re.compile(r"/in/([^/?#]{1,200})")


def profile_slug(url: str | None) -> str | None:
    m = _SLUG.search(urlsplit(url).path) if url else None
    return unquote(m.group(1)).casefold() if m else None


def _find_contact(conn, slug: str | None, key: str):
    """Identity: the profile slug first (only when the name agrees); a name-only contact gets the
    slug attached; two slugs with one name are two contacts. Without a slug only contacts that
    have no slug are matched (newest, if several)."""
    if slug:
        row = conn.execute("SELECT * FROM crm_contacts WHERE profile_slug = ?", (slug,)).fetchone()
        if row is not None:
            return row if row["name_key"] == key else None  # see record_event
        return conn.execute(
            "SELECT * FROM crm_contacts WHERE name_key = ? AND profile_slug IS NULL "
            "ORDER BY last_event_at DESC, id DESC LIMIT 1",
            (key,),
        ).fetchone()
    return conn.execute(
        "SELECT * FROM crm_contacts WHERE name_key = ? AND profile_slug IS NULL "
        "ORDER BY last_event_at DESC, id DESC LIMIT 1",
        (key,),
    ).fetchone()


def dedupe_key(ev: ParsedEvent, received_at: datetime | None, message_id: str | None) -> str:
    if message_id:
        return f"{message_id.strip()[:200]}#{ev.index}"
    when = iso(_utc(received_at)) if received_at else ""  # no Date: a retry must still match
    raw = f"{ev.kind}|{when}|{ev.snippet}|{ev.name}"
    return "h:" + hashlib.sha256(raw.encode()).hexdigest()[:32]


def record_event(
    conn: sqlite3.Connection,
    ev: ParsedEvent,
    received_at: datetime | None,
    message_id: str | None,
    now: datetime,
) -> bool:
    """Store one event. False when it was already stored (same mail or same content).

    Order is by the event's own `received_at`: an OLDER event (a manually forwarded old mail)
    is stored in the history but never marks the contact unread or moves it.
    """
    received = iso(_utc(received_at or now))  # a mail without a Date counts as just received
    name = ev.name.strip()[:200] or UNKNOWN_NAME
    key = name_key(name)
    profile = safe_linkedin_url(ev.profile_url)
    slug = profile_slug(profile)
    row = _find_contact(conn, slug, key)
    if (
        row is None
        and slug
        and conn.execute("SELECT 1 FROM crm_contacts WHERE profile_slug = ?", (slug,)).fetchone()
    ):
        # the slug is taken by a contact with ANOTHER name: never merge on a link alone (the
        # event could carry someone else's profile link); handle it as a name-only event
        slug = profile = None
        row = _find_contact(conn, None, key)
    if row is None:
        cur = conn.execute(
            "INSERT INTO crm_contacts (name, name_key, headline, company, profile_url, "
            "profile_slug, last_event_at, unread, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,1,?,?)",
            (
                name,
                key,
                (ev.headline or None) and ev.headline[:300],
                (ev.company or None) and ev.company[:200],
                profile,
                slug,
                received,
                iso(now),
                iso(now),
            ),
        )
        cid = cur.lastrowid
        newest = True
    else:
        cid = row["id"]
        newest = row["last_event_at"] is None or received > row["last_event_at"]
    dk = dedupe_key(ev, received_at, message_id)
    cur = conn.execute(
        "INSERT OR IGNORE INTO crm_events (contact_id, kind, received_at, snippet, view_url, "
        "mail_message_id, dedupe_key, created_at) VALUES (?,?,?,?,?,?,?,?)",
        (
            cid,
            ev.kind,
            received,
            ev.snippet[:SNIPPET_MAX],
            safe_linkedin_url(ev.view_url),
            (message_id or None) and message_id[:200],
            dk,
            iso(now),
        ),
    )
    if cur.rowcount == 0:
        return False
    if row is None:
        return True
    # existing contact: fill what is missing; take the newest event's values when it is newest
    headline = ev.headline[:300] if ev.headline else None
    company = ev.company[:200] if ev.company else None
    if newest:
        conn.execute(
            "UPDATE crm_contacts SET headline = COALESCE(?, headline), "
            "company = COALESCE(?, company), profile_url = COALESCE(profile_url, ?), "
            "profile_slug = COALESCE(profile_slug, ?), last_event_at = ?, unread = 1, "
            "col = CASE WHEN archived = 1 THEN 'inbox' ELSE col END, archived = 0, "
            "updated_at = ? WHERE id = ?",
            (headline, company, profile, slug, received, iso(now), cid),
        )
    else:
        conn.execute(
            "UPDATE crm_contacts SET headline = COALESCE(headline, ?), "
            "company = COALESCE(company, ?), profile_url = COALESCE(profile_url, ?), "
            "profile_slug = COALESCE(profile_slug, ?), updated_at = ? WHERE id = ?",
            (headline, company, profile, slug, iso(now), cid),
        )
    return True


# --- reading ---

_ORDER = "ORDER BY last_event_at DESC, id DESC"


def columns(conn: sqlite3.Connection) -> dict[str, list[sqlite3.Row]]:
    out: dict[str, list[sqlite3.Row]] = {c: [] for c in COLUMNS}
    rows = conn.execute(
        f"SELECT * FROM crm_contacts WHERE archived = 0 {_ORDER}"  # noqa: S608 - constant
    ).fetchall()
    for r in rows:
        out[r["col"]].append(r)
    return out


def archived(conn: sqlite3.Connection, limit: int = 200) -> list[sqlite3.Row]:
    return conn.execute(
        f"SELECT * FROM crm_contacts WHERE archived = 1 {_ORDER} LIMIT ?",  # noqa: S608
        (limit,),
    ).fetchall()


def last_snippets(conn: sqlite3.Connection, ids: list[int]) -> dict[int, sqlite3.Row]:
    """Newest event per contact (for the list rows)."""
    out: dict[int, sqlite3.Row] = {}
    for cid in ids:
        r = conn.execute(
            "SELECT * FROM crm_events WHERE contact_id = ? ORDER BY received_at DESC, id DESC "
            "LIMIT 1",
            (cid,),
        ).fetchone()
        if r is not None:
            out[cid] = r
    return out


def contact(conn: sqlite3.Connection, cid: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM crm_contacts WHERE id = ?", (cid,)).fetchone()


def events(conn: sqlite3.Connection, cid: int, limit: int = 100) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM crm_events WHERE contact_id = ? ORDER BY received_at DESC, id DESC LIMIT ?",
        (cid, limit),
    ).fetchall()


_SUFFIX = re.compile(r"\b(inc|llc|ltd|limited|gmbh|corp|co|sa|ag|bv|plc|lda)\b\.?")


def _company_norm(s: str | None) -> str:
    s = _SUFFIX.sub(" ", (s or "").casefold())
    return re.sub(r"[^a-z0-9]+", "", s)


def related_jobs(conn: sqlite3.Connection, company: str | None, limit: int = 10) -> list[dict]:
    """Jobs of the same company (normalised name equal, legal suffix ignored)."""
    target = _company_norm(company)
    if len(target) < 3:
        return []
    word = re.sub(r"[%_\\]", "", (company or "").split()[0].casefold())
    rows = conn.execute(
        "SELECT id, title, company FROM jobs WHERE id = canonical_id AND lower(company) "
        "LIKE ? ESCAPE '\\' ORDER BY id DESC LIMIT 300",
        (f"%{word}%",),
    ).fetchall()
    out = [
        {"id": r["id"], "title": r["title"], "company": r["company"]}
        for r in rows
        if _company_norm(r["company"]) == target
    ]
    return out[:limit]


def stats(conn: sqlite3.Connection, now: datetime) -> dict:
    cols = {c: 0 for c in COLUMNS}
    for r in conn.execute(
        "SELECT col, COUNT(*) n FROM crm_contacts WHERE archived = 0 GROUP BY col"
    ):
        cols[r["col"]] = r["n"]
    arch = conn.execute("SELECT COUNT(*) FROM crm_contacts WHERE archived = 1").fetchone()[0]
    unread = conn.execute(
        "SELECT COUNT(*) FROM crm_contacts WHERE unread = 1 AND archived = 0"
    ).fetchone()[0]
    windows = {}
    for days in (7, 30):
        since = iso(now - timedelta(days=days))
        accepted = conn.execute(
            "SELECT DISTINCT contact_id, MIN(received_at) at FROM crm_events "
            "WHERE kind = 'invitation_accepted' AND received_at >= ? GROUP BY contact_id",
            (since,),
        ).fetchall()
        replied = sum(
            1
            for a in accepted
            if conn.execute(
                "SELECT 1 FROM crm_events WHERE contact_id = ? "
                "AND kind IN ('message', 'message_accepted') "
                "AND received_at >= ? LIMIT 1",
                (a["contact_id"], a["at"]),
            ).fetchone()
        )
        windows[days] = {"accepted": len(accepted), "replied": replied}
    return {
        "columns": cols,
        "archived": arch,
        "unread": unread,
        "windows": windows,
        "talk": cols["talk"],
    }


def unread_count(conn: sqlite3.Connection) -> int:
    return conn.execute(
        "SELECT COUNT(*) FROM crm_contacts WHERE unread = 1 AND archived = 0"
    ).fetchone()[0]


# --- owner actions ---


def mark_read(conn: sqlite3.Connection, cid: int, now: datetime) -> None:
    conn.execute(
        "UPDATE crm_contacts SET unread = 0, updated_at = ? WHERE id = ? AND unread = 1",
        (iso(now), cid),
    )


def mark_unread(conn: sqlite3.Connection, cid: int, now: datetime) -> None:
    conn.execute("UPDATE crm_contacts SET unread = 1, updated_at = ? WHERE id = ?", (iso(now), cid))


def move(conn: sqlite3.Connection, cid: int, col: str, now: datetime) -> bool:
    if col not in COLUMNS:
        return False
    cur = conn.execute(
        "UPDATE crm_contacts SET col = ?, archived = 0, updated_at = ? WHERE id = ?",
        (col, iso(now), cid),
    )
    return cur.rowcount == 1


def set_archived(conn: sqlite3.Connection, cid: int, value: bool, now: datetime) -> bool:
    cur = conn.execute(
        "UPDATE crm_contacts SET archived = ?, unread = CASE WHEN ? THEN 0 ELSE unread END, "
        "updated_at = ? WHERE id = ?",
        (int(value), int(value), iso(now), cid),
    )
    return cur.rowcount == 1


def set_note(conn: sqlite3.Connection, cid: int, kind: str, note: str, now: datetime) -> bool:
    if kind not in CONTACT_KINDS:
        return False
    note = note.replace("\r\n", "\n").strip()[:NOTE_MAX]
    cur = conn.execute(
        "UPDATE crm_contacts SET kind = ?, note = ?, updated_at = ? WHERE id = ?",
        (kind, note or None, iso(now), cid),
    )
    return cur.rowcount == 1


def set_owner_context(conn: sqlite3.Connection, cid: int, text: str, now: datetime) -> bool:
    """The owner's own text for drafts (his first message, notes). Empty clears it."""
    text = text.replace("\r\n", "\n").strip()[:CONTEXT_MAX]
    cur = conn.execute(
        "UPDATE crm_contacts SET owner_context = ?, updated_at = ? WHERE id = ?",
        (text or None, iso(now), cid),
    )
    return cur.rowcount == 1


def parse_due(value: str) -> str | None:
    value = value.strip()
    if not _DATE.match(value):
        return None
    try:
        date.fromisoformat(value)
    except ValueError:
        return None
    return value


def set_next(conn: sqlite3.Connection, cid: int, step: str, due: str, now: datetime) -> bool:
    step = " ".join(step.split())[:STEP_MAX]
    d = parse_due(due) if due.strip() else None
    if due.strip() and d is None:
        return False
    cur = conn.execute(
        "UPDATE crm_contacts SET next_step = ?, due_at = ?, updated_at = ? WHERE id = ?",
        (step or None, d if step else None, iso(now), cid),
    )
    return cur.rowcount == 1


# --- reminders ---


def due_contacts(conn: sqlite3.Connection, today: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM crm_contacts WHERE due_at IS NOT NULL AND next_step IS NOT NULL "
        "AND due_at <= ? ORDER BY due_at, id",
        (today,),
    ).fetchall()


# --- reply drafts (queue; one Opus call per request) ---


def request_reply(conn: sqlite3.Connection, cid: int, now: datetime) -> int | None:
    """Queue one draft request. None when the contact is missing or one is already active."""
    if contact(conn, cid) is None:
        return None
    cur = conn.execute(
        "INSERT OR IGNORE INTO crm_reply_requests (contact_id, status, requested_at) "
        "VALUES (?, 'queued', ?)",
        (cid, iso(now)),
    )
    return cur.lastrowid if cur.rowcount == 1 else None


def latest_reply(conn: sqlite3.Connection, cid: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM crm_reply_requests WHERE contact_id = ? ORDER BY id DESC LIMIT 1", (cid,)
    ).fetchone()


def has_queued_reply(conn: sqlite3.Connection, max_attempts: int) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM crm_reply_requests WHERE status = 'queued' AND attempts < ? LIMIT 1",
            (max_attempts,),
        ).fetchone()
        is not None
    )


def claim_next_reply(conn: sqlite3.Connection, now: datetime, max_attempts: int):
    return conn.execute(
        "UPDATE crm_reply_requests SET status = 'in_progress', started_at = ?1, "
        "attempts = attempts + 1 WHERE id = (SELECT id FROM crm_reply_requests WHERE "
        "status = 'queued' AND attempts < ?2 ORDER BY id LIMIT 1) RETURNING *",
        (iso(now), max_attempts),
    ).fetchone()


def requeue_reply(conn, rid: int, *, feedback: str | None, refund: bool) -> None:
    conn.execute(
        "UPDATE crm_reply_requests SET status = 'queued', started_at = NULL, feedback = ?, "
        "attempts = attempts - ? WHERE id = ? AND status = 'in_progress'",
        (feedback, int(refund), rid),
    )


def finish_reply(
    conn,
    rid: int,
    *,
    status: str,
    now: datetime,
    draft: str | None = None,
    error: str | None = None,
    call_id: int | None = None,
    prompt_version: str | None = None,
    profile_sha: str | None = None,
    email: tuple[str, str, str] | None = None,  # (to, subject, body)
) -> bool:
    to, subject, body = email or (None, None, None)
    cur = conn.execute(
        "UPDATE crm_reply_requests SET status = ?, finished_at = ?, draft = ?, error = ?, "
        "last_call_id = ?, prompt_version = ?, profile_sha = ?, feedback = NULL, "
        "email_to = ?, email_subject = ?, email_body = ? "
        "WHERE id = ? AND status = 'in_progress'",
        (status, iso(now), draft, (error or None) and error[:300], call_id, prompt_version,
         profile_sha, to, subject, body, rid),
    )  # fmt: skip
    return cur.rowcount == 1


def recover_stale_replies(conn, older_than: datetime, max_attempts: int, now: datetime) -> None:
    """A worker that died mid-call: back to the queue, or failed when attempts are used up."""
    conn.execute(
        "UPDATE crm_reply_requests SET status = 'failed', finished_at = ?, "
        "error = 'attempts used up' WHERE status = 'in_progress' AND started_at < ? "
        "AND attempts >= ?",
        (iso(now), iso(older_than), max_attempts),
    )
    conn.execute(
        "UPDATE crm_reply_requests SET status = 'queued', started_at = NULL "
        "WHERE status = 'in_progress' AND started_at < ?",
        (iso(older_than),),
    )
