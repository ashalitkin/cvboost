"""Hard rules + relevance: config, rule_version, RuleInput and `evaluate`."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, ValidationError

from cvboost.core.geo import (
    HomeCfg,
    allows_broad,
    classify_location,
    mentions_allowed,
    names_non_latam_place,
)
from cvboost.core.language import detect_language
from cvboost.core.normalize import company_norm
from cvboost.core.relevance import CompiledRelevance, compile_relevance, score
from cvboost.core.salary import evaluate_rate

ENGINE_VERSION = 6  # 5: latam_only (issue 67); 6: home no-rate keep + remote/Europe title
MAX_TEXT = 30000
MAX_TITLE = 300
MAX_LOCATION = 500
MAX_SALARY_TEXT = 200


class _Cfg(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RateCfg(_Cfg):
    min_hourly_usd: float
    min_daily_usd: float
    min_annual_usd: float
    fx_to_usd: dict[str, float]
    assume_currency: str
    plausible: dict[str, tuple[float, float]]


class LocationCfg(_Cfg):
    allow: list[str]
    home: HomeCfg | None = None  # #59; the code and label are the owner's, the rest is generic


class LanguagesCfg(_Cfg):
    allowed: list[str]
    min_words: int


class DedupeCfg(_Cfg):
    window_days: int


class TitleGroup(_Cfg):
    weight: float
    terms: list[str]
    ignore_negative: bool = False  # #65: a hit in this group is exempt from negative_title


class ProfileCfg(_Cfg):
    title: list[TitleGroup]
    skills: list[str]
    require_title: bool = False  # #65: no title hit -> this profile scores 0
    # #65, both apply to this profile only (the global negative_title is for every profile):
    negative_title: list[str] = []  # soft penalty (negative_title_multiplier) for this profile
    exclude_title: list[str] = []  # the profile scores 0 when the title has one of these


class Weights(_Cfg):
    title: float
    tags: float = 0.0
    description: float


class RelevanceCfg(_Cfg):
    threshold: float
    weights: Weights
    weights_no_tags: Weights
    tags_saturation: float
    description_saturation: float
    negative_title_multiplier: float
    negative_title: list[str]
    profiles: dict[str, ProfileCfg]


class RulesConfig(_Cfg):
    rate: RateCfg
    location: LocationCfg
    languages: LanguagesCfg
    unwanted_titles: list[str]
    unwanted_stack: list[str] = []
    blocked_companies: list[str]
    dedupe: DedupeCfg
    relevance: RelevanceCfg


def load_config(path: Path | str) -> RulesConfig:
    try:
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
        return RulesConfig.model_validate(data)
    except (yaml.YAMLError, ValidationError, OSError) as exc:
        raise ValueError(f"bad rules config {path}: {exc}") from exc


def rule_version(cfg: RulesConfig) -> str:
    blob = json.dumps(cfg.model_dump(mode="json"), sort_keys=True)
    return f"r{ENGINE_VERSION}-{hashlib.sha256(blob.encode()).hexdigest()[:12]}"


@dataclass(frozen=True)
class RuleInput:
    company: str
    title: str
    description_text: str
    location_text: str | None
    tags: tuple[str, ...]
    salary_min: float | None
    salary_max: float | None
    salary_currency: str | None
    salary_period: str | None
    salary_text: str | None
    employment_type: str | None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> RuleInput:
        return cls(
            company=row["company"],
            title=row["title"],
            description_text=(row["description_text"] or "")[:MAX_TEXT],
            location_text=row["location_text"],
            tags=tuple(json.loads(row["tags_json"] or "[]")),
            salary_min=row["salary_min"],
            salary_max=row["salary_max"],
            salary_currency=row["salary_currency"],
            salary_period=row["salary_period"],
            salary_text=row["salary_text"],
            employment_type=row["employment_type"],
        )

    @classmethod
    def from_job(cls, job: Any) -> RuleInput:
        return cls(
            company=job.company,
            title=job.title,
            description_text=job.description_text[:MAX_TEXT],
            location_text=job.location_text,
            tags=tuple(job.tags),
            salary_min=job.salary_min,
            salary_max=job.salary_max,
            salary_currency=job.salary_currency,
            salary_period=job.salary_period,
            salary_text=job.salary_text,
            employment_type=job.employment_type,
        )

    def input_hash(self) -> str:
        blob = json.dumps(asdict(self), sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(blob.encode()).hexdigest()


@dataclass
class RuleResult:
    decision: str
    reasons: list[str]
    flags: list[str]
    relevance: int
    profile: str | None
    details: dict[str, Any] = field(default_factory=dict)


REASON_ORDER = [
    "blocked_company",
    "unwanted_role",
    "unwanted_stack",
    "language",
    "us_only",
    "region_restricted",
    "region_excluded",
    "latam_only",
    "region_eastern_europe",
    "region_uk",
    "w2_only",
    "onsite",
    "rate_below_min",
    # the home country's `<code>_no_rate` sits here (evaluate); db.rule_outcome sorts unknown
    # codes after the known ones
    "low_relevance",
]

_TITLE_TAIL = re.compile(
    r"(?:\s[-–—|]\s?|\(|,\s?)(?:remote\s?[-–—]?\s?)?"
    r"(US|USA|U\.S\.A?\.?|United States|North America|Canada|UK|United Kingdom|LATAM|"
    r"Latin America|APAC|India|Australia)(?:\sonly)?\s?\)?\s?$",
    re.I,
)
_AUTH = re.compile(
    r"(?:authori[sz]ed|eligible|legally (?:able|permitted|entitled)|right|permission) "
    r"to work in (?:the )?([A-Za-z .,&()-]{2,60})",
    re.I,
)
_RESIDE = re.compile(
    r"\b(?:must|required to|need to|needs to|should)\s(?:be\s)?(?:physically\s|currently\s)?"
    r"(?:reside|be based|be located|located|based|residing|live|living)\s(?:in|within)\s"
    r"(?:the\s|one of the following\s)?([^.\n]{2,150})",
    re.I,
)
_ONLY = re.compile(r"\bonly (?:hiring|considering|accepting|open to)\b([^.\n]{0,80})", re.I)
_CITIZEN = re.compile(
    r"\b(?:must|required|requires?|requirement|only|need(?:s)? to be|have to be)\b[^.\n]{0,40}?"
    r"\b(?:U\.?S\.? citizen(?:ship|s)?\b(?!\s?status)|"
    r"United States citizen(?:ship|s)?\b(?!\s?status)|"
    r"green card holder)"
    r"|\b(?:U\.?S\.?|United States) citizens? only\b"
    r"|\b(?:active|current|obtain|maintain)[^.\n]{0,30}"
    r"(?:security|secret|top secret|public trust) clearance",
    re.I,
)
_LATAM_T = r"(?:LATAM|Latin America(?:n)?|Latin American countries)"
# Explicit "hired in Latin America" (issue 67). An office there next to US offices is no signal.
_LATAM_ONLY = re.compile(
    rf"\b{_LATAM_T}[- ](?:only|based|residents?)\b"
    rf"|\b(?:based|located|residing|residents?|living)\s(?:in|within)\s(?:the\s)?{_LATAM_T}\b"
    rf"(?!\s?(?:,|&|/|\band\b|\bor\b))"
    rf"|\bonly\s(?:hiring|considering|accepting|open to)\b[^.\n]{{0,40}}\b{_LATAM_T}\b"
    rf"(?!\s?(?:,|&|/|\band\b|\bor\b))",
    re.I,
)
_NEG = re.compile(r"\b(?:not|no|never|don't|doesn't|do not|does not)\b", re.I)
_TZ = re.compile(r"time ?zones?|\bhours\b|overlap|\b(?:CET|CEST|EST|EDT|PST|PDT)\b", re.I)
_W2 = re.compile(
    r"\bW-?2\s?(?:only|contract|employee|employment|position|basis|hourly|role)\b"
    r"(?!\s?(?:/|or|and|,)\s?(?:c2c|1099|corp))"
    r"|\b(?:only|must be)\sW-?2\b"
    r"|\bno\s(?:c2c|corp[- ]to[- ]corp|1099|contractors|independent contractors)\b"
    r"|\b(?:c2c|corp[- ]to[- ]corp)\s(?:is\s)?not\s"
    r"(?:accepted|allowed|available|possible|considered)\b"
    r"|\bnot open to\s(?:c2c|corp[- ]to[- ]corp|contractors)\b",
    re.I,
)
_ONSITE_SHORT = re.compile(
    r"\b(?<!optional )(?:hybrid(?! (?:option|options|available|possible))"
    r"|on[- ]?site|in[- ]office|office[- ]based)\b",
    re.I,
)
_ONSITE_DESC = re.compile(
    r"\b(?:is|be) (?:a |an )?(?:fully )?(?:hybrid|on[- ]?site|in[- ]office|office[- ]based) "
    r"(?:role|position|job)\b"
    r"|\b(?:this|the) (?:role|position|job) is (?:a )?"
    r"(?:hybrid|on[- ]?site|in[- ]office|office[- ]based)\b"
    r"|\b\d\s?\+?\s?(?:days?|x)\s?(?:a|per|/)\s?week\s(?:in|at)\s(?:the|our)\s"
    r"[\w ,]{0,30}office\b"
    r"|\b(?:must|required to|expected to|need to)\s(?:be able to\s)?(?:work|commute|come)\s"
    r"(?:on[- ]?site|in(?:to)? (?:the|our) office)\b"
    r"|\bability to work on[- ]?site\b"
    r"|\b(?<!optional )hybrid (?:role|position|job)\b(?! option)",
    re.I,
)


_PART_TIME = re.compile(
    r"\bpart[-_ ]?time\b|\bfractional\b|\bfreelance\b|\b\d{1,2}(?:\s?[-–]\s?\d{1,2})?\s?"
    r"(?:h|hrs|hours)\s?(?:/|per|a)\s?(?:week|wk)\b",
    re.I,
)


_CURN = (
    r"(?:(?:US\$|\$|€|£|USD|EUR|GBP) ?\d[\d,.]{0,10}|\d[\d,.]{0,10} ?[kK]? ?(?:USD|EUR|GBP|€|\$|£))"
)
# Any amount of money (currency + number): Portugal-only roles with one go to the LLM
_MONEY = re.compile(_CURN, re.I)


# Portugal-only is dropped when the text also offers a broader allowed region
# "Data Engineer (Remote | Europe)": LinkedIn alerts carry only "Portugal" as the location
_TITLE_REMOTE_BROAD = re.compile(
    r"\bremote\b.{0,20}\b(?:europe|eu|emea|worldwide|anywhere|global)\b"
    r"|\b(?:europe|eu|emea|worldwide|anywhere|global)\b.{0,20}\bremote\b",
    re.I,
)
_OR_REMOTE = re.compile(
    r"(?:\bor|/) ?(?:fully |100% )?(?:remote|anywhere)\b|\b(?:remote|anywhere) ?(?:or|/)", re.I
)
_BROAD = re.compile(
    r"\banywhere in (?:the )?(?:eu|europe|european union|world)\b"
    r"|\bremote from (?:anywhere|(?:the )?(?:eu|europe))\b"
    r"|\b(?:eu|europe)[- ]wide\b|\bwithin (?:the )?(?:eu|europe)\b"
    r"|\b(?:remote|work(?:ing)?|hir(?:e|ing)|open to|located|based|from|candidates|applicants)\b"
    r"[^\n.]{0,30}?\bworldwide\b"
    r"|\bworldwide (?:remote|candidates|applicants|hiring)\b"
    r"|\b(?:remote|based|located|candidates|hiring)\b[^\n.]{0,30}?\bemea\b"
    r"|\bemea (?:remote|candidates|applicants|hiring)\b",
    re.I,
)
# UK-only roles (issue 23): kept only for contractors outside IR35 / non-UK residents / B2B
_UK_STRONG = re.compile(
    r"\boutside (?:of )?IR35\b|\bIR35[: -]{1,3}outside\b"
    r"|\bnon[- ]UK (?:resident|based|national|candidate)s?\b",
    re.I,
)
_UK_BAD = re.compile(
    r"\binside (?:of )?IR35\b|\bIR35[: -]{1,3}inside\b|\bumbrella\b"
    r"|\bright to work in the UK\b|\bUK right to work\b"
    r"|\bUK (?:based )?(?:resident|national|citizen)s? only\b",
    re.I,
)
_UK_WEAK = re.compile(
    r"\b(?:b2b|c2c|corp[- ]to[- ]corp|contractors?|freelance(?:rs?)?|self[- ]employed)\b", re.I
)
_UK_WEAK_SHORT = re.compile(r"\bcontract\b", re.I)  # title / employment type only
_NEGATED = re.compile(r"\b(?:not|no|never|n't)\b", re.I)


def _broad(desc: str) -> bool:
    for m in _BROAD.finditer(desc):
        if not _NEGATED.search(desc[max(0, m.start() - 25) : m.end()]):
            return True
    return False


_HYB_BEFORE = re.compile(
    r"(?:\b(?:remote|optional) ?(?:or|/|and) ?|\b(?:no|not|non|without)[- ])$", re.I
)
_HYB_AFTER = re.compile(
    r"^ ?(?:\( ?optional|\(? ?(?:option|available|possible)|(?:or|/) ?remote)", re.I
)


def _onsite_short(text: str) -> re.Match[str] | None:
    """Mandatory hybrid/on-site only: 'Remote or Hybrid', 'no hybrid', 'Hybrid (optional)' pass."""
    for m in _ONSITE_SHORT.finditer(text):
        if m.group(0).lower() == "hybrid" and (
            _HYB_BEFORE.search(text[max(0, m.start() - 14) : m.start()])
            or _HYB_AFTER.match(text[m.end() : m.end() + 14])
        ):
            continue
        return m
    return None


# analytics/measurement/data-architecture around paid media is the candidate's field (#28)
_ANALYTICS_TITLE = re.compile(
    r"analyst|analytics|measurement|attribution|tracking|data architect", re.I
)
# never rescued by an analytics word in the title (owner: data science; 2026-11-23: product analyst)
_ALWAYS_UNWANTED = re.compile(r"data scien|product (?:data )?analyst")


def fold(text: str) -> str:
    """Collapse whitespace runs (NBSP included) so no regex ever sees a long run of blanks."""
    text = re.sub(r"[^\S\n]+", " ", text)
    return re.sub(r" ?\n[ \n]*", "\n", text).strip()


# A line that marks optional skills ("nice to have", "a plus") or a section heading that opens /
# closes an optional block. Headings are short lines.
_OPTIONAL = re.compile(
    r"nice[- ]to[- ]have|nice to haves|\bbonus\b|\bplus\b|preferred|desirable|desired|advantage|"
    r"ideally|would be great|good to have|not required|not a must|familiarity with|exposure to|"
    r"optional|extra points|is a big plus",
    re.I,
)
_REQUIRED_HEAD = re.compile(
    r"requirements?|must[- ]haves?|qualifications|what you(?:'ll)? (?:bring|need)|you have|"
    r"about you|responsibilit|what you(?:'ll)? do|looking for|tech stack|our stack|skills",
    re.I,
)
_HEAD_MAX = 60


def _required_stack(title: str, desc: str, rx: re.Pattern[str]) -> str | None:
    """Evidence for the first required Spark/streaming mention, None if all are optional."""
    m = rx.search(title)
    if m:
        return f"title: {m.group(0)}"
    optional_section = False
    for line in desc.split("\n"):
        short = len(line) <= _HEAD_MAX
        m = rx.search(line)
        if short and not m:
            if _OPTIONAL.search(line):
                optional_section = True
            elif _REQUIRED_HEAD.search(line):
                optional_section = False
            continue
        if not m or optional_section:
            continue
        # optional marker in the same sentence as the mention
        for sent in re.split(r"(?<=[.;!?])\s", line):
            sm = rx.search(sent)
            if sm and not _OPTIONAL.search(sent):
                return f"text: {_snip(sent)}"
    return None


def _snip(text: str, m: re.Match[str] | None = None, limit: int = 120) -> str:
    frag = m.group(0) if m else text
    return re.sub(r"\s+", " ", frag).strip()[:limit]


class Compiled:
    def __init__(self, cfg: RulesConfig):
        self.cfg = cfg
        self.allow = tuple(t.lower() for t in cfg.location.allow)
        self.home = cfg.location.home
        code = self.home.code if self.home else None
        self.home_only = f"{code}_only" if code else None  # reason of geo, never stored
        self.home_no_rate = f"{code}_no_rate" if code else None
        self.home_rate_check = f"{code}_rate_check" if code else None
        order = list(REASON_ORDER)
        if self.home_no_rate:
            order.insert(order.index("low_relevance"), self.home_no_rate)
        self.reason_order = order
        self.relevance: CompiledRelevance = compile_relevance(cfg.relevance)
        # entries are regex fragments (matched on the lower-cased title, word-bounded)
        terms = sorted({t.lower() for t in cfg.unwanted_titles}, key=lambda t: (-len(t), t))
        self.unwanted = (
            re.compile(r"(?<![a-z0-9])(?:" + "|".join(terms) + r")(?![a-z0-9])") if terms else None
        )
        self.blocked = {company_norm(c) for c in cfg.blocked_companies}
        stack = sorted(set(cfg.unwanted_stack), key=lambda t: (-len(t), t))
        self.stack = (
            re.compile(r"(?<![a-z0-9])(?:" + "|".join(stack) + r")(?![a-z0-9])", re.I)
            if stack
            else None
        )


def compile_rules(cfg: RulesConfig) -> Compiled:
    return Compiled(cfg)


def _citizen_match(desc: str) -> re.Match[str] | None:
    for m in _CITIZEN.finditer(desc):
        before = desc[max(0, m.start() - 25) : m.start()].rsplit(". ", 1)[-1]
        if _NEG.search(before + " " + m.group(0)):
            continue  # "do not need to be a US citizen"
        return m
    return None


_COND_FLAGS = frozenset({"uk_conditional", "switzerland", "israel", "uae"})


def _weak_contractor(text: str) -> bool:
    for m in _UK_WEAK.finditer(text):
        if not _NEGATED.search(text[max(0, m.start() - 25) : m.start()]):
            return True
    return False


def _uk_verdict(title: str, location_text: str, desc: str, employment_type: str) -> bool:
    """True when a UK-only role is acceptable (contractor outside IR35 / non-UK / B2B)."""
    text = f"{title}\n{location_text}\n{desc}"
    if _UK_STRONG.search(text):
        return True
    if _UK_BAD.search(text):
        return False
    return bool(
        _weak_contractor(text)
        or _UK_WEAK_SHORT.search(title)
        or _UK_WEAK_SHORT.search(employment_type)
    )


def _latam_match(desc: str) -> re.Match[str] | None:
    for m in _LATAM_ONLY.finditer(desc):
        if _NEG.search(desc[max(0, m.start() - 25) : m.start()]):
            continue  # "not only hiring in LATAM"
        return m
    return None


def _location_reason(
    title: str,
    location_text: str,
    desc: str,
    employment_type: str,
    c: Compiled,
    evidence: dict[str, str],
) -> tuple[set[str], set[str]]:
    found: set[str] = set()
    flags: set[str] = set()
    explicit = False
    broad = _broad(desc)

    def take(
        res: tuple[str | None, str, tuple[str, ...]], src: str, from_desc: bool = False
    ) -> None:
        verdict, ev, fl = res
        cond = set(fl) & _COND_FLAGS
        if from_desc and cond and not verdict and "eu_country_specific" not in fl:
            # "must be authorized to work in the UK / based in Switzerland": a real restriction
            verdict = "region_restricted"
        else:
            flags.update(fl)
        if verdict:
            found.add(verdict)
            evidence.setdefault(verdict, f"{src}: {ev}"[:140])

    res = classify_location(location_text, c.allow, c.home)
    if res[0] and res[0] == c.home_only and _OR_REMOTE.search(location_text):
        res = (None, "", ())  # "Lisbon or Remote"
    take(res, f"location_text '{_snip(location_text)}'")
    if _TITLE_REMOTE_BROAD.search(title):
        broad = True
    tm = _TITLE_TAIL.search(title)
    if tm:
        take(classify_location(tm.group(1), c.allow, c.home), f"title '{_snip(title)}'")
    for rx in (_AUTH, _RESIDE, _ONLY):
        for m in rx.finditer(desc):
            if rx is _RESIDE and _TZ.search(m.group(1)):
                flags.add("tz_overlap")
                continue
            res = classify_location(m.group(1), c.allow, c.home)
            if res[0] is None and allows_broad(m.group(1), c.allow, c.home):
                broad = True
            take(res, f"description '{_snip(desc, m)}'", from_desc=True)
    cm = _citizen_match(desc)
    if cm:
        take(("us_only", cm.group(0), ()), f"description '{_snip(desc, cm)}'")
    lm = _latam_match(desc)
    if lm:
        found.add("latam_only")
        evidence.setdefault("latam_only", f"description '{_snip(desc, lm)}'")
        explicit = True
    if names_non_latam_place(location_text, c.allow, c.home):
        flags.add("loc_not_latam")
    if "latam_only" in found and evidence.get("latam_only", "").startswith("location_text"):
        flags.add("latam_loc")
    if broad:
        found.discard(c.home_only)
        found.discard("region_excluded")
        if not explicit:
            found.discard("latam_only")  # explicitly worldwide / EMEA remote: HQ does not matter
    elif (
        (flags & (_COND_FLAGS | {"eu_country_specific"})) == {"uk_conditional"}
        and not _uk_verdict(title, location_text, desc, employment_type)
        and not mentions_allowed(f"{title}\n{location_text}\n{desc}", c.allow, c.home)
    ):
        found.add("region_uk")
        evidence.setdefault("region_uk", "UK only; no contractor / outside IR35 / B2B stated")
    return found, flags


def _first_seen_date(value: datetime | str | None) -> date | None:
    if value is None:
        return None
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            return None
    return value.date()


def evaluate(
    inp: RuleInput, c: Compiled, *, first_seen_at: datetime | str | None = None
) -> RuleResult:
    """`first_seen_at` only picks the home no-rate behaviour (`no_rate_keep_since`); unknown =
    a new posting."""
    cfg = c.cfg
    reasons: set[str] = set()
    flags: set[str] = set()
    evidence: dict[str, str] = {}
    # fold whitespace once, before any regex, and cap every field (ReDoS guard)
    desc = fold(inp.description_text[:MAX_TEXT])
    title = fold(inp.title[:MAX_TITLE])
    location_text = fold((inp.location_text or "")[:MAX_LOCATION])
    salary_text = fold(inp.salary_text[:MAX_SALARY_TEXT]) if inp.salary_text else None

    if company_norm(inp.company) in c.blocked:
        reasons.add("blocked_company")
        evidence["blocked_company"] = inp.company

    um = c.unwanted.search(title.lower()) if c.unwanted else None
    if um and not _ALWAYS_UNWANTED.match(um.group(0)) and _ANALYTICS_TITLE.search(title):
        um = None  # "Paid Media Analytics Manager" is the candidate's field, not campaign operation
    if um:
        reasons.add("unwanted_role")
        evidence["unwanted_role"] = f"title: {um.group(0)}"

    sm = _required_stack(title, desc, c.stack) if c.stack else None
    if sm:
        reasons.add("unwanted_stack")
        evidence["unwanted_stack"] = sm

    lang = detect_language(f"{title} {desc}", tuple(cfg.languages.allowed), cfg.languages.min_words)
    if lang:
        reasons.add("language")
        evidence["language"] = f"detected {lang}"

    loc, loc_flags = _location_reason(
        title, location_text, desc, inp.employment_type or "", c, evidence
    )
    flags |= loc_flags
    reasons |= loc
    if "us_only" in reasons:
        reasons.discard("region_restricted")
        evidence.pop("region_restricted", None)
    home_only = c.home_only in reasons if c.home_only else False
    reasons.discard(c.home_only)
    if reasons & {
        "us_only",
        "region_restricted",
        "region_excluded",
        "latam_only",
        "region_eastern_europe",
        "region_uk",
    }:
        home_only = False

    wm = _W2.search(title) or _W2.search(desc)
    if wm:
        reasons.add("w2_only")
        evidence["w2_only"] = f"text: {_snip(wm.group(0))}"

    om = _onsite_short(title) or _onsite_short(location_text)
    src = "title/location"
    if not om:
        om = _ONSITE_DESC.search(desc)
        src = "description"
    if om:
        reasons.add("onsite")
        evidence["onsite"] = f"{src}: {_snip(om.group(0))}"

    rate = evaluate_rate(
        salary_min=inp.salary_min,
        salary_max=inp.salary_max,
        salary_currency=inp.salary_currency,
        salary_period=inp.salary_period,
        salary_text=salary_text,
        description=desc,
        cfg=cfg.rate,
    )
    if rate.unknown:
        flags.add("rate_unknown")
    elif rate.low_unconfirmed:
        flags.add("rate_low_unconfirmed")
    elif rate.reject:
        reasons.add("rate_below_min")
        evidence["rate_below_min"] = json.dumps(rate.detail)

    if home_only and cfg.location.home.require_stated_rate and rate.unknown:
        if any(_MONEY.search(t) for t in (title, location_text, salary_text or "", desc)):
            flags.add(c.home_rate_check)  # some amount is stated: Haiku decides
        else:
            since = cfg.location.home.no_rate_keep_since
            seen = _first_seen_date(first_seen_at)
            if since is not None and (seen is None or seen >= since):
                flags.add(c.home_no_rate)  # kept: the normal LLM + alert path
                evidence.pop(c.home_only, None)
            else:
                reasons.add(c.home_no_rate)
                evidence[c.home_no_rate] = (
                    f"{evidence.pop(c.home_only, cfg.location.home.label)}; no rate stated"
                )[:140]

    rel = score(title, list(inp.tags), desc, c.relevance)
    if rel.relevance < cfg.relevance.threshold:
        reasons.add("low_relevance")
        evidence["low_relevance"] = f"relevance {rel.relevance} < {cfg.relevance.threshold:g}"

    if (inp.employment_type and _PART_TIME.search(inp.employment_type[:40])) or _PART_TIME.search(
        title
    ):
        flags.add("part_time_ok")  # visibility only: part-time / fractional / freelance welcome

    ordered = [r for r in c.reason_order if r in reasons]
    return RuleResult(
        decision="reject" if ordered else "keep",
        reasons=ordered,
        flags=sorted(flags),
        relevance=rel.relevance,
        profile=rel.profile,
        details={
            "evidence": {r: evidence[r] for r in ordered if r in evidence},
            "rate": rate.detail,
            "relevance": rel.per_profile,
        },
    )
