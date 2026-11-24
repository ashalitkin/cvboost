"""Location lexicon and a shared parser for `location_text` and location-like phrases."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from functools import lru_cache

from pydantic import BaseModel, ConfigDict, field_validator


class HomeCfg(BaseModel):
    """rules.yaml `location.home`: the one country the candidate lives in (#59). A posting that
    names only it (or its cities) needs a stated rate: `<code>_only` -> `<code>_no_rate` /
    `<code>_rate_check`. Every other region list stays in this module (tuned for an EU-based
    contractor)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    code: str  # machine word in reason codes and flags: "portugal" -> portugal_no_rate
    label: str  # shown to the owner: "Португалия"
    terms: list[str]  # country and city names that mean "home and nothing else"
    require_stated_rate: bool = True
    # Owner 2026-11-23: home-only postings with no rate first seen on/after this date are kept with
    # the flag `<code>_no_rate` (the normal LLM + alert path) instead of being rejected. Earlier
    # ones keep the old reject. None = always reject.
    no_rate_keep_since: date | None = None

    @field_validator("code")
    @classmethod
    def _code(cls, v: str) -> str:
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,23}", v):
            raise ValueError("code: lower-case letters, digits and _, at most 24 chars")
        return v

    @field_validator("label")
    @classmethod
    def _label(cls, v: str) -> str:
        if not v.strip() or "\n" in v:
            raise ValueError("label: one non-empty line")
        return v.strip()

    @field_validator("terms")
    @classmethod
    def _terms(cls, v: list[str]) -> list[str]:
        if not v or any(not t.strip() for t in v):
            raise ValueError("terms: at least one non-empty name")
        return v


_US_ABBR_TERMS = ["us", "u.s.", "usa", "u.s.a."]
_US_PHRASES = [
    "united states",
    "united states of america",
    "america",
    "north america",
    "us timezones",
    "us time zones",
    "continental us",
]

US_STATES = [
    "alabama", "alaska", "arizona", "arkansas", "california", "colorado", "connecticut",
    "delaware", "florida", "hawaii", "idaho", "illinois", "indiana", "iowa", "kansas",
    "kentucky", "louisiana", "maine", "maryland", "massachusetts", "michigan", "minnesota",
    "mississippi", "missouri", "montana", "nebraska", "nevada", "new hampshire", "new jersey",
    "new mexico", "new york", "north carolina", "north dakota", "ohio", "oklahoma", "oregon",
    "pennsylvania", "rhode island", "south carolina", "south dakota", "tennessee", "texas",
    "utah", "vermont", "virginia", "washington", "west virginia", "wisconsin", "wyoming",
    "district of columbia",
]  # fmt: skip

US_CITIES = [
    "new york city", "nyc", "boston", "seattle", "san francisco", "palo alto", "menlo park",
    "austin", "dallas", "chicago", "los angeles", "waltham", "denver", "atlanta", "miami",
    "san diego", "san jose", "houston", "philadelphia", "phoenix", "portland", "san mateo",
    "columbus", "indianapolis", "louisville", "sacramento", "tampa", "orlando", "kansas city",
    "st louis", "cleveland", "cincinnati", "baltimore", "scotch plains", "jersey city", "newark",
    "buffalo", "omaha", "tulsa", "boise", "madison", "milwaukee", "richmond", "albany",
    "mountain view", "brooklyn", "washington dc", "washington d.c.", "salt lake city",
    "minneapolis", "pittsburgh", "raleigh", "nashville", "charlotte", "las vegas", "detroit",
    "cambridge ma", "boulder", "irvine", "santa monica", "sunnyvale", "redwood city",
]  # fmt: skip

