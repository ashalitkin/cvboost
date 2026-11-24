"""Pages and actions. Sync handlers (threadpool) with a SQLite connection per request."""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    PlainTextResponse,
    RedirectResponse,
    Response,
)

from cvboost import connectors
from cvboost import stats as statslib
from cvboost.core.freshness import closed_reason  # noqa: F401  (documented dependency)
from cvboost.cv import kit as kitlib
from cvboost.cv.render import RenderError, RendererUnavailable
from cvboost.explain import Why, reason_labels, why_for
from cvboost.inbox import (
    DECISION_VALUES,
    ELIGIBLE_LABELS,
    ELIGIBLE_VALUES,
    SORT_LABELS,
    SORT_VALUES,
    TYPE_LABELS,
    TYPE_VALUES,
    InboxFilters,
    InboxResult,
    Sighting,
    attribution_for,
    closed_members,
    current_decision,
    inbox_result,
)
from cvboost.llm.prompts import registry
from cvboost.llm.sanitize import normalize_text
from cvboost.score import local_day
from cvboost.sources_view import MAX_INTERVAL_S, source_rows
from cvboost.storage.db import APPLICATION_STATUSES, Store, connect
from cvboost.web import security

KIT_FILES = {
    "resume.md": "text/plain; charset=utf-8",
    "resume.pdf": "application/pdf",
    "recruiter_note.md": "text/plain; charset=utf-8",
    "fit_notes.md": "text/plain; charset=utf-8",
    "cover_letter.md": "text/plain; charset=utf-8",
    "cover_letter.pdf": "application/pdf",
}
KIT_TEXT_MAX = 200_000
RESUME_MAX = 20_000
COVER_EDIT_MAX = 6_000
BUSY = "The kit is being generated right now, try again later."
REASONS = {
    "stack": "Stack",
    "rate": "Rate",
    "location": "Location",
    "seniority": "Seniority",
    "other": "Other",
}
APP_LABELS = {
    "preparing": "Preparing CV",
    "ready": "Ready",
    "applied": "Applied",
    "replied": "Replied",
    "interview": "Interview",
    "rejected": "Rejected",
    "withdrawn": "Withdrawn",
    "offer": "Offer",
    "cv_failed": "CV failed",
    "needs_review": "Ready — review",
}
CV_LABELS = {
    "queued": "queued",
    "in_progress": "in progress",
    "done": "done",
    "failed": "failed",
    "cancelled": "cancelled",
    "needs_review": "needs review",
}
FUNNEL = ["preparing", "ready", "applied", "replied", "interview", "offer", "rejected"]
# Applications board: fixed left-to-right pipeline order (owner 2026-11-10: columns must never
# move). BOARD_ALWAYS columns are shown even when empty; the rest only when used, at the end.
BOARD_ALWAYS = ["preparing", "needs_review", *FUNNEL[1:]]
BOARD_ORDER = [*BOARD_ALWAYS, "cv_failed", "withdrawn"]
NOTE_MAX = 500
EXTRA_MAX = 20000
NAV = [
    ("inbox", "/inbox", "Inbox"),
    ("screened", "/screened", "Screened out by AI"),
    ("applications", "/applications", "Applications"),
    ("crm", "/crm", "CRM"),
    ("sources", "/sources", "Sources"),
    ("usage", "/usage", "Usage"),
    ("stats", "/stats", "Statistics"),
]

# --- helpers ---


def _unconfigured(request) -> frozenset[str]:
    """Connectors that need credentials the settings do not have."""
    return frozenset() if request.app.state.settings.imap_creds() else frozenset({"email_alerts"})


@contextmanager
def open_store(request: Request) -> Iterator[Store]:
    conn = connect(request.app.state.settings.db_path)
    try:
        yield Store(conn)
    finally:
        conn.close()


def render(request: Request, name: str, ctx: dict, status: int = 200) -> HTMLResponse:
    st = request.app.state
    token = security.csrf_token_for(request)
    base = {"csrf_token": token, "nav": st.nav, "active": "", "app_labels": APP_LABELS}
    resp = st.templates.TemplateResponse(request, name, {**base, **ctx}, status_code=status)
    fresh = getattr(request.state, "csrf_new", None)
    if fresh is not None:
        resp.set_cookie(
            security.COOKIE,
            fresh,
            httponly=True,
            samesite="strict",
            path="/",
            secure=request.url.scheme == "https",
        )
    return resp


def error_page(request: Request, status: int, message: str) -> HTMLResponse:
    ctx = {
        "csrf_token": "",
        "nav": request.app.state.nav,
        "active": "",
        "status": status,
        "message": message,
    }
    return request.app.state.templates.TemplateResponse(
        request, "error.html", ctx, status_code=status
    )


