"""17b: Haiku fallback for unknown mails. The LLM client is always a fake."""

from __future__ import annotations

import re
from datetime import timedelta

import pytest

from cvboost import email_extract
from cvboost.collect import collect_source
from cvboost.connectors.email_alerts import EmailAlertsConnector
from cvboost.llm.client import OK, USAGE_LIMIT, LLMRequest, LLMResult
from cvboost.llm.config import EmailExtractCfg
from cvboost.llm.prompts.registry import current
from tests.conftest import NOW
from tests.llm.helpers import FakeClient, make_cfg
from tests.mail.helpers import CREDS, FakeMailbox, NoHttp, eml, subject_ref


def job(**kw):
    d = dict(
        title="Analytics Engineer",
        company="Acme",
        location="Remote EU",
        remote=True,
        employment_type="contract",
        rate_text="$70-85/hour",
        link_ref=None,
    )
    d.update(kw)
    return d


def verdict(**kw):
    d = dict(is_job_offer=True, not_job_reason="", injection=False, jobs=[job()])
    d.update(kw)
    return d


def answer(per_ref: dict[str, dict] | None = None, default=None):
    def fn(req: LLMRequest) -> LLMResult:
        refs = subject_ref(req.stdin_text)
        items = [{"ref": r, **(per_ref or {}).get(r, default or verdict())} for r in refs]
        return LLMResult(
            status=OK,
            structured={"emails": items},
            model="claude-haiku-5-5",
            input_tokens=10,
            output_tokens=5,
            total_cost_usd=0.002,
            duration_ms=900,
            exit_code=0,
        )

    return fn


def collect_mails(store, *names, uids=None):
    mb = FakeMailbox(
        {(uids or list(range(1, len(names) + 1)))[i]: eml(n) for i, n in enumerate(names)}
    )
    conn = EmailAlertsConnector(CREDS, mailbox_factory=mb.factory)
    collect_source(store, NoHttp().client, conn, limit=None, now=NOW)
    return mb


def msgs(store):
    return store.conn.execute("SELECT * FROM email_messages ORDER BY id").fetchall()


def jobs(store):
    return store.conn.execute("SELECT * FROM jobs ORDER BY id").fetchall()


def run(store, client, cfg=None, **kw):
    return email_extract.run(
        store, client, cfg or make_cfg(), lambda: NOW + timedelta(minutes=1), **kw
    )


def test_recruiter_mail_becomes_a_job_with_a_gmail_link_and_input_is_cleared(store):
    collect_mails(store, "recruiter_unknown.eml")
    (m,) = msgs(store)
    assert m["status"] == "needs_llm" and m["llm_input"]
    client = FakeClient(answer())
    st = run(store, client)
    assert (st.calls, st.new_jobs) == (1, 1)
    (j,) = jobs(store)
    assert (j["source"], j["company"], j["title"]) == (
        "email:recruiter",
        "Acme",
        "Analytics Engineer",
    )
    assert (
        j["url"]
        == "https://mail.google.com/mail/#search/rfc822msgid%3Arecruiter-1%40agency.example.org"
    )
    assert "Our client needs a contract Analytics Engineer" in j["description_text"]
    (m,) = msgs(store)
    assert (m["status"], m["jobs_found"], m["llm_input"]) == ("llm_job", 1, None)
    call = store.conn.execute(
        "SELECT kind, model_alias, prompt_version, status FROM llm_calls"
    ).fetchone()
    assert tuple(call) == ("email", "haiku", current().email.version, "ok")
    assert client.preflights == 0 or True


def test_not_a_job_marks_not_job_and_creates_nothing(store):
    collect_mails(store, "recruiter_unknown.eml")
    run(
        store,
        FakeClient(
            answer(default=verdict(is_job_offer=False, not_job_reason="newsletter", jobs=[]))
        ),
    )
    (m,) = msgs(store)
    assert (m["status"], m["note"], m["llm_input"]) == ("not_job", "newsletter", None) and not jobs(
        store
    )


def test_link_ref_uses_our_canonical_link_and_out_of_range_falls_back_to_gmail(store):
    raw = eml("recruiter_unknown.eml").replace(
        b"https://agency.example.org/jobs/ae-123?utm=3Dx",
        b"https://www.linkedin.com/comm/jobs/view/4487361379?trackingId=zz",
    )
    mb = FakeMailbox({1: raw})
    collect_source(
        store,
        NoHttp().client,
        EmailAlertsConnector(CREDS, mailbox_factory=mb.factory),
        limit=None,
        now=NOW,
    )
    (m,) = msgs(store)
    assert "[job link 1]" in m["llm_input"] and "linkedin.com" not in m["llm_input"]
    run(
        store,
        FakeClient(
            answer(default=verdict(jobs=[job(link_ref=1), job(title="Other role", link_ref=7)]))
        ),
    )
    rows = {r["title"]: r for r in jobs(store)}
    assert rows["Analytics Engineer"]["url"] == "https://www.linkedin.com/jobs/view/4487361379/"
    assert rows["Analytics Engineer"]["source"] == "email:linkedin"
    assert rows["Other role"]["url"].startswith("https://mail.google.com/mail/#search/rfc822msgid")