EU_EEA = [
    "austria", "belgium", "cyprus", "denmark", "finland", "france", "germany", "greece",
    "ireland", "italy", "luxembourg", "malta", "netherlands", "the netherlands", "spain",
    "sweden", "iceland", "liechtenstein", "norway",
    # big cities (not the ones that are also US towns: Paris, Rome, Dublin, Valencia, ...)
    "madrid", "barcelona", "seville", "malaga", "bilbao", "berlin", "munich", "hamburg",
    "frankfurt", "cologne", "stuttgart", "dusseldorf", "amsterdam", "rotterdam", "brussels",
    "copenhagen", "stockholm", "lyon", "marseille",
]  # fmt: skip
# Eastern Europe is not wanted (owner, 2026-11-08): its own bucket, never `eu`.
EASTERN_EUROPE = [
    "poland", "czechia", "czech republic", "slovakia", "hungary", "romania", "bulgaria",
    "croatia", "slovenia", "serbia", "bosnia", "bosnia and herzegovina", "montenegro",
    "north macedonia", "macedonia", "albania", "kosovo", "moldova", "ukraine", "belarus",
    "russia", "estonia", "latvia", "lithuania", "baltics", "baltic states", "eastern europe",
    "cee", "central and eastern europe", "central & eastern europe",
]  # fmt: skip
# The home country (rules.yaml `location.home`, #59) is configured, not listed here: its terms
# stay in `location.allow` and are the labels that mean "the home country and nothing else".
# UK and Switzerland are not EU.

# Not wanted at all (owner, 2026-11-08, issue 23): Asia, Latin America, Australia/NZ, Saudi Arabia.
# A posting that is explicitly worldwide/EMEA remote still passes (see rules._location_reason).
# Latin America: a hire there is a local hire (issue 67), so it has its own reason `latam_only`.
LATAM = [
    "argentina", "brazil", "chile", "colombia", "mexico", "peru", "uruguay", "venezuela",
    "ecuador", "bolivia", "paraguay", "costa rica", "panama", "guatemala", "honduras",
    "el salvador", "nicaragua", "dominican republic", "cuba", "latam", "latin america",
    "south america", "central america", "sao paulo", "buenos aires", "bogota", "mexico city",
    "medellin", "lima peru", "porto alegre", "porto velho",
]  # fmt: skip
EXCLUDED_FAR = [
    # Asia
    "china", "hong kong", "japan", "korea", "south korea", "taiwan", "singapore", "india",
    "indonesia", "malaysia", "philippines", "thailand", "vietnam", "viet nam", "bangladesh",
    "pakistan", "sri lanka", "nepal", "cambodia", "myanmar", "mongolia", "laos", "apac",
    "asia", "asia pacific", "southeast asia", "south east asia", "tokyo", "seoul", "beijing",
    "shanghai", "shenzhen", "bangalore", "bengaluru", "mumbai", "hyderabad", "pune", "chennai",
    "gurgaon", "gurugram", "noida", "kolkata", "manila", "jakarta", "bangkok", "kuala lumpur",
    "ho chi minh city",
    # Australia / New Zealand
    "australia", "new zealand", "anz", "oceania", "sydney", "melbourne", "brisbane", "perth",
    "auckland", "wellington nz", "new south wales",
    # Saudi Arabia
    "saudi arabia", "ksa", "riyadh", "jeddah",
]  # fmt: skip
EXCLUDED_FAR = [*EXCLUDED_FAR, *LATAM]
_LATAM_SET = frozenset(LATAM)
# Canada is handled like the US (full-time local hiring; owner has no work authorization).
CANADA = [
    "canada", "toronto", "vancouver", "montreal", "ottawa", "calgary", "ontario",
    "british columbia", "quebec", "alberta",
]  # fmt: skip
# Conditional: kept with a flag, the LLM (or rules.py for the UK) decides by the posting text.
CONDITIONAL = {
    "uk_conditional": [
        "uk", "u.k.", "united kingdom", "united kingdom of great britain and northern ireland",
        "great britain", "britain", "england", "scotland", "wales", "northern ireland",
        "london", "manchester", "edinburgh", "birmingham", "leeds", "bristol", "glasgow",
        "belfast", "cardiff", "liverpool",
    ],
    "switzerland": ["switzerland", "zurich", "geneva", "basel", "zug", "lausanne", "bern"],
    "israel": ["israel", "tel aviv", "jerusalem", "haifa", "herzliya"],
    "uae": ["uae", "united arab emirates", "dubai", "abu dhabi"],
}  # fmt: skip