def _now(request: Request) -> datetime:
    return request.app.state.clock()


def _max_age(request: Request) -> int:
    return request.app.state.worker_cfg.alerts.max_age_days


def _to_int(value: str | None, lo: int, hi: int) -> int | None:
    try:
        n = int(value) if value not in (None, "") else None
    except ValueError:
        return None
    return n if n is not None and lo <= n <= hi else None


def _why(request: Request, store: Store, cids: list[int]) -> dict[int, Why]:
    st = request.app.state
    return why_for(
        store,
        cids,
        rule_version=st.rule_version,
        threshold=st.rules_cfg.relevance.threshold,
        prompt_version=registry.score_version(),
        labels=reason_labels(st.rules_cfg),
    )


def _inbox(
    request: Request, store: Store, filters: InboxFilters, only_cid: int | None = None
) -> InboxResult:
    st = request.app.state
    return inbox_result(
        store,
        rule_version=st.rule_version,
        threshold=st.rules_cfg.relevance.threshold,
        prompt_version=registry.score_version(),
        max_age_days=_max_age(request),
        filters=filters,
        now=_now(request),
        only_cid=only_cid,
        reason_labels=reason_labels(st.rules_cfg),
    )


def _view_params(request: Request) -> tuple[tuple[str, ...], bool, str]:
    """Employment types (repeated `type=`), "unseen only", sort: all from the URL."""
    q = request.query_params
    types = tuple(t for t in TYPE_VALUES if t in set(q.getlist("type")))
    sort = q.get("sort", "")
    return types, q.get("unseen") == "1", sort if sort in SORT_VALUES else "fit"


def _view_ctx(types: tuple[str, ...], unseen_only: bool, sort: str, res: InboxResult) -> dict:
    return {
        "items": res.items,
        "total": res.total,
        "capped": res.capped,
        "unseen_count": res.unseen,
        "type_values": TYPE_VALUES,
        "type_labels": TYPE_LABELS,
        "sort_values": SORT_VALUES,
        "sort_labels": SORT_LABELS,
        "sel_types": types,
        "unseen_only": unseen_only,
        "sort": sort,
    }


@dataclass
class Group:
    cid: int
    members: list  # all rows
    alive: list  # rows still open at the source
    closed: dict[int, str | None]

    @property
    def rep(self):
        """The member we show: the first live one with a description, else the first live one."""
        return next((m for m in self.alive if m["description_text"]), self.alive[0])


def _load_group(request: Request, store: Store, cid: int) -> Group:
    """404 for an unknown id, 307 (via exception) for a non-canonical member id."""
    canonical = store.canonical_of(cid)
    if canonical is None:
        raise HTTPException(404, "Job not found.")
    if canonical != cid:
        raise _Redirect(f"{request.url.path.replace(str(cid), str(canonical), 1)}")
    members = store.group_jobs(cid)
    closed = closed_members(members, _now(request), _max_age(request))
    alive = [m for m in members if closed[m["id"]] is None]
    return Group(cid, members, alive, closed)


class _Redirect(Exception):  # noqa: N818
    def __init__(self, to: str):
        self.to = to


NEXT = {"inbox": "/inbox", "applications": "/applications", "screened": "/screened"}


def _restore_or_409(store: Store, cid: int, now) -> None:
    try:
        store.restore_application(cid, now)
    except ValueError as e:  # a second tab / double click: already restored
        raise HTTPException(409, "The application is not in the Withdrawn status.") from e


def _after(next_: str, cid: int) -> str:
    return f"/jobs/{cid}" if next_ == "job" else NEXT.get(next_, "/inbox")


def kit_folder(cv, cid: int) -> str:
    """`cv_requests.kit_dir` (written by #7) when it is a plain number, else the group id."""
    value = cv["kit_dir"] if cv is not None and cv["kit_dir"] else None
    if value is not None and re.fullmatch(r"\d+", str(value)):
        return str(value)
    return str(int(cid))


def kit_file(request: Request, cid: int, name: str, cv=None) -> Path | None:
    """Only fixed names under kits_dir/<digits>; a path that leaves kits_dir is refused."""
    if name not in KIT_FILES:
        return None
    root = request.app.state.settings.kits_dir.resolve()
    path = (root / kit_folder(cv, cid) / name).resolve()
    if not path.is_relative_to(root) or not path.is_file():
        return None
    return path


def _reasons(cv) -> list[str]:
    try:
        value = json.loads(cv["review_reasons"]) if cv is not None and cv["review_reasons"] else []
    except ValueError:
        return []
    return [str(x) for x in value] if isinstance(value, list) else []


