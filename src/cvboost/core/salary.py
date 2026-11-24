"""Rate extraction from structured fields, free text and descriptions; compare with minimums."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

CUR = r"(?:US\$|\$|€|£|USD|EUR|GBP)"
AMT = (
    rf"({CUR})? ?(\d[\d,.]{{0,12}}) ?([kK])?"
    rf"(?:[ ]{{0,3}}(?:-|–|—|to)[ ]{{0,3}}({CUR})? ?(\d[\d,.]{{0,12}}) ?([kK])?)?"
)
PERIOD = [
    ("hour", r"(?:/ ?|per |an? )(?:hr|hour)\b|hourly"),
    ("day", r"(?:/ ?|per |a )day\b|daily"),
    ("month", r"(?:/ ?|per |a )(?:mo|month)\b|monthly"),
    ("year", r"(?:/ ?|per |an? )(?:yr|year|annum)\b|annual|annually|yearly"),
]
DESC_SALARY = (
    r"(?:US\$|\$|€|£) ?\d[\d,.]{0,12} ?[kK]?"
    r"(?:[ ]{0,3}(?:-|–|—|to)[ ]{0,3}(?:US\$|\$|€|£)? ?\d[\d,.]{0,12} ?[kK]?)?"
    r"[ ]{0,3}(?:USD|EUR)?[ ]{0,3}"
    r"(?:(?:/ ?|per |an? )(?:hr|hour|year|yr|annum)\b|hourly|annually)"
)
CONTEXT = re.compile(r"salary|compensation|\bpay\b|\brate\b|\bbase\b|\brange\b|\bOTE\b", re.I)
CONTEXT_WINDOW = 60
MAX_SALARY_TEXT = 200

_CODES = "USD EUR GBP CAD AUD NZD CHF INR PLN BRL MXN SEK NOK DKK CZK JPY CNY SGD".split()
_AMT_RE = re.compile(AMT)
_PERIOD_RE = [(p, re.compile(rx, re.I)) for p, rx in PERIOD]
_DESC_RE = re.compile(DESC_SALARY, re.I)
_NO_PERIOD_HINT = re.compile(r"\b(?:task|project|commission)\b", re.I)
_SYMBOL = {
    "$": "USD",
    "us$": "USD",
    "usd": "USD",
    "€": "EUR",
    "eur": "EUR",
    "£": "GBP",
    "gbp": "GBP",
}


@dataclass(frozen=True)
class Candidate:
    source: str
    low: float
    high: float
    currency: str
    period: str
    confirmed: bool = True


@dataclass(frozen=True)
class RateResult:
    unknown: bool
    reject: bool
    detail: dict[str, Any] | None
    low_unconfirmed: bool = False


def _num(s: str) -> float | None:
    s = s.strip(",.")
    if re.fullmatch(r"\d+,\d", s):
        s = s.replace(",", ".")
    else:
        s = s.replace(",", "")
    try:
        return float(s)
    except ValueError:
        return None


def _detect_period(text: str) -> str | None:
    for period, rx in _PERIOD_RE:
        if rx.search(text):
            return period
    return None


def _parse_amount(text: str, assume: str) -> tuple[float, float, str] | None:
    m = _AMT_RE.search(text)
    if not m:
        return None
    c1, n1, k1, c2, n2, k2 = m.groups()
    lo = _num(n1)
    hi = _num(n2) if n2 else lo
    if lo is None or hi is None:
        return None
    if k1 or k2:
        lo, hi = lo * 1000, hi * 1000
    sym = (c1 or c2 or "").lower()
    if not sym:
        suffix = re.search(r"\b(" + "|".join(_CODES) + r")\b", text, re.I)
        sym = suffix.group(1).lower() if suffix else ""
    cur = _SYMBOL.get(sym, sym.upper()) if sym else assume
    return min(lo, hi), max(lo, hi), cur


def _from_text(text: str, source: str, assume: str, *, explicit_only: bool) -> list[Candidate]:
    out: list[Candidate] = []
    if explicit_only:
        pieces = [
            (
                m.group(0),
                bool(
                    CONTEXT.search(
                        text[max(0, m.start() - CONTEXT_WINDOW) : m.end() + CONTEXT_WINDOW]
                    )
                ),
            )
            for m in _DESC_RE.finditer(text)
        ]
    else:
        pieces = [(text[:MAX_SALARY_TEXT], True)]
    for piece, confirmed in pieces:
        parsed = _parse_amount(piece, assume)
        if not parsed:
            continue
        lo, hi, cur = parsed
        period = _detect_period(piece)
        if period is None:
            if _NO_PERIOD_HINT.search(piece) or hi < 10000:
                continue
            period = "year"
        out.append(Candidate(source, lo, hi, cur, period, confirmed))
    return out


def _best(cands: list[Candidate], cfg: Any) -> tuple[Candidate, float, float] | None:
    best = None
    for c in cands:
        lo, hi = cfg.plausible[c.period]
        if not lo <= c.high <= hi:
            continue
        if c.currency not in cfg.fx_to_usd:  # unknown currency: never guess USD
            continue
        usd = c.high * cfg.fx_to_usd[c.currency]
        ratio = usd / minimum(c.period, cfg)
        if best is None or ratio > best[2]:
            best = (c, usd, ratio)
    return best


def minimum(period: str, cfg: Any) -> float:
    return {
        "hour": cfg.min_hourly_usd,
        "day": cfg.min_daily_usd,
        "year": cfg.min_annual_usd,
        "month": cfg.min_annual_usd / 12,
    }[period]


def evaluate_rate(
    *,
    salary_min: float | None,
    salary_max: float | None,
    salary_currency: str | None,
    salary_period: str | None,
    salary_text: str | None,
    description: str,
    cfg: Any,
) -> RateResult:
    """`cfg` is the `rate` section of the rules config."""
    sources: list[list[Candidate]] = []
    hi = salary_max if salary_max is not None else salary_min
    lo = salary_min if salary_min is not None else salary_max
    if hi is not None and lo is not None:
        period = salary_period
        if period is None and hi >= 10000:
            period = "year"
        if period:
            cur = (salary_currency or cfg.assume_currency).upper()
            sources.append([Candidate("fields", lo, hi, cur, period)])
    if salary_text:
        sources.append(
            _from_text(salary_text, "salary_text", cfg.assume_currency, explicit_only=False)
        )
    if description:
        sources.append(
            _from_text(description, "description", cfg.assume_currency, explicit_only=True)
        )
    for cands in sources:
        for confirmed in (True, False):
            best = _best([c for c in cands if c.confirmed is confirmed], cfg)
            if not best:
                continue
            c, usd, ratio = best
            detail = {
                "source": c.source,
                "currency": c.currency,
                "period": c.period,
                "max": c.high,
                "usd_equiv": round(usd, 2),
                "confirmed": confirmed,
            }
            if not confirmed and ratio < 1:
                # a low figure with no salary-like context: do not reject on it
                return RateResult(unknown=False, reject=False, detail=detail, low_unconfirmed=True)
            return RateResult(unknown=False, reject=ratio < 1, detail=detail)
    return RateResult(unknown=True, reject=False, detail=None)