US_STATE_ABBR = (
    "AL AK AZ AR CA CO CT DE FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT NE NV NH NJ "
    "NM NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY DC"
).split()
# Abbreviations that are also ISO country codes (IN India, DE Germany, CO Colombia, ...):
# they count as a US state only after a known US city.
_AMBIGUOUS_ABBR = frozenset(
    "AL AR AZ CA CO DE GA ID IL IN LA MA MD ME MN MO MS MT NC NE PA SC SD TN VA".split()
)
_STATE_ABBR_RE = re.compile(r"([A-Za-z.' -]{2,40}),[ ]{0,3}(" + "|".join(US_STATE_ABBR) + r")\b")

COUNTRIES = [
    "afghanistan", "albania", "algeria", "andorra", "angola", "antigua and barbuda", "argentina",
    "armenia", "australia", "austria", "azerbaijan", "bahamas", "bahrain", "bangladesh",
    "barbados", "belarus", "belgium", "belize", "benin", "bhutan", "bolivia",
    "bosnia and herzegovina", "bosnia", "botswana", "brazil", "brunei", "bulgaria",
    "burkina faso", "burundi", "cabo verde", "cape verde", "cambodia", "cameroon", "canada",
    "central african republic", "chad", "chile", "china", "colombia", "comoros", "congo",
    "costa rica", "cote d'ivoire", "ivory coast", "croatia", "cuba", "cyprus", "czechia",
    "czech republic", "denmark", "djibouti", "dominica", "dominican republic", "ecuador",
    "egypt", "el salvador", "equatorial guinea", "eritrea", "estonia", "eswatini", "ethiopia",
    "fiji", "finland", "france", "gabon", "gambia", "georgia", "germany", "ghana", "greece",
    "grenada", "guatemala", "guinea", "guinea-bissau", "guyana", "haiti", "honduras",
    "hong kong", "hungary", "iceland", "india", "indonesia", "iran", "iraq", "ireland",
    "israel", "italy", "jamaica", "japan", "jordan", "kazakhstan", "kenya", "kiribati",
    "kosovo", "kuwait", "kyrgyzstan", "laos", "latvia", "lebanon", "lesotho", "liberia",
    "libya", "liechtenstein", "lithuania", "luxembourg", "madagascar", "malawi", "malaysia",
    "maldives", "mali", "malta", "marshall islands", "mauritania", "mauritius", "mexico",
    "micronesia", "moldova", "monaco", "mongolia", "montenegro", "morocco", "mozambique",
    "myanmar", "namibia", "nauru", "nepal", "netherlands", "the netherlands", "new zealand",
    "nicaragua", "niger", "nigeria", "north korea", "north macedonia", "macedonia", "norway",
    "oman", "pakistan", "palau", "palestine", "panama", "papua new guinea", "paraguay", "peru",
    "philippines", "poland", "puerto rico", "qatar", "romania", "russia", "rwanda",
    "saint kitts and nevis", "saint lucia", "saint vincent and the grenadines", "samoa",
    "san marino", "sao tome and principe", "saudi arabia", "senegal", "serbia", "seychelles",
    "sierra leone", "singapore", "slovakia", "slovenia", "solomon islands", "somalia",
    "south africa", "south korea", "korea", "south sudan", "spain", "sri lanka", "sudan",
    "suriname", "sweden", "switzerland", "syria", "taiwan", "tajikistan", "tanzania",
    "thailand", "timor-leste", "togo", "tonga", "trinidad and tobago", "tunisia", "turkey",
    "turkiye", "turkmenistan", "tuvalu", "uganda", "ukraine", "united arab emirates",
    "united kingdom", "united kingdom of great britain and northern ireland", "uruguay",
    "uzbekistan", "vanuatu", "vatican", "venezuela", "vietnam", "viet nam", "yemen", "zambia",
    "zimbabwe",
]  # fmt: skip

