"""Everything that keeps untrusted text and cross-site requests away from the owner.

* `sanitize_html` is the ONLY place a `Markup` (trusted HTML) is created. Templates never use
  `|safe`; a test greps for it.
* `safe_url` gates every `href` that comes from external data (autoescape does not stop
  `javascript:`).
* CSRF: signed double-submit cookie + Origin / Sec-Fetch-Site checks, as an app-level dependency
  so a new POST route cannot forget it.
* Security headers, optional basic auth: plain ASGI middleware.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets
from urllib.parse import urlsplit

import nh3
from fastapi import HTTPException, Request
from markupsafe import Markup

CSP = (
    "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self'; "
    "connect-src 'self'; font-src 'self'; form-action 'self'; frame-ancestors 'none'; "
    "base-uri 'none'"
)
HEADERS = {
    "content-security-policy": CSP,
    "x-content-type-options": "nosniff",
    "referrer-policy": "same-origin",
    "x-frame-options": "DENY",
    "cross-origin-opener-policy": "same-origin",
    "permissions-policy": "camera=(), microphone=(), geolocation=()",
    "x-robots-tag": "noindex, nofollow",
}

ALLOWED_TAGS = {
    "p", "br", "ul", "ol", "li", "strong", "b", "em", "i", "h2", "h3", "h4", "a", "code", "pre",
    "blockquote",
}  # fmt: skip


def sanitize_html(html: str | None) -> Markup:
    """Strict allowlist: no img/style/class/id, no hx-* or on* attributes, http(s) links only,
    no relative links (they would point into the panel), every link nofollow."""
    cleaned = nh3.clean(
        html or "",
        tags=ALLOWED_TAGS,
        attributes={"a": {"href"}},
        url_schemes={"http", "https"},
        url_relative="deny",
        link_rel="noopener noreferrer nofollow",
        strip_comments=True,
    )
    return Markup(cleaned)  # noqa: S704 - the single, deliberate Markup source


def safe_url(url: object) -> str | None:
    if not isinstance(url, str) or not url or any(ord(c) <= 32 or ord(c) == 127 for c in url):
        return None
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    if parts.scheme.lower() not in ("http", "https") or not parts.netloc:
        return None
    return url


# --- CSRF ---

COOKIE = "cvb_csrf"
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


def new_token(secret: bytes) -> str:
    nonce = secrets.token_urlsafe(24)
    return f"{nonce}.{_sign(secret, nonce)}"


def _sign(secret: bytes, nonce: str) -> str:
    return hmac.new(secret, nonce.encode(), hashlib.sha256).hexdigest()


def valid_token(secret: bytes, token: str | None) -> bool:
    if not token or token.count(".") != 1:
        return False
    nonce, sig = token.split(".")
    return hmac.compare_digest(sig, _sign(secret, nonce))


def csrf_token_for(request: Request) -> str:
    """The token to render: the cookie's if it is valid, else a fresh one (the response then sets
    the cookie, see `routes.render`)."""
    secret = request.app.state.csrf_secret
    cookie = request.cookies.get(COOKIE)
    if valid_token(secret, cookie):
        return cookie  # type: ignore[return-value]
    token = getattr(request.state, "csrf_new", None)
    if token is None:
        token = request.state.csrf_new = new_token(secret)
    return token


async def csrf_guard(request: Request) -> None:
    """App-level dependency: every unsafe method must prove it came from our own pages."""
    if request.method in SAFE_METHODS:
        return
    secret = request.app.state.csrf_secret
    cookie = request.cookies.get(COOKIE)
    sent = request.headers.get("x-csrf-token")
    if not sent:
        form = await request.form()
        value = form.get("csrf_token")
        sent = value if isinstance(value, str) else None
    ok = (
        valid_token(secret, cookie)
        and sent is not None
        and hmac.compare_digest(sent.encode(), (cookie or "").encode())
    )
    origin = request.headers.get("origin")
    if ok and origin is not None:
        if origin == "null":
            # Chrome sends `Origin: null` on a native form POST under some referrer policies; trust
            # it only when the browser itself says same-origin (the signed token was checked above)
            ok = request.headers.get("sec-fetch-site") == "same-origin"
        else:
            ok = urlsplit(origin).netloc == request.headers.get("host", "")
    if ok and request.headers.get("sec-fetch-site", "same-origin") == "cross-site":
        ok = False
    if not ok:
        raise HTTPException(status_code=403, detail="Invalid CSRF token. Reload the page.")


# --- ASGI middleware ---

MAX_BODY = 64 * 1024  # forms here are tiny
# the kit editor posts up to 20000 chars; cyrillic is 6 bytes per char once form-encoded
KIT_EDIT_BODY = 256 * 1024
KIT_EDIT_PATH = re.compile(r"/jobs/\d+/(?:kit/resume|extra)")


class BodyLimitMiddleware:
    """413 for a body over MAX_BODY, by Content-Length and by bytes actually received."""

    def __init__(self, app, limit: int = MAX_BODY):
        self.app = app
        self.limit = limit

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        limit = self.limit
        if scope["method"] == "POST" and KIT_EDIT_PATH.fullmatch(scope["path"]):
            limit = max(limit, KIT_EDIT_BODY)
        declared = dict(scope["headers"]).get(b"content-length")
        if declared is not None and (not declared.isdigit() or int(declared) > limit):
            await self._reject(send)
            return
        seen = 0
        started = False  # a response (ours or the app's) has begun
        rejected = False

        async def limited_receive():
            nonlocal seen, started, rejected
            msg = await receive()
            if msg["type"] == "http.request":
                seen += len(msg.get("body", b""))
                if seen > limit and not rejected:
                    # FastAPI turns any body-parse exception into a 400, so answer 413 right here
                    # and let the app's follow-up response be dropped
                    rejected = True
                    if not started:
                        started = True
                        await self._reject(send)
                    raise _TooLarge
            return msg

        async def tracking_send(message):
            nonlocal started
            if rejected:
                return
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        try:
            await self.app(scope, limited_receive, tracking_send)
        except _TooLarge:
            pass

    @staticmethod
    async def _reject(send):
        body = b"Request body is too large."
        await send(
            {
                "type": "http.response.start",
                "status": 413,
                "headers": [
                    (b"content-type", b"text/plain; charset=utf-8"),
                    (b"content-length", str(len(body)).encode()),
                    (b"connection", b"close"),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})


class _TooLarge(Exception):  # noqa: N818
    pass


class SecurityHeadersMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def wrapped(message):
            if message["type"] == "http.response.start":
                headers = [(k, v) for k, v in message["headers"]]
                have = {k.lower() for k, _ in headers}
                for k, v in HEADERS.items():
                    if k.encode() not in have:
                        headers.append((k.encode(), v.encode()))
                ctype = next((v for k, v in headers if k.lower() == b"content-type"), b"")
                if ctype.startswith(b"text/html") and b"cache-control" not in have:
                    headers.append((b"cache-control", b"no-store"))
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, wrapped)


class BasicAuthMiddleware:
    """Optional. `/healthz` stays open so the container healthcheck needs no credentials."""

    def __init__(self, app, user: str, password: str):
        self.app = app
        self.user = user.encode()
        self.password = password.encode()

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["path"] == "/healthz":
            await self.app(scope, receive, send)
            return
        headers = dict(scope["headers"])
        if self._ok(headers.get(b"authorization", b"").decode("latin-1")):
            await self.app(scope, receive, send)
            return
        body = b"Authentication required"
        await send(
            {
                "type": "http.response.start",
                "status": 401,
                "headers": [
                    (b"www-authenticate", b'Basic realm="cvboost"'),
                    (b"content-type", b"text/plain; charset=utf-8"),
                    (b"content-length", str(len(body)).encode()),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})

    def _ok(self, header: str) -> bool:
        scheme, _, value = header.partition(" ")
        if scheme.lower() != "basic":
            return False
        try:
            user, sep, password = base64.b64decode(value, validate=True).partition(b":")
        except ValueError:
            return False
        good_user = hmac.compare_digest(user, self.user)
        good_pass = hmac.compare_digest(password, self.password)
        return bool(sep) and good_user and good_pass
