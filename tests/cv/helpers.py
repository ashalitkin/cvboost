from __future__ import annotations

from pathlib import Path

from cvboost.config import Settings
from cvboost.cv.profile import load_gaps, load_profile
from cvboost.cv.render import Rendered, RendererUnavailable
from cvboost.cv.schema import CvDraft
from cvboost.llm.client import OK, LLMRequest, LLMResult
from cvboost.llm.config import LLMConfig
from cvboost.storage.db import Store
from tests.conftest import NOW
from tests.fixture_profile import OUTREACH_YAML, PROFILE_YAML, resume_dir
from tests.llm.helpers import seed

ROOT = Path(__file__).resolve().parents[2]
RESUME_DIR = resume_dir()
PROFILE = load_profile(RESUME_DIR)
GAPS = load_gaps(RESUME_DIR)

ACME = "Analytics Solutions Architect, Acme Subscriptions (contract, remote), Feb 2018 – Aug 2022"
DEXP = "Founder & Data Consultant, Example Consulting (self-employed), Mar 2019 – present"

RESUME_MD = """\
![](photo.jpg){.photo}

# Jordan Example, PhD

Senior Analytics Engineer: BigQuery, dbt, Looker

Berlin, Germany | jordan@example.com | [LinkedIn](https://linkedin.com/in/jordan-example)

## Work arrangement

Based in Berlin, Germany. I typically work B2B via my own company, but I'm open to other
engagement models, including employment through an EOR. Overlap with US Pacific
9:00 AM - 12:00 PM PT. 7+ years working remotely with US clients.

## Summary

Analytics engineer with ~8 years in BI and analytics, 18 years in software and data.

## Experience

### Example Consulting, Founder & Data Consultant, Mar 2019 - present

- **Server-side tracking for a US digital agency.** Saved **12+ hours/week**
  of manual reporting.
- **Embedded analytics for a logistics SaaS.** Self-service for **60+** customer teams.

### Acme Subscriptions, Analytics Solutions Architect, Feb 2018 - Aug 2022

- Owned the BI architecture: data warehouse, pipelines and reporting.

## Education

- PhD, Computer Science, Example State University, 2009-2013
"""


def claims() -> list[dict]:
    return [
        {
            "resume_text": "Saved 12+ hours/week of manual reporting.",
            "section": DEXP,
            "profile_quote": "**12+ hours/week** of manual reporting saved",
        },
        {
            "resume_text": "Self-service for 60+ customer teams.",
            "section": DEXP,
            "profile_quote": "self-service for **60+** customer teams",
        },
        {
            "resume_text": "Owned the BI architecture.",
            "section": ACME,
            "profile_quote": (
                "Led the BI architecture: the warehouse, its pipelines and the reports"
            ),
        },
        {
            "resume_text": "PhD, Computer Science, Example State University",
            "section": "Education",
            "profile_quote": "PhD, Computer Science, Example State University, 2009–2013",
        },
        {
            "resume_text": "Overlap with US Pacific 9:00 AM - 12:00 PM PT",
            "section": "Work authorization and arrangement",
            "profile_quote": "Overlap with US Pacific: 9:00 AM – 12:00 PM PT",
        },
    ]


def draft_dict(**over) -> dict:
    d = {
        "resume_md": RESUME_MD,
        "recruiter_message": {
            "connection_note": "Hi, I am based in Germany and work B2B. Is that workable?",
            "inmail": "Hello. I am in Berlin, work as a B2B contractor, overlap 9-12 PT.",
        },
        "fit_notes_ru": {
            "matches": ["dbt, BigQuery"],
            "risks": ["нет разрешения на работу в США"],
            "gaps": [],
            "recommendation": "apply_with_caveats",
            "summary": "Хорошее совпадение по стеку.",
        },
        "claims": claims(),
        "injection_suspected": False,
    }
    d.update(over)
    return d


def draft(**over) -> CvDraft:
    return CvDraft.model_validate(draft_dict(**over))


def with_md(extra: str) -> dict:
    """A draft whose resume_md has `extra` appended to the Summary section."""
    return draft_dict(resume_md=RESUME_MD.replace("## Experience", extra + "\n\n## Experience", 1))


def ok_draft(dd: dict | None = None, **kw) -> LLMResult:
    args = dict(
        status=OK,
        structured=dd if dd is not None else draft_dict(),
        model="claude-opus-5-5",
        input_tokens=5000,
        output_tokens=2000,
        total_cost_usd=0.4,
        duration_ms=40000,
        exit_code=0,
    )
    args.update(kw)
    return LLMResult(**args)


class FakeRenderer:
    def __init__(self, unavailable: bool = False):
        self.unavailable = unavailable
        self.calls: list[str] = []
        self.letters: list[str] = []

    def render(self, resume_md: str) -> Rendered:
        self.calls.append(resume_md)
        if self.unavailable:
            raise RendererUnavailable("no pango")
        return Rendered(b"%PDF-1.4 fake", 1)

    def render_letter(self, letter_md: str) -> Rendered:
        self.letters.append(letter_md)
        if self.unavailable:
            raise RendererUnavailable("no pango")
        return Rendered(b"%PDF-1.4 fake letter", 1)


def make_settings(tmp_path: Path, **over) -> Settings:
    data = dict(
        db_path=tmp_path / "data" / "cvboost.sqlite",
        log_level="INFO",
        rules_path=ROOT / "tests" / "fixtures" / "config" / "rules.yaml",
        llm_config_path=ROOT / "tests" / "fixtures" / "config" / "llm.yaml",
        kits_override=tmp_path / "data" / "kits",
        panel_url="https://panel.example.com",
        resume_dir=RESUME_DIR,
        outreach_path=OUTREACH_YAML,
        profile_path=PROFILE_YAML,
    )
    data.update(over)
    return Settings(**data)


def cv_cfg(**over) -> LLMConfig:
    data = {"daily_call_caps": {"haiku": 60, "opus": 10}}
    data.update(over)
    return LLMConfig.model_validate(data)


def queue_request(store: Store, n: int = 1) -> list[tuple[int, int, int]]:
    """Seed n kept groups and one queued CV request each: [(request_id, job_id, canonical_id)]."""
    ids = seed(store, n)
    out = []
    for jid in ids:
        _, rid, created = store.request_cv(jid, channel="panel", now=NOW)
        assert created
        cid = store.conn.execute("SELECT canonical_id FROM jobs WHERE id=?", (jid,)).fetchone()[0]
        out.append((rid, jid, cid))
    return out


def last_stdin(client) -> str:
    req: LLMRequest = client.requests[-1]
    return req.stdin_text
