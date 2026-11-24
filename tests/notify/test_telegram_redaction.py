"""The bot token must not reach exceptions, tracebacks, logs, repr or the database."""

from __future__ import annotations

import logging
import traceback

import httpx
import pytest

from cvboost import cli
from cvboost.config import Settings
from cvboost.http import HttpClient
from cvboost.notify.redact import RedactingFilter, redact
from cvboost.notify.telegram import TelegramClient, TelegramError
from cvboost.worker_config import WorkerConfig
from tests.conftest import NOW, PUBLIC_RESOLVER
from tests.notify.helpers import OWNER, TOKEN, FakeTelegram, err, ok

SECRET = TOKEN.split(":")[1]


def client(handler):
    return TelegramClient(
        TOKEN,
        HttpClient(httpx.MockTransport(handler), sleep=lambda s: None, resolver=PUBLIC_RESOLVER),
    )


def assert_clean(exc: BaseException):
    texts = [str(exc), repr(exc), "".join(traceback.format_exception(exc))]
    c = exc.__cause__ or exc.__context__
    assert c is None, "exception chain would carry the request URL"
    for t in texts:
        assert TOKEN not in t and SECRET not in t, t


def transport_error(req):
    raise httpx.ConnectError(f"cannot connect to {req.url}")


SCENARIOS = {
    "connect_error": transport_error,
    "timeout": lambda req: (_ for _ in ()).throw(httpx.ReadTimeout(f"timed out {req.url}")),
    "401": lambda req: err(401, "Unauthorized"),
    "ok_false": lambda req: err(400, f"Bad Request: see https://api.telegram.org/bot{TOKEN}/x"),
    "redirect": lambda req: httpx.Response(
        302, headers={"Location": f"https://evil.example.com/{TOKEN}"}
    ),
    "garbage": lambda req: httpx.Response(200, content=b"<html>"),
    "500": lambda req: httpx.Response(500, text=f"boom {TOKEN}"),
}


@pytest.mark.parametrize("name", SCENARIOS)
def test_errors_never_carry_the_token(name):
    tg = client(SCENARIOS[name])
    with pytest.raises(TelegramError) as ei:
        tg.send_message(OWNER, "hi")
    assert_clean(ei.value)


def test_policy_violations_do_not_leak_either():
    # wrong host is impossible through the API, but UnsafeURLError texts contain the URL
    http = HttpClient(httpx.MockTransport(lambda r: ok(True)), resolver=lambda h: ["10.0.0.1"])
    with pytest.raises(TelegramError) as ei:
        TelegramClient(TOKEN, http).get_me()
    assert ei.value.kind == "UnsafeURLError"
    assert_clean(ei.value)


def test_error_carries_useful_facts():
    with pytest.raises(TelegramError) as ei:
        client(lambda r: err(429, "Too Many Requests: retry after 9", retry_after=9)).send_message(
            1, "x"
        )
    e = ei.value
    assert (e.method, e.status, e.retry_after) == (
        "sendMessage",
        429,
        9,
    ) and "retry after 9" in str(e)


def test_client_repr_hides_token_and_settings_too():
    http = HttpClient(resolver=PUBLIC_RESOLVER)
    assert TOKEN not in repr(TelegramClient(TOKEN, http))
    s = Settings(
        db_path="x", log_level="INFO", rules_path="r", llm_config_path="l", telegram_token=TOKEN
    )
    assert TOKEN not in repr(s) and TOKEN not in str(s)


def test_requests_use_only_the_telegram_host_https_and_no_redirects():
    fake = FakeTelegram()
    tg = fake.client()
    tg.get_me()
    assert fake.urls == [f"https://api.telegram.org/bot{TOKEN}/getMe"]


def test_logging_filter_scrubs_message_args_and_traceback(caplog):
    log = logging.getLogger("cvboost.test_redaction")
    handler = logging.Handler()
    seen: list[str] = []
    handler.emit = lambda rec: seen.append(logging.Formatter().format(rec))  # type: ignore[method-assign]
    handler.addFilter(RedactingFilter(TOKEN))
    log.addHandler(handler)
    log.setLevel(logging.DEBUG)
    try:
        try:
            raise RuntimeError(f"POST https://api.telegram.org/bot{TOKEN}/getUpdates failed")
        except RuntimeError:
            log.exception("polling %s", TOKEN)
        log.warning("other token 987654321:%s", "Z" * 35)
    finally:
        log.removeHandler(handler)
    blob = "\n".join(seen)
    assert TOKEN not in blob and SECRET not in blob and "Z" * 35 not in blob
    assert "<redacted>" in blob and "RuntimeError" in blob  # still useful


def test_redact_patterns():
    assert redact(f"x {TOKEN} y", TOKEN) == "x <redacted> y"
    assert redact("https://api.telegram.org/botANYTHING/sendMessage") == (
        "https://api.telegram.org/bot<redacted>/sendMessage"
    )
    assert redact("12:short") == "12:short"


def test_cli_logging_setup_silences_httpx_and_adds_filter(monkeypatch):
    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)
    logging.getLogger("httpx").setLevel(logging.NOTSET)
    cli._setup_logging("DEBUG", TOKEN)
    try:
        assert logging.getLogger("httpx").level == logging.WARNING
        assert logging.getLogger("httpcore").level == logging.WARNING
        assert all(any(isinstance(f, RedactingFilter) for f in h.filters) for h in root.handlers)
    finally:
        for h in list(root.handlers):
            root.removeHandler(h)


def test_httpx_request_logging_is_off_after_setup_so_the_url_is_never_logged(caplog):
    cli._setup_logging("DEBUG", TOKEN)
    caplog.set_level(logging.DEBUG)
    client(lambda r: ok({"id": 1})).get_me()
    assert TOKEN not in caplog.text and SECRET not in caplog.text


def test_token_not_in_any_table_after_failures_and_sends(s, sent, tg):
    # failed sends store a redacted error; sent alerts store only our own text
    from cvboost.notify.alerts import send_pending as sp

    s.conn.execute("UPDATE alerts SET status='pending' WHERE 1")
    tg.queue("sendMessage", err(400, f"Bad Request: bot{TOKEN} chat not found"))
    sp(s, tg.client(), WorkerConfig(), OWNER, NOW, token=TOKEN)
    for (table,) in s.conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall():
        for row in s.conn.execute(f"SELECT * FROM {table}").fetchall():
            for v in tuple(row):
                assert not (isinstance(v, str) and (TOKEN in v or SECRET in v)), (table, v)
