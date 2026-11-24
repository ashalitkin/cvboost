"""#68: the "Check on the platform" badge in Inbox and on the job page."""

from __future__ import annotations

from fastapi.testclient import TestClient

from cvboost import stub_alert
from cvboost.web.app import create_app
from tests.conftest import NOW
from tests.rules_helpers import add, sync
from tests.web.conftest import db, llm_verdict, rule_verdict, verdict

STUB = (
    "Freelance Data Engineer at PIXIE. From a linkedin e-mail alert; "
    "the full description is on the platform."
)


def test_badge_in_inbox_and_job_page(settings, seeded):

    s = db(settings)
    sync(s, "email:linkedin")
    jid = add(
        s,
        "email:linkedin",
        "4490455840",
        company="PIXIE",
        title="Freelance Data Engineer",
        description_text=STUB,
        location_text="Madrid",
    )
    rule_verdict(s, jid)
    llm_verdict(s, jid, verdict(red_flags=[stub_alert.FLAG], fit=50))
    c = TestClient(create_app(settings, clock=lambda: NOW), follow_redirects=False)
    inbox = c.get("/inbox").text
    assert "Check on LinkedIn" in inbox and "check_on_platform" not in inbox
    job = c.get(f"/jobs/{jid}").text
    assert job.count("Check on LinkedIn") == 2  # next to the title and in the verdict
    assert "Check on LinkedIn" not in c.get(f"/jobs/{seeded.b}").text
