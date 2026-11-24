"""/crm: contacts from LinkedIn mail in three columns, the contact page, owner actions (#33).

Every POST goes through the app-wide CSRF guard. Nothing here sends anything anywhere.
"""

from __future__ import annotations

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from cvboost.crm import store as S
from cvboost.crm.types import (
    COLUMN_LABELS,
    COLUMNS,
    CONTACT_KINDS,
    CONTEXT_MAX,
    EVENT_LABELS,
    NOTE_MAX,
    STEP_MAX,
    kind_labels,
)
from cvboost.web.routes import _now, open_store, render

REPLY_LABELS = {
    "queued": "queued for Opus",
    "in_progress": "Opus is writing a draft",
    "done": "ready",
    "failed": "failed",
    "cancelled": "cancelled",
}


def register(app: FastAPI) -> None:
    def _get(conn, cid: int):
        c = S.contact(conn, cid)
        if c is None:
            raise HTTPException(404, "Contact not found.")
        return c

    @app.get("/crm", response_class=HTMLResponse)
    def crm_page(request: Request):
        show_archive = request.query_params.get("archive") == "1"
        with open_store(request) as store:
            conn = store.conn
            cols = S.columns(conn)
            arch = S.archived(conn) if show_archive else []
            ids = [c["id"] for rows in cols.values() for c in rows] + [c["id"] for c in arch]
            ctx = {
                "active": "crm",
                "cols": [(k, COLUMN_LABELS[k], cols[k]) for k in COLUMNS],
                "col_labels": COLUMN_LABELS,
                "last": S.last_snippets(conn, ids),
                "archive": arch,
                "show_archive": show_archive,
                "stats": S.stats(conn, _now(request)),
                "kind_labels": kind_labels(),
                "event_labels": EVENT_LABELS,
                "columns": COLUMNS,
            }
        return render(request, "crm.html", ctx)

    @app.get("/crm/{cid}", response_class=HTMLResponse)
    def crm_contact(request: Request, cid: int):
        with open_store(request) as store:
            conn = store.conn
            c = _get(conn, cid)
            if c["unread"]:  # opening the contact reads it
                with store.transaction():
                    S.mark_read(conn, cid, _now(request))
            ctx = {
                "active": "crm",
                "c": S.contact(conn, cid),
                "events": S.events(conn, cid),
                "jobs": S.related_jobs(conn, c["company"]),
                "reply": S.latest_reply(conn, cid),
                "reply_labels": REPLY_LABELS,
                "kinds": CONTACT_KINDS,
                "kind_labels": kind_labels(),
                "col_labels": COLUMN_LABELS,
                "columns": COLUMNS,
                "event_labels": EVENT_LABELS,
                "note_max": NOTE_MAX,
                "context_max": CONTEXT_MAX,
                "step_max": STEP_MAX,
            }
        return render(request, "crm_contact.html", ctx)

    def _back(next_: str, cid: int) -> RedirectResponse:
        return RedirectResponse("/crm" if next_ == "list" else f"/crm/{cid}", status_code=303)

    @app.post("/crm/{cid}/read")
    def crm_read(
        request: Request, cid: int, unread: str = Form(""), next_: str = Form("list", alias="next")
    ):
        with open_store(request) as store:
            _get(store.conn, cid)
            with store.transaction():
                (S.mark_unread if unread == "1" else S.mark_read)(store.conn, cid, _now(request))
        return _back(next_, cid)

    @app.post("/crm/{cid}/move")
    def crm_move(
        request: Request, cid: int, col: str = Form(""), next_: str = Form("list", alias="next")
    ):
        if col not in COLUMNS:
            raise HTTPException(400, "Unknown column.")
        with open_store(request) as store:
            _get(store.conn, cid)
            with store.transaction():
                S.move(store.conn, cid, col, _now(request))
        return _back(next_, cid)

    @app.post("/crm/{cid}/archive")
    def crm_archive(
        request: Request, cid: int, restore: str = Form(""), next_: str = Form("list", alias="next")
    ):
        with open_store(request) as store:
            _get(store.conn, cid)
            with store.transaction():
                S.set_archived(store.conn, cid, restore != "1", _now(request))
        return _back(next_, cid)

    @app.post("/crm/{cid}/note")
    def crm_note(request: Request, cid: int, kind: str = Form("other"), note: str = Form("")):
        if kind not in CONTACT_KINDS:
            raise HTTPException(400, "Unknown type.")
        if len(note) > NOTE_MAX:
            raise HTTPException(400, f"The note is longer than {NOTE_MAX} characters.")
        with open_store(request) as store:
            _get(store.conn, cid)
            with store.transaction():
                S.set_note(store.conn, cid, kind, note, _now(request))
        return RedirectResponse(f"/crm/{cid}", status_code=303)

    @app.post("/crm/{cid}/context")
    def crm_context(request: Request, cid: int, owner_context: str = Form("")):
        owner_context = owner_context.replace("\r\n", "\n")  # browsers send CRLF: count after
        if len(owner_context.strip()) > CONTEXT_MAX:
            raise HTTPException(400, f"The context is longer than {CONTEXT_MAX} characters.")
        with open_store(request) as store:
            _get(store.conn, cid)
            with store.transaction():
                S.set_owner_context(store.conn, cid, owner_context, _now(request))
        return RedirectResponse(f"/crm/{cid}#context", status_code=303)

    @app.post("/crm/{cid}/next")
    def crm_next(
        request: Request, cid: int, step: str = Form(""), due: str = Form(""), done: str = Form("")
    ):
        if len(step) > STEP_MAX:
            raise HTTPException(400, f"The step is longer than {STEP_MAX} characters.")
        with open_store(request) as store:
            _get(store.conn, cid)
            with store.transaction():
                ok = S.set_next(store.conn, cid, "" if done == "1" else step, due, _now(request))
            if not ok:
                raise HTTPException(400, "The date must be in YYYY-MM-DD format.")
        return RedirectResponse(f"/crm/{cid}", status_code=303)

    @app.post("/crm/{cid}/reply")
    def crm_reply(request: Request, cid: int):
        """Queue one Opus draft (shared Opus cap). The owner copies and sends it himself."""
        llm = request.app.state.llm_cfg
        with open_store(request) as store:
            _get(store.conn, cid)
            if llm is None or not llm.cv.enabled:
                raise HTTPException(409, "Opus drafts are turned off in the config.")
            if not S.events(store.conn, cid, 1):
                raise HTTPException(409, "The contact has no events.")
            with store.transaction():
                S.request_reply(store.conn, cid, _now(request))
        return RedirectResponse(f"/crm/{cid}#reply", status_code=303)
