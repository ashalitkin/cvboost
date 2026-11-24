from __future__ import annotations

import imaplib
import json
import logging
from datetime import date

import pytest

from cvboost.collect import collect_source
from cvboost.config import Settings
from cvboost.connectors.email_alerts import EmailAlertsConnector
from cvboost.llm.claude_cli import minimal_env
from cvboost.mail.imap import ImapCreds, ImapMailbox, MailError, imap_date
from cvboost.notify.redact import RedactingFilter
from tests.conftest import NOW
from tests.mail.helpers import CREDS, PASSWORD, NoHttp, eml


class FakeIMAP4:
    instances: list[FakeIMAP4] = []
    login_error: Exception | None = None

    def __init__(self, host, port=993, ssl_context=None, timeout=None):
        self.host, self.port, self.ssl_context, self.timeout = host, port, ssl_context, timeout
        self.debug = 0
        self.calls: list[tuple] = []
        self.messages = {5: eml("linkedin_alert_5.eml"), 7: eml("dice_alert_5.eml")}
        FakeIMAP4.instances.append(self)

    def login(self, user, password):
        self.calls.append(("login", user))
        if self.login_error:
            raise self.login_error
        return "OK", [b"logged in"]

    def select(self, mailbox, readonly=False):
        self.calls.append(("select", mailbox, readonly))
        return "OK", [b"2"]

    def response(self, name):
        return name, [b"42"]

    def uid(self, command, *args):
        self.calls.append(("uid", command, *args))
        if command == "SEARCH":
            return "OK", [b" ".join(str(u).encode() for u in sorted(self.messages))]
        if command == "FETCH" and args[1] == "(RFC822.SIZE)":
            return "OK", [
                f"{i} (UID {u} RFC822.SIZE {len(self.messages[u])})".encode()
                for i, u in enumerate(self.messages, 1)
            ]
        if command == "FETCH":
            return "OK", [(b"1 (UID 5 BODY[] {99}", self.messages[int(args[0])]), b")"]
        raise AssertionError(command)

    def logout(self):
        self.calls.append(("logout",))
        return "BYE", []

    def __getattr__(self, name):  # store / copy / expunge / append / close ...
        if name in {"store", "copy", "expunge", "append", "close", "delete", "create", "rename"}:
            raise AssertionError(f"mailbox-modifying call: {name}")
        raise AttributeError(name)


@pytest.fixture
def fake_imap(monkeypatch):
    FakeIMAP4.instances = []
    FakeIMAP4.login_error = None
    monkeypatch.setattr(imaplib, "IMAP4_SSL", FakeIMAP4)
    return FakeIMAP4


def test_wrapper_is_read_only_and_uses_peek(fake_imap):
    mb = ImapMailbox(CREDS)
    assert mb.open() == 42
    assert mb.search_uids(1, date(2026, 11, 7)) == [5, 7]
    assert mb.sizes([5, 7]) == {
        5: len(eml("linkedin_alert_5.eml")),
        7: len(eml("dice_alert_5.eml")),
    }
    assert mb.fetch(5) == eml("linkedin_alert_5.eml")
    mb.close()
    (c,) = fake_imap.instances
    assert ("select", "INBOX", True) in c.calls  # EXAMINE
    fetches = [x for x in c.calls if x[:2] == ("uid", "FETCH")]
    assert fetches and all(f[3] in ("(RFC822.SIZE)", "(BODY.PEEK[])") for f in fetches)
    assert not any("RFC822]" in str(x) for x in c.calls) and ("logout",) in c.calls
    assert c.debug == 0 and c.ssl_context is not None and c.timeout == 30 and c.port == 993
    assert ("uid", "SEARCH", "UID", "1:*", "SINCE", "07-Nov-2026") in c.calls


def test_imap_date_is_locale_independent():
    assert imap_date(date(2026, 1, 5)) == "05-Jan-2026"


def test_login_error_is_redacted_and_has_no_chain(fake_imap):
    fake_imap.login_error = imaplib.IMAP4.error(f"AUTHENTICATIONFAILED bad password {PASSWORD}")
    with pytest.raises(MailError) as ei:
        ImapMailbox(CREDS).open()
    assert PASSWORD not in str(ei.value) and "<redacted>" in str(ei.value)
    assert ei.value.__suppress_context__  # raised `from None`: no traceback chain
    assert ei.value.__cause__ is None