OTHER_ALIASES = [
    "uk", "britain", "great britain", "england", "scotland", "wales", "northern ireland", "uae",
    "americas", "latam", "latin america", "south america", "central america", "apac", "asia",
    "asia pacific", "africa", "middle east", "mena", "oceania", "anz", "nordics", "dach",
    "benelux", "central europe", "northern europe", "caribbean",
    "scandinavia", "emea ex-eu", "porto alegre", "porto velho",
]  # fmt: skip

MAX_LOCATION = 500
_FLAGS = re.compile("[\U0001f1e6-\U0001f1ff]")
_PAREN_SCOPE = re.compile(r"^([^()]*)\((.*)\) ?$", re.S)
# A parenthesis that lists exclusions, examples or preferences must not narrow the scope:
# "(incl. Poland)", "(excl. Russia)", "(Poland preferred)" -> analyse the text before it.
_INCL = re.compile(
    r"\s{0,5}(?:incl|including|inc|e\.g|i\.e|excl|excluding|except|ex|not|outside|other than)\b"
    r"|[^()]{0,200}\b(?:preferred|hq|headquarters?|headquartered)\b",
    re.I,
)
_OTHER_COUNT = re.compile(r"\+ ?\d{1,4} {1,3}other")


def _home_terms(home: HomeCfg | None) -> tuple[str, ...]:
    return tuple(t.lower() for t in home.terms) if home else ()


@dataclass(frozen=True)
class _Matcher:
    regex: re.Pattern[str]
    labels: dict[str, str]


@lru_cache(maxsize=8)
def _matcher(allow: tuple[str, ...], home: tuple[str, ...] = ()) -> _Matcher:
    labels: dict[str, str] = {}
    for t in [*COUNTRIES, *OTHER_ALIASES]:
        labels[t] = "other"
    for t in EU_EEA:
        labels[t] = "eu"
    for t in EASTERN_EUROPE:
        labels[t] = "east"
    for t in [*US_STATES, *US_CITIES, *_US_ABBR_TERMS, *_US_PHRASES, *CANADA]:
        labels[t] = "us"
    for t in EXCLUDED_FAR:
        labels[t] = "far"
    for flag, terms in CONDITIONAL.items():
        for t in terms:
            labels[t] = f"cond:{flag}"
    for t in allow:
        labels[t.lower()] = "allow"
    # Home terms come last on purpose: they replace the same term in `allow` (rules.yaml lists the
    # home terms in `allow` too, so a config without a `home` block behaves as before) and in the
    # built-in country lists. A location with home terms only is then "home" (-> <code>_only);
    # next to another allowed place ("Lisbon or worldwide") the "allow" hit still wins (see below).
    for t in home:
        labels[t.lower()] = "home"
    terms = sorted(labels, key=lambda t: (-len(t), t))
    body = "|".join(re.escape(t) for t in terms)
    regex = re.compile(rf"(?<![a-z0-9])(?:{body})(?![a-z0-9])", re.I)
    return _Matcher(regex, labels)


def _scan(text: str, m: _Matcher) -> list[tuple[str, str]]:
    hits = []
    for mt in m.regex.finditer(text):
        raw = mt.group(0)
        term = raw.lower()
        # the pronoun "us" must not count: only upper-case "US" is the country
        if term == "us" and raw != "US":
            continue
        hits.append((m.labels[term], term))
    return hits


def _state_abbr_us(text: str) -> str | None:
    for m in _STATE_ABBR_RE.finditer(text):
        city, abbr = m.group(1).strip().lower(), m.group(2)
        if abbr not in _AMBIGUOUS_ABBR or any(
            city == c or city.endswith(" " + c) for c in US_CITIES
        ):
            return m.group(0).strip()[-40:]
    return None


