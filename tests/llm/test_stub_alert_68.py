"""#68: a stub e-mail-alert job is kept with `check_on_platform` instead of an llm_ineligible
reject for location doubt; already scored jobs are never queued again."""

from __future__ import annotations

from cvboost import stub_alert
from cvboost.core.rules import rule_version
from cvboost.llm.prompts.registry import score_version
from cvboost.score import score
from cvboost.triage import triage
from tests.conftest import NOW
from tests.llm.helpers import CFG, RV, THRESHOLD, FakeClient, make_cfg, ok_result, verdict_dict
from tests.rules_helpers import add, sync

STUB = (
    "Freelance Data Engineer at PIXIE. Location: Madrid. "
    "From a linkedin e-mail alert; the full description is on the platform."
)
REJECT = dict(
    eligible=False,
    eligibility_reason="Madrid, remote not stated, description is a stub",
    fit=30,
)


def seed_stub(
    store,
    source="email:linkedin",
    title="Freelance Data Engineer (dbt, BigQuery)",
    location="Madrid",
    desc=STUB,
):
    sync(store, source)
    jid = add(
        store,
        source,
        "4490455840",
        company="PIXIE",
        title=title,
        description_text=desc,
        location_text=location,
        employment_type="freelance",
        tags=["dbt", "bigquery", "sql"],
    )
    triage(store, CFG, NOW)
    return jid


def run(store, client):
    return score(store, client, make_cfg(), RV, THRESHOLD, lambda: NOW)


def llm_row(store):
    return store.llm_verdicts(score_version())[0]


def test_stub_location_doubt_is_kept_with_flag(store):
    seed_stub(store)
    st = run(store, FakeClient(lambda r: ok_result(r, {"j1": REJECT})))
    assert st.keep == 1 and st.reject == 0
    sg = llm_row(store)
    assert sg.decision == "keep" and sg.verdict.eligible
    assert stub_alert.FLAG in sg.verdict.red_flags
    reasons = store.conn.execute("SELECT reasons_json FROM verdicts WHERE stage='llm'").fetchone()[
        0
    ]
    assert "llm_ineligible" not in reasons


def test_explicit_blocker_stays_rejected(store):
    seed_stub(store, title="Freelance Data Engineer (US citizens only)")
    run(store, FakeClient(lambda r: ok_result(r, {"j1": REJECT})))
    assert llm_row(store).decision == "reject"


def test_blocker_in_reason_or_location_stays_rejected():
    v = stub_alert.apply  # pure function
    from cvboost.llm.schema import JobVerdict

    def go(reason, location="Madrid"):
        return v(
            JobVerdict(**verdict_dict(**{**REJECT, "eligibility_reason": reason})),
            sources=["email:linkedin"],
            description=STUB,
            has_extra=False,
            title="Freelance Data Engineer",
            location=location,
        )

    assert go("Madrid, remote not stated, description is a stub").eligible
    assert not go("On-site in Madrid, remote not stated").eligible
    assert not go("Requires US work authorization, location unclear").eligible
    assert not go("Rate below minimum, remote not stated").eligible
    assert not go("Madrid, remote not stated", location="Madrid, on-site").eligible


def test_full_description_or_other_source_is_unchanged(store):
    seed_stub(store, desc="Full real description of the role. " * 10)
    run(store, FakeClient(lambda r: ok_result(r, {"j1": REJECT})))
    assert llm_row(store).decision == "reject"
    # the same verdict for an ATS source: no change either
    assert not stub_alert.is_stub(["remoteok"], STUB, False)
    assert not stub_alert.is_stub(["email:linkedin"], STUB, True)  # owner pasted a description
    assert not stub_alert.is_stub(["email:linkedin", "remoteok"], STUB, False)


def test_indeed_platform_label():
    assert stub_alert.platform_of(["email:indeed"]) == "Indeed"
    assert stub_alert.platform_of(["email:recruiter"]) is None


def test_already_scored_jobs_are_not_rescored_and_stay_as_they_were(store, monkeypatch):
    """Scored before the release (rule off): reject stays; the release queues nothing."""
    seed_stub(store)
    with monkeypatch.context() as m:  # the code before the release
        m.setattr(stub_alert, "apply", lambda v, **kw: v)
        run(store, FakeClient(lambda r: ok_result(r, {"j1": REJECT})))
    assert llm_row(store).decision == "reject"
    version_before = score_version()
    assert rule_version(CFG) == RV  # no rules or prompt change: the version is the same key
    again = FakeClient()
    st = run(store, again)
    assert st.pending == 0 and again.requests == []
    assert store.conn.execute("SELECT COUNT(*) FROM rescore_requests").fetchone()[0] == 0
    assert (
        store.conn.execute("SELECT COUNT(*) FROM llm_calls WHERE kind='score'").fetchone()[0] == 1
    )
    assert llm_row(store).decision == "reject" and score_version() == version_before


def test_telegram_alert_gets_the_note(store):
    from cvboost.notify.alerts import build_view
    from cvboost.notify.format import render_alert
    from cvboost.worker_config import WorkerConfig

    seed_stub(store)
    run(store, FakeClient(lambda r: ok_result(r, {"j1": REJECT})))
    sg = llm_row(store)
    alert = {"canonical_id": sg.canonical_id, "job_id": sg.job_id}
    view = build_view(store, alert, sg.verdict, NOW, WorkerConfig())
    text = render_alert(view)
    assert "check the description on LinkedIn" in text and "check_on_platform" not in text


SPANISH = (
    "Buscamos un ingeniero de datos freelance para trabajar con nuestro equipo en Madrid. "
    "Serás responsable de diseñar y mantener los pipelines de datos, modelos en dbt y "
    "dashboards para el equipo de producto. Requisitos: experiencia sólida con SQL y "
    "BigQuery, buen nivel de comunicación y ganas de aprender. Ofrecemos un proyecto "
    "estable, flexibilidad horaria y un entorno de trabajo colaborativo con mucha autonomía."
)


def test_rescore_with_spanish_description_is_rejected_by_rules_without_llm(store):
    seed_stub(store)
    cid = store.kept_canonicals(RV, THRESHOLD)[0].canonical_id
    store.set_extra_description(cid, SPANISH, NOW)
    assert store.request_rescore(cid, NOW)
    client = FakeClient()
    st = score(
        store, client, make_cfg(), RV, THRESHOLD, lambda: NOW,
        max_calls=1, only_canonical=cid, rules_cfg=CFG,
    )  # fmt: skip
    assert client.requests == [] and client.preflights == 0
    assert st.reject == 1
    sg = llm_row(store)
    assert sg.decision == "reject" and "language" in sg.verdict.red_flags
    # English pasted text is not touched by the hard rules: goes to the model as before
    seed2 = FakeClient()
    cid2 = cid
    store.set_extra_description(cid2, "Remote contract, worldwide. SQL, dbt, BigQuery. " * 8, NOW)
    store.request_rescore(cid2, NOW) if not store.queued_rescore_for(cid2) else None
    score(
        store, seed2, make_cfg(), RV, THRESHOLD, lambda: NOW,
        max_calls=1, only_canonical=cid2, rules_cfg=CFG,
    )  # fmt: skip
    assert len(seed2.requests) == 1