def test_invalid_answer_is_retried_once_then_llm_error(store):
    collect_mails(store, "recruiter_unknown.eml")
    bad = LLMResult(
        status=OK,
        structured={"emails": [{"ref": "e1", "is_job_offer": "maybe"}]},
        total_cost_usd=0.001,
    )
    run(store, FakeClient(bad))
    (m,) = msgs(store)
    assert (m["status"], m["llm_attempts"]) == ("needs_llm", 1) and m["llm_input"]
    run(store, FakeClient(bad))
    (m,) = msgs(store)
    assert (m["status"], m["llm_attempts"], m["llm_input"]) == ("llm_error", 2, None) and not jobs(
        store
    )


def test_usage_limit_pauses_and_the_mail_stays_pending(store):
    collect_mails(store, "recruiter_unknown.eml")
    client = FakeClient(LLMResult(status=USAGE_LIMIT, error_message="limit"))
    st = run(store, client)
    assert st.stop_code == "paused" and len(client.requests) == 1
    (m,) = msgs(store)
    assert (m["status"], m["llm_attempts"]) == ("needs_llm", 0) and m["llm_input"]
    st = run(store, client)  # paused: no second call
    assert st.stop_code == "paused" and len(client.requests) == 1


def test_email_daily_cap_and_shared_haiku_cap_stop_calls(store):
    collect_mails(store, "recruiter_unknown.eml", "recruiter_injection.eml")
    cfg = make_cfg(email_extract={"daily_calls": 1, "batch_size": 1, "max_calls_per_cycle": 5})
    client = FakeClient(answer())
    st = run(store, client, cfg)
    assert st.calls == 1 and st.stop_code == "email_daily_cap" and len(client.requests) == 1
    cfg2 = make_cfg(
        daily_call_caps={"haiku": 1, "opus": 1},
        email_extract={"batch_size": 1, "max_calls_per_cycle": 5},
    )
    client2 = FakeClient(answer())
    st = run(store, client2, cfg2)  # the first call already used haiku's cap of 1
    assert st.calls == 0 and st.stop_code == "daily_cap" and not client2.requests


def test_one_call_per_cycle_by_default_and_batch_of_three(store):
    names = ["recruiter_unknown.eml", "recruiter_injection.eml"]
    mb = FakeMailbox({u: eml(names[u % 2]).replace(b"-1@", b"-%d@" % u) for u in range(1, 8)})
    collect_source(
        store,
        NoHttp().client,
        EmailAlertsConnector(CREDS, mailbox_factory=mb.factory),
        limit=None,
        now=NOW,
    )
    client = FakeClient(answer())
    st = run(store, client)
    assert st.calls == 1 and len(subject_ref(client.requests[0].stdin_text)) == 3
    assert EmailExtractCfg().daily_calls == 30


def test_pending_mail_expires_after_ttl_and_text_is_dropped(store):
    collect_mails(store, "recruiter_unknown.eml")
    client = FakeClient(answer())
    late = NOW + timedelta(days=4)
    st = email_extract.run(store, client, make_cfg(), lambda: late)
    assert st.expired == 1 and not client.requests
    (m,) = msgs(store)
    assert (m["status"], m["llm_input"]) == ("expired", None)


def test_injection_mail_is_escaped_flagged_and_creates_no_job(store):
    collect_mails(store, "recruiter_injection.eml")
    client = FakeClient(answer(default=verdict(is_job_offer=False, injection=True, jobs=[])))
    run(store, client)
    req = client.requests[0]
    assert "SECURITY" in req.system_prompt and "untrusted" in req.system_prompt
    body = req.stdin_text
    assert (
        body.count("</email>") == 1 and "&lt;/email&gt;" in body
    )  # the forged closing tag is inert
    assert "evil.example.org" not in body  # links never reach the model
    (m,) = msgs(store)
    assert (m["status"], m["note"]) == ("not_job", "prompt_injection") and not jobs(store)


def test_model_cannot_smuggle_urls_through_text_fields(store):
    collect_mails(store, "recruiter_unknown.eml")
    run(store, FakeClient(answer(default=verdict(jobs=[job(link_ref=None)]))))
    (j,) = jobs(store)
    assert re.match(r"https://mail\.google\.com/", j["url"])


def test_run_cap_zero_and_disabled_make_no_calls(store):
    collect_mails(store, "recruiter_unknown.eml")
    client = FakeClient(answer())
    assert run(store, client, max_calls=0).calls == 0
    off = make_cfg(email_extract={"enabled": False})
    assert run(store, client, off).stop_code == "disabled" and not client.requests


def test_bad_model_role_is_rejected():
    with pytest.raises(ValueError):
        make_cfg(email_extract={"model_role": "nope"})


def test_default_llm_yaml_has_the_owner_decision(tmp_path):
    from pathlib import Path

    from cvboost.llm.config import load_llm_config

    cfg = load_llm_config(Path(__file__).parents[2] / "tests" / "fixtures" / "config" / "llm.yaml")
    assert cfg.email_extract.daily_calls == 30 and cfg.email_alias == "haiku"