def classify_location(
    text: str | None, allow: tuple[str, ...], home: HomeCfg | None = None
) -> tuple[str | None, str, tuple[str, ...]]:
    """Return (`us_only` | `region_restricted` | `region_excluded` | `latam_only` |
    `region_eastern_europe` |
    `<home code>_only` | None, evidence, flags).

    `<home>_only` (`portugal_only` for code `portugal`): the text names the home country (or a
    city of it) and nothing else; the caller decides by the rate.
    `region_excluded`: Asia / AU-NZ / Saudi Arabia (or Latin America next to those).
    `latam_only`: the text names Latin American places and nothing else.

    flags: `eu_country_specific` when the text names only specific EU/EEA countries;
    `uk_conditional`, `switzerland`, `israel`, `uae` when the text names those (kept, the caller
    or the LLM decides by the posting text).
    """
    none: tuple[str, ...] = ()
    if not text:
        return None, "", none
    text = _FLAGS.sub("", text).strip()[:MAX_LOCATION]
    m = _matcher(allow, _home_terms(home))
    scope = text
    pm = _PAREN_SCOPE.match(text)
    if pm and _INCL.match(pm.group(2)):
        scope = pm.group(1) if pm.group(1).strip() else text
    elif pm and _scan(pm.group(2), m):
        scope = pm.group(2)
    if _OTHER_COUNT.search(scope):
        return None, "", none
    hits = _scan(scope, m)
    kinds = {k.split(":")[0] for k, _ in hits}
    cond = tuple(sorted({k.split(":")[1] for k, _ in hits if k.startswith("cond:")}))
    if "allow" in kinds:
        return None, "", none
    if kinds == {"home"} and home:
        return f"{home.code}_only", ", ".join(dict.fromkeys(t for _, t in hits))[:80], none
    if "home" in kinds:
        return None, "", none  # the home country next to other places: acceptable as before
    ev_all = ", ".join(dict.fromkeys(t for _, t in hits))[:80]
    if "eu" in kinds:
        if "other" in kinds:
            return "region_restricted", ev_all, none
        if "far" in kinds:
            return "region_excluded", ev_all, none  # EU next to Asia/LatAm: lifted by worldwide
        return None, ev_all, ("eu_country_specific", *cond)
    if "us" in kinds:
        return "us_only", ", ".join(dict.fromkeys(t for k, t in hits if k == "us"))[:80], none
    if cond:
        return None, ev_all, cond
    if "far" in kinds and "other" not in kinds:
        if all(t in _LATAM_SET for k, t in hits if k == "far"):
            return "latam_only", ev_all, none
        return "region_excluded", ev_all, none
    if "east" in kinds and "other" not in kinds:
        return "region_eastern_europe", ev_all, none
    if kinds & {"other", "far"}:
        return "region_restricted", ev_all, none
    # last resort: "City, ST" (two-letter state code)
    st = _state_abbr_us(text)
    if st:
        return "us_only", st, none
    return None, "", none


def allows_broad(text: str, allow: tuple[str, ...], home: HomeCfg | None = None) -> bool:
    """True when the text names an allowed region other than home (Europe, EU, worldwide...)."""
    if not text:
        return False
    return any(
        k == "allow" for k, _ in _scan(text[:MAX_LOCATION], _matcher(allow, _home_terms(home)))
    )


_VAGUE = frozenset({"global", "globally", "international", "anywhere"})
_NEG_BEFORE = re.compile(r"\b(?:not|no|never|n't|outside)\b", re.I)


def mentions_allowed(text: str, allow: tuple[str, ...], home: HomeCfg | None = None) -> bool:
    """True when the text names an EU/EEA/home/Europe place outside a negation."""
    if not text:
        return False
    m = _matcher(allow, _home_terms(home))
    text = text[:30000]
    for mt in m.regex.finditer(text):
        term = mt.group(0).lower()
        if m.labels[term] not in ("allow", "eu", "home") or term in _VAGUE:
            continue
        if _NEG_BEFORE.search(text[max(0, mt.start() - 25) : mt.start()]):
            continue
        return True
    return False


def names_non_latam_place(
    text: str | None, allow: tuple[str, ...], home: HomeCfg | None = None
) -> bool:
    """True when the location names any place that is not Latin American (Portugal, EU,
    worldwide, US...): such a sighting says the posting is not a LatAm-only hire."""
    if not text:
        return False
    hits = _scan(_FLAGS.sub("", text)[:MAX_LOCATION], _matcher(allow, _home_terms(home)))
    return any(t not in _LATAM_SET for _, t in hits)