def test_password_never_reaches_logs_db_repr_or_child_env(fake_imap, store, caplog, monkeypatch):
    fake_imap.login_error = imaplib.IMAP4.error(f"login failed for {PASSWORD}")
    conn = EmailAlertsConnector(CREDS)  # real ImapMailbox over the fake imaplib
    handler = logging.StreamHandler()
    handler.addFilter(RedactingFilter(None, PASSWORD))
    logging.getLogger().addHandler(handler)
    try:
        with caplog.at_level(logging.DEBUG):
            res = collect_source(store, NoHttp().client, conn, limit=None, now=NOW)
    finally:
        logging.getLogger().removeHandler(handler)
    assert res.status == "error"
    assert PASSWORD not in (res.error_message or "")
    dump = json.dumps(
        {
            t: [tuple(r) for r in store.conn.execute(f"SELECT * FROM {t}")]
            for (t,) in store.conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        },
        default=str,
    )
    assert PASSWORD not in dump
    assert PASSWORD not in repr(conn) and PASSWORD not in repr(CREDS)
    assert PASSWORD not in repr(
        Settings(
            db_path="x",
            log_level="INFO",
            rules_path="x",
            llm_config_path="x",
            imap_user="u",
            imap_password=PASSWORD,
        )
    )  # type: ignore[arg-type]
    env = minimal_env(
        {"CVBOOST_IMAP_PASSWORD": PASSWORD, "CVBOOST_IMAP_USER": "u", "PATH": "/bin"}, "claude"
    )
    assert "CVBOOST_IMAP_PASSWORD" not in env and PASSWORD not in str(env)


def test_redacting_filter_hides_the_imap_password_in_messages_and_tracebacks():
    f = RedactingFilter("tok", PASSWORD)
    try:
        raise RuntimeError(f"boom {PASSWORD}")
    except RuntimeError:
        import sys

        rec = logging.LogRecord(
            "x", logging.ERROR, __file__, 1, "failed %s", (PASSWORD,), sys.exc_info()
        )
    f.filter(rec)
    assert PASSWORD not in rec.getMessage() and PASSWORD not in (rec.exc_text or "")


# --- settings -----------------------------------------------------------------------------------


def _env(monkeypatch, **kw):
    for k in (
        "CVBOOST_IMAP_USER",
        "CVBOOST_IMAP_PASSWORD",
        "CVBOOST_IMAP_HOST",
        "CVBOOST_MAIL_FORWARDERS",
    ):
        monkeypatch.delenv(k, raising=False)
    for k, v in kw.items():
        monkeypatch.setenv(k, v)


def test_no_imap_env_means_not_configured(monkeypatch):
    _env(monkeypatch)
    s = Settings.from_env()
    assert s.imap_creds() is None and s.mail_forwarders == ()
    assert not EmailAlertsConnector(s.imap_creds()).configured()


def test_only_one_of_user_and_password_is_an_error(monkeypatch):
    _env(monkeypatch, CVBOOST_IMAP_USER="a@example.com")
    with pytest.raises(ValueError, match="CVBOOST_IMAP_USER"):
        Settings.from_env()
    _env(monkeypatch, CVBOOST_IMAP_PASSWORD="x")
    with pytest.raises(ValueError):
        Settings.from_env()


def test_imap_host_is_validated_and_forwarders_parsed(monkeypatch):
    _env(
        monkeypatch,
        CVBOOST_IMAP_USER="a@example.com",
        CVBOOST_IMAP_PASSWORD="p",
        CVBOOST_IMAP_HOST="bad host/x",
    )
    with pytest.raises(ValueError, match="hostname"):
        Settings.from_env()
    _env(
        monkeypatch,
        CVBOOST_IMAP_USER="a@example.com",
        CVBOOST_IMAP_PASSWORD="p",
        CVBOOST_MAIL_FORWARDERS="Owner@Example.com, ",
    )
    s = Settings.from_env()
    assert s.imap_creds() == ImapCreds("a@example.com", "p") and s.mail_forwarders == (
        "owner@example.com",
    )
    assert s.imap_host == "imap.gmail.com" and s.imap_mailbox == "INBOX"