def _read_text(path: Path | None) -> str | None:
    if path is None:
        return None
    with path.open("rb") as fh:
        return fh.read(KIT_TEXT_MAX).decode("utf-8", errors="replace")


# --- routes ---


def register(app: FastAPI) -> None:
    @app.exception_handler(_Redirect)
    async def _redirect(request: Request, exc: _Redirect):
        return RedirectResponse(exc.to, status_code=307)

    @app.get("/healthz", include_in_schema=False)
    def healthz() -> Response:
        return PlainTextResponse("ok")

    @app.get("/", include_in_schema=False)
    def root() -> Response:
        return RedirectResponse("/inbox", status_code=307)

    # ---- Inbox ----

    @app.get("/inbox", response_class=HTMLResponse)
    def inbox_page(
        request: Request,
        profile: str = "",
        fit_min: str = "",
        rate_unknown: str = "",
        source: str = "",
        since_days: str = "",
        eligible: str = "default",
        decision: str = "default",
    ):
        types, unseen_only, sort = _view_params(request)
        profiles = list(request.app.state.rules_cfg.relevance.profiles)  # rules.yaml
        filters = InboxFilters(
            profile=profile if profile in profiles else None,
            fit_min=_to_int(fit_min, 0, 100),
            rate_unknown={"1": True, "0": False}.get(rate_unknown),
            source=source if source in connectors.all_source_names() else None,
            since_days=_to_int(since_days, 1, 365),
            eligible=eligible if eligible in ELIGIBLE_VALUES else "default",
            decision=decision if decision in DECISION_VALUES else "default",
            types=types,
            unseen_only=unseen_only,
            sort=sort,
        )
        with open_store(request) as store:
            res = _inbox(request, store, filters)
        ctx = {
            **_view_ctx(types, unseen_only, sort, res),
            "active": "inbox",
            "page": "inbox",
            "f": filters,
            "form": {
                "profile": profile,
                "fit_min": fit_min,
                "rate_unknown": rate_unknown,
                "source": source,
                "since_days": since_days,
                "eligible": filters.eligible,
                "decision": filters.decision,
            },
            "profiles": profiles,
            "sources": connectors.all_source_names(),
            "reasons": REASONS,
            "eligible_values": ELIGIBLE_VALUES,
            "eligible_labels": ELIGIBLE_LABELS,
            "decision_values": DECISION_VALUES,
        }
        if request.headers.get("hx-request") and request.headers.get("hx-target") == "rows":
            return render(request, "partials/inbox_rows.html", ctx)
        return render(request, "inbox.html", ctx)

    @app.post("/jobs/{cid}/decision", response_class=HTMLResponse)
    def decision(
        request: Request,
        cid: int,
        action: str = Form(""),
        reason: str = Form(""),
        note: str = Form(""),
        next_: str = Form("inbox", alias="next"),
    ):
        if action not in ("interested", "rejected", "prepare_cv", "undo", "override_in"):
            raise HTTPException(400, "Unknown action.")
        if action == "rejected" and reason not in REASONS:
            raise HTTPException(400, "A reason is required for No.")
        if len(note) > NOTE_MAX:
            raise HTTPException(400, f"The note is longer than {NOTE_MAX} characters.")
        if action == "override_in" and not note.strip():
            raise HTTPException(400, "Explain why the job fits.")
        now = _now(request)
        with open_store(request) as store:
            g = _load_group(request, store, cid)
            if not g.alive:
                raise HTTPException(410, "The job is closed by the source.")
            job_id = g.rep["id"]
            had_override = cid in store.owner_overrides()
            if action == "override_in":
                v = store.llm_verdicts(registry.score_version(), [cid])
                if not v or v[0].verdict.eligible or had_override:
                    raise HTTPException(409, "Haiku did not reject this job.")
            app_now = store.application(cid)
            on_withdrawn = app_now is not None and app_now["status"] == "withdrawn"
            if action == "undo" and store.is_withdrawn_on_top(cid):
                _restore_or_409(store, cid, now)  # the same "Restore" as on the card
            elif action == "prepare_cv":
                if on_withdrawn:
                    _restore_or_409(store, cid, now)  # a new CV request revives the card
                store.request_cv(job_id, channel="panel", now=now)
            else:
                store.record_decision(
                    job_id,
                    action,
                    channel="panel",
                    reason=reason if action == "rejected" else None,
                    note=(note or None) if action in ("rejected", "override_in") else None,
                    now=now,
                )
            if request.headers.get("hx-request") and action in ("interested", "undo"):
                item = None
                for mode in ("any", "rejected"):  # "Screened" rows are not in "any"
                    f = InboxFilters(eligible=mode, decision="all")
                    items = _inbox(request, store, f, only_cid=cid).items
                    item = next((i for i in items if i.canonical_id == cid), None)
                    if item is not None:
                        break
                if item is not None and not (had_override and item.override_note is None):
                    # (undoing the override returns the job to "Screened out by AI": the row leaves)
                    ctx = {"item": item, "reasons": REASONS, "type_labels": TYPE_LABELS}
                    return render(request, "partials/inbox_row.html", ctx)
        if request.headers.get("hx-request"):
            return HTMLResponse("")  # the row leaves the list
        return RedirectResponse(_after(next_, cid), status_code=303)

    # ---- Screened by AI ----

    @app.get("/screened", response_class=HTMLResponse)
    def screened_page(request: Request, source: str = "", since_days: str = "", fit_min: str = ""):
        types, unseen_only, sort = _view_params(request)
        filters = InboxFilters(
            source=source if source in connectors.all_source_names() else None,
            since_days=_to_int(since_days, 1, 365),
            fit_min=_to_int(fit_min, 0, 100),
            eligible="screened",
            decision="none",
            types=types,
            unseen_only=unseen_only,
            sort=sort,
        )
        with open_store(request) as store:
            res = _inbox(request, store, filters)
        ctx = {
            **_view_ctx(types, unseen_only, sort, res),
            "active": "screened",
            "page": "screened",
            "form": {"source": source, "since_days": since_days, "fit_min": fit_min},
            "sources": connectors.all_source_names(),
            "reasons": REASONS,
            "note_max": NOTE_MAX,
        }
        if request.headers.get("hx-request") and request.headers.get("hx-target") == "rows":
            return render(request, "partials/screened_rows.html", ctx)
        return render(request, "screened.html", ctx)

    @app.post("/seen/mark")
    def mark_seen(
        request: Request,
        page: str = Form("inbox"),
        qs: str = Form(""),
        cids: list[int] = Form(default=[]),  # noqa: B008
    ):
        """ "Mark all shown as seen": the ids of the rows on the page."""
        if page not in ("inbox", "screened"):
            raise HTTPException(400, "Unknown page.")
        with open_store(request) as store:
            store.mark_seen([c for c in cids[:1000] if 0 < c < 2**63], _now(request))
        back = f"/{page}" + (f"?{quote(qs[:2000], safe='=&%+,')}" if qs else "")
        return RedirectResponse(back, status_code=303)

    # ---- Job ----

    @app.get("/jobs/{cid}", response_class=HTMLResponse)
    def job_page(request: Request, cid: int):
        st = request.app.state
        with open_store(request) as store:
            g = _load_group(request, store, cid)
            store.mark_seen([cid], _now(request))  # opened = seen
            row = store.latest_decisions([cid]).get(cid)
            app_row = store.application(cid)
            cv = store.latest_cv_request(cid)
            cover_req = store.latest_cover_request(cid)
            events = store.application_events(cid)
            eff = next((r for r in store.applications_view() if r.canonical_id == cid), None)
            ctx = {
                "active": "inbox",
                "cid": cid,
                "closed": not g.alive,
                "title": (g.rep if g.alive else g.members[0])["title"],
                "company": (g.rep if g.alive else g.members[0])["company"],
                "decision": current_decision(row),
                "decision_note": row["note"] if row is not None else None,
                "override_note": store.owner_overrides().get(cid, {"note": None})["note"],
                "override_top": cid in store.owner_override_tops(),
                "note_max": NOTE_MAX,
                "app": app_row,
                "app_status": eff.status if eff else None,
                # a bare 'preparing' without events is the note-only placeholder: shows as "auto"
                "manual_status": (
                    app_row["status"]
                    if app_row and (events or app_row["status"] != "preparing")
                    else None
                ),
                "events": events,
                "statuses": APPLICATION_STATUSES,
                "withdraw_note": eff.withdraw_note if eff else None,
                "why": _why(request, store, [cid]).get(cid, Why()),
                "extra": store.extra_description(cid),
                "extra_max": EXTRA_MAX,
                "rescore_queued": store.queued_rescore_for(cid),
                "cv": cv,
                "cv_label": CV_LABELS.get(cv["status"], cv["status"]) if cv else None,
                "reasons": REASONS,
                "cv_labels": CV_LABELS,
                "kit": {n: (kit_file(request, cid, n, cv) is not None) for n in KIT_FILES},
                "resume_md": _read_text(kit_file(request, cid, "resume.md", cv)),
                "note_md": _read_text(kit_file(request, cid, "recruiter_note.md", cv)),
                "fit_md": _read_text(kit_file(request, cid, "fit_notes.md", cv)),
                "cover_md": _read_text(kit_file(request, cid, "cover_letter.md", cv)),
                "cover_req": cover_req,
                # only a letter newer than the latest CV: an older one's warnings are history
                "cover_warn": cover_req is not None
                and cv is not None
                and cover_req["id"] > cv["id"]
                and cover_req["status"] == "needs_review",
                "cover_reasons": _reasons(cover_req),
                "review_reasons": _reasons(cv),
                "can_edit": cv is not None and cv["status"] in ("done", "needs_review"),
                "hidden_sources": sum(1 for m in g.members if g.closed[m["id"]]),
            }
            if g.alive:
                rep = g.rep
                ids = [m["id"] for m in g.alive]
                rules = store.latest_verdicts(st.rule_version, ids)
                best = max(rules.values(), key=lambda v: v["relevance"] or 0, default=None)
                flags: list[str] = []
                reasons: list[str] = []
                for v in rules.values():
                    flags += [f for f in json.loads(v["flags_json"]) if f not in flags]
                    reasons += [r for r in json.loads(v["reasons_json"]) if r not in reasons]
                llm = store.llm_verdicts(registry.score_version(), [cid])
                ctx.update(
                    {
                        "rep": rep,
                        "tags": json.loads(rep["tags_json"] or "[]"),
                        "apply_url": rep["apply_url"] or rep["url"],
                        "rate_text": next(
                            (m["salary_text"] for m in g.alive if m["salary_text"]), None
                        ),
                        "sightings": [
                            (
                                Sighting(m["source"], m["url"], attribution_for(m["source"])),
                                m["first_seen_at"],
                                m["last_seen_at"],
                            )
                            for m in g.alive
                        ],
                        "rule_relevance": best["relevance"] if best else None,
                        "rule_profile": best["profile"] if best else None,
                        "rule_flags": flags,
                        "rule_reasons": reasons,
                        "llm": llm[0].verdict if llm else None,
                    }
                )
            return render(request, "job.html", ctx, status=410 if not g.alive else 200)

    @app.post("/jobs/{cid}/extra")
    def extra_save(request: Request, cid: int, extra: str = Form("")):
        """Save the owner's pasted job-ad text (untrusted data). A CV request already running is
        not touched: the text reaches the next one ("Rebuild")."""
        text = normalize_text(extra)
        if len(text) > EXTRA_MAX:
            raise HTTPException(400, f"The text is longer than {EXTRA_MAX} characters.")
        with open_store(request) as store:
            g = _load_group(request, store, cid)
            if not g.alive:
                raise HTTPException(410, "The job is closed by the source.")
            store.set_extra_description(cid, text, _now(request))
        return RedirectResponse(f"/jobs/{cid}#extra", status_code=303)

    @app.post("/jobs/{cid}/extra/rescore")
    def extra_rescore(request: Request, cid: int):
        with open_store(request) as store:
            g = _load_group(request, store, cid)
            if not g.alive:
                raise HTTPException(410, "The job is closed by the source.")
            if store.request_rescore(cid, _now(request)) is None:
                raise HTTPException(409, "No saved text, or a re-score is already queued.")
        return RedirectResponse(f"/jobs/{cid}#extra", status_code=303)

    @app.post("/jobs/{cid}/extra/rebuild")
    def extra_rebuild(request: Request, cid: int):
        """One Opus CV job, same queue and cap as "Prepare CV"; only for a group with a kit."""
        with open_store(request) as store:
            g = _load_group(request, store, cid)
            if not g.alive:
                raise HTTPException(410, "The job is closed by the source.")
            cv = store.latest_cv_request(cid)
            if cv is None or cv["status"] not in ("done", "needs_review"):
                raise HTTPException(
                    409, "Nothing to rebuild: there is no kit or it is in progress."
                )
            _, _, created = store.request_cv(g.rep["id"], channel="panel", now=_now(request))
            if not created:
                raise HTTPException(409, "A CV request is already queued.")
        return RedirectResponse(f"/jobs/{cid}#kit", status_code=303)

    @app.get("/jobs/{cid}/kit/{name}")
    def kit(request: Request, cid: int, name: str):
        with open_store(request) as store:
            cv = store.latest_cv_request(cid)
        path = kit_file(request, cid, name, cv)
        if path is None:
            raise HTTPException(404, "No such file.")
        # Kit files are rebuilt in place: never let the browser reuse an old copy.
        headers = {
            "content-disposition": f'attachment; filename="{name}"',
            "cache-control": "no-store",
        }
        if name.endswith(".pdf"):
            return FileResponse(path, media_type=KIT_FILES[name], headers=headers)
        return PlainTextResponse(_read_text(path) or "", headers=headers)

    @app.post("/jobs/{cid}/kit/resume")
    def kit_resume(
        request: Request, cid: int, resume_md: str = Form(""), request_id: str = Form("")
    ):
        text = resume_md.replace("\r\n", "\n").replace("\r", "\n")
        if not text.strip():
            raise HTTPException(400, "The resume cannot be empty.")
        if len(text) > RESUME_MAX:
            raise HTTPException(400, f"The resume is longer than {RESUME_MAX} characters.")
        st = request.app.state
        settings = st.settings
        with open_store(request) as store:
            _load_group(request, store, cid)
            try:
                lock = kitlib.kit_lock(settings.kits_dir, cid)
            except kitlib.KitBusy:
                raise HTTPException(409, BUSY) from None
            except (kitlib.KitError, OSError, ValueError):
                raise HTTPException(503, "The kit is not writable right now.") from None
            try:
                cv = store.latest_cv_request(cid)  # re-read under the lock
                if cv is None or cv["status"] not in ("done", "needs_review"):
                    raise HTTPException(409, "The kit cannot be edited right now.")
                if request_id != str(cv["id"]):
                    raise HTTPException(409, "The kit was updated, reload the page.")
                try:
                    kitlib.write_one(settings.kits_dir, cid, kitlib.RESUME_MD, text)
                except (kitlib.KitError, OSError):
                    raise HTTPException(500, "Could not save the kit.") from None
                pdf_status = "ok"
                try:
                    pdf = st.renderer.render(text).pdf
                    kitlib.write_one(settings.kits_dir, cid, kitlib.RESUME_PDF, pdf)
                except (RendererUnavailable, RenderError, kitlib.KitError, OSError) as exc:
                    pdf_status = "unavailable" if isinstance(exc, RendererUnavailable) else "failed"
                if pdf_status != "ok":
                    try:  # the old PDF no longer matches the text: not downloadable any more
                        kitlib.retire_pdf(settings.kits_dir, cid)
                    except (kitlib.KitError, OSError):
                        pdf_status = "failed"
                store.mark_kit_edited(cid, pdf_status=pdf_status, now=_now(request))
            finally:
                lock.release()
        return RedirectResponse(f"/jobs/{cid}#kit", status_code=303)

    def _cover_write(request: Request, store: Store, cid: int, request_id: str, text: str | None):
        """Save the letter (when `text` is given) and rebuild its PDF from what is on disk."""
        st = request.app.state
        settings = st.settings
        _load_group(request, store, cid)
        try:
            lock = kitlib.kit_lock(settings.kits_dir, cid)
        except kitlib.KitBusy:
            raise HTTPException(409, BUSY) from None
        except (kitlib.KitError, OSError, ValueError):
            raise HTTPException(503, "The kit is not writable right now.") from None
        try:
            cv = store.latest_cv_request(cid)  # re-read under the lock
            if cv is None or cv["status"] not in ("done", "needs_review"):
                raise HTTPException(409, "The kit cannot be edited right now.")
            if request_id != str(cv["id"]):
                raise HTTPException(409, "The kit was updated, reload the page.")
            if kit_file(request, cid, "cover_letter.md", cv) is None:
                raise HTTPException(409, "The cover letter is not made yet.")
            try:
                if text is not None:
                    kitlib.write_one(settings.kits_dir, cid, kitlib.COVER_MD, text)
                else:
                    path = kit_file(request, cid, "cover_letter.md", cv)
                    text = _read_text(path) or ""
                ok = False
                try:
                    pdf = st.renderer.render_letter(text).pdf
                    kitlib.write_one(settings.kits_dir, cid, kitlib.COVER_PDF, pdf)
                    ok = True
                except (RendererUnavailable, RenderError, kitlib.KitError, OSError):
                    pass
                if not ok:  # the old PDF no longer matches the text: not downloadable any more
                    kitlib.retire_pdf(settings.kits_dir, cid, kitlib.COVER_PDF)
            except (kitlib.KitError, OSError):
                raise HTTPException(500, "Could not save the cover letter.") from None
        finally:
            lock.release()

    @app.post("/jobs/{cid}/kit/cover")
    def kit_cover_save(
        request: Request, cid: int, cover_md: str = Form(""), request_id: str = Form("")
    ):
        text = cover_md.replace("\r\n", "\n").replace("\r", "\n")
        if not text.strip():
            raise HTTPException(400, "The letter cannot be empty.")
        if len(text) > COVER_EDIT_MAX:
            raise HTTPException(400, f"The letter is longer than {COVER_EDIT_MAX} characters.")
        with open_store(request) as store:
            _cover_write(request, store, cid, request_id, text)
        return RedirectResponse(f"/jobs/{cid}#cover", status_code=303)

    @app.post("/jobs/{cid}/kit/cover/pdf")
    def kit_cover_pdf(request: Request, cid: int, request_id: str = Form("")):
        with open_store(request) as store:
            _cover_write(request, store, cid, request_id, None)
        return RedirectResponse(f"/jobs/{cid}#cover", status_code=303)

    @app.post("/jobs/{cid}/kit/cover/make")
    def kit_cover_make(request: Request, cid: int):
        """Queue one Opus call for a kit that has no cover letter (same queue, same Opus cap)."""
        with open_store(request) as store:
            _load_group(request, store, cid)
            cv = store.latest_cv_request(cid)
            if kit_file(request, cid, "cover_letter.md", cv) is not None:
                raise HTTPException(409, "The cover letter already exists.")
            if store.request_cover(cid, now=_now(request)) is None:
                raise HTTPException(
                    409, "Not possible now: the kit is not ready or already queued."
                )
        return RedirectResponse(f"/jobs/{cid}#cover", status_code=303)

    @app.post("/jobs/{cid}/kit/cover/approve")
    def kit_cover_approve(request: Request, cid: int):
        with open_store(request) as store:
            _load_group(request, store, cid)
            if not store.approve_cover(cid, now=_now(request)):
                raise HTTPException(409, "The letter is not waiting for review.")
        return RedirectResponse(f"/jobs/{cid}#cover", status_code=303)

    @app.post("/jobs/{cid}/kit/approve")
    def kit_approve(request: Request, cid: int):
        with open_store(request) as store:
            _load_group(request, store, cid)
            if not store.approve_kit(cid, now=_now(request)):
                raise HTTPException(409, "The kit is not waiting for review.")
        return RedirectResponse(f"/jobs/{cid}#kit", status_code=303)

    @app.post("/jobs/{cid}/status")
    @app.post("/applications/{cid}/status")
    def set_status(
        request: Request,
        cid: int,
        status: str = Form(""),
        note: str = Form(""),
        next_: str = Form("job", alias="next"),
    ):
        if status and status not in APPLICATION_STATUSES:
            raise HTTPException(400, "Unknown status.")  # Withdrawn only via /withdraw
        if len(note) > 2000:
            raise HTTPException(400, "The note is longer than 2000 characters.")
        with open_store(request) as store:
            _load_group(request, store, cid)
            cur = store.application(cid)
            if cur is not None and cur["status"] == "withdrawn":
                raise HTTPException(409, "Press Restore first.")
            if status:
                store.set_application_status(cid, status, note, _now(request))
            else:  # "— auto —": only the note is saved, the status stays derived
                store.set_application_note(cid, note, _now(request))
        return RedirectResponse(_after(next_, cid), status_code=303)

    # ---- Applications ----

    @app.post("/applications/{cid}/withdraw")
    def withdraw(
        request: Request,
        cid: int,
        reason: str = Form(""),
        note: str = Form(""),
        next_: str = Form("applications", alias="next"),
    ):
        if reason not in REASONS:
            raise HTTPException(400, "A reason is required for Withdrawn.")
        note = note.strip()
        if len(note) > NOTE_MAX:
            raise HTTPException(400, f"The text is longer than {NOTE_MAX} characters.")
        if reason == "other" and not note:
            raise HTTPException(400, "Text is required for the reason Other.")
        with open_store(request) as store:
            _load_group(request, store, cid)
            try:
                store.withdraw_application(cid, reason, note or None, _now(request))
            except ValueError as e:
                raise HTTPException(409, "No application, or it is already Withdrawn.") from e
        return RedirectResponse(_after(next_, cid), status_code=303)

    @app.post("/applications/{cid}/restore")
    def restore(request: Request, cid: int, next_: str = Form("applications", alias="next")):
        with open_store(request) as store:
            _load_group(request, store, cid)
            _restore_or_409(store, cid, _now(request))
        return RedirectResponse(_after(next_, cid), status_code=303)

    @app.get("/applications", response_class=HTMLResponse)
    def applications_page(request: Request):
        with open_store(request) as store:
            rows = store.applications_view()
            whys = _why(request, store, [r.canonical_id for r in rows])
        order = list(BOARD_ORDER)
        order += sorted({r.status for r in rows} - set(order))  # unknown statuses are shown
        columns = [(s, [r for r in rows if r.status == s]) for s in order]
        return render(
            request,
            "applications.html",
            {
                "active": "applications",
                "rows": rows,
                "columns": columns,
                "funnel": BOARD_ALWAYS,
                "whys": whys,
                "statuses": APPLICATION_STATUSES,
                "cv_labels": CV_LABELS,
                "reasons": REASONS,
            },
        )

    # ---- Sources ----

    @app.get("/sources", response_class=HTMLResponse)
    def sources_page(request: Request):
        with open_store(request) as store:
            rows = source_rows(store, _now(request), _unconfigured(request))
        choices = [3600 * h for h in (1, 2, 3, 4, 6, 12, 24, 48, 72, 168)]
        return render(
            request,
            "sources.html",
            {"active": "sources", "rows": rows, "choices": choices},
        )

    @app.post("/sources/{name}/enabled")
    def source_enabled(request: Request, name: str, enabled: str = Form("")):
        if enabled not in ("0", "1"):
            raise HTTPException(400, "enabled must be 0 or 1.")
        with open_store(request) as store:
            try:
                store.set_source_enabled(name, enabled == "1")
            except KeyError:
                raise HTTPException(404, "Source not found.") from None
        return RedirectResponse("/sources", status_code=303)

    @app.post("/sources/{name}/interval")
    def source_interval(request: Request, name: str, seconds: str = Form("")):
        cls = connectors.REGISTRY.get(name)
        value: int | None = None
        if seconds.strip():
            if not seconds.strip().isdigit():
                raise HTTPException(400, "The interval must be a whole number of seconds.")
            value = int(seconds.strip())
            floor = int(cls.interval.total_seconds()) if cls else 3600
            if value < floor:
                raise HTTPException(
                    400, f"The interval cannot be below {floor} s (source ToS limit)."
                )
            if value > MAX_INTERVAL_S:
                raise HTTPException(400, "The interval cannot exceed 7 days.")
        with open_store(request) as store:
            try:
                store.set_interval_override(name, value)
            except KeyError:
                raise HTTPException(404, "Source not found.") from None
        return RedirectResponse("/sources", status_code=303)

    # ---- Usage ----

    @app.get("/usage", response_class=HTMLResponse)
    def usage_page(request: Request, days: str = "7"):
        st = request.app.state
        n = _to_int(days, 1, 90) or 7
        now = _now(request)
        cfg = st.llm_cfg
        tz = cfg.timezone if cfg else "UTC"
        today = local_day(now, tz)
        since = local_day(now - timedelta(days=n - 1), tz)
        with open_store(request) as store:
            rows = store.usage_summary(since)
            pause = store.active_pause(now)
            method, expires = store.last_auth_info()
            alerts = store.latest_alerts(10)
            decisions = store.recent_decisions(10)
            cost_today = store.cost_today(today)
        return render(
            request,
            "usage.html",
            {
                "active": "usage",
                "rows": rows,
                "days": n,
                "today": today,
                "caps": cfg.daily_call_caps if cfg else {},
                "cost_cap": cfg.daily_cost_cap_usd if cfg else None,
                "cost_today": cost_today,
                "cfg_ok": cfg is not None,
                "pause": pause,
                "auth_method": method,
                "auth_expires": expires,
                "prompt_version": registry.score_version(),
                "alerts": alerts,
                "decisions": decisions,
            },
        )

    # ---- Stats ----

    @app.get("/stats", response_class=HTMLResponse)
    def stats_page(request: Request, period: str = "7d"):
        st = request.app.state
        if period not in statslib.PERIODS:
            period = "7d"
        now = _now(request)
        cfg, wcfg = st.llm_cfg, st.worker_cfg
        tz = cfg.timezone if cfg else "UTC"
        today = local_day(now, tz)
        with open_store(request) as store:
            data = statslib.compute(
                store,
                period=period,
                now=now,
                rule_version=st.rule_version,
                relevance_threshold=st.rules_cfg.relevance.threshold,
                prompt_version=registry.score_version(),
                fit_threshold=wcfg.alerts.fit_threshold,
                batch_size=cfg.scoring.batch_size if cfg else 8,
                per_cycle=wcfg.score_max_calls_per_cycle,
                tick_minutes=wcfg.tick_minutes,
                max_age_days=cfg.scoring.max_age_days if cfg else 30,
            )
            caps = cfg.daily_call_caps if cfg else {}
            calls = [(a, store.calls_today(a, today), cap) for a, cap in caps.items()]
            cost_today = store.cost_today(today)
            pause = store.active_pause(now)
        return render(
            request,
            "stats.html",
            {
                "active": "stats",
                "s": data,
                "period": period,
                "periods": [(k, v[0]) for k, v in statslib.PERIODS.items()],
                "fit_threshold": wcfg.alerts.fit_threshold,
                "reason_labels": reason_labels(st.rules_cfg),
                "no_labels": REASONS,
                "app_order": BOARD_ORDER,
                "calls": calls,
                "cost_today": cost_today,
                "cost_cap": cfg.daily_cost_cap_usd if cfg else None,
                "pause": pause,
            },
        )
