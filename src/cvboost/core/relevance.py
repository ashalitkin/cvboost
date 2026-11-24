"""Cheap keyword relevance (no AI): two profiles, title > tags > description."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any


def _rx(terms: list[str]) -> re.Pattern[str]:
    ordered = sorted(set(t.lower() for t in terms), key=lambda t: (-len(t), t))
    body = "|".join(re.escape(t) for t in ordered)
    return re.compile(rf"(?<![a-z0-9])(?:{body})(?![a-z0-9])")


@dataclass(frozen=True)
class CompiledProfile:
    name: str
    title_groups: list[tuple[float, re.Pattern[str], bool]]
    skills: re.Pattern[str]
    require_title: bool = False
    negative: re.Pattern[str] | None = None
    exclude: re.Pattern[str] | None = None


@dataclass(frozen=True)
class CompiledRelevance:
    cfg: Any
    profiles: list[CompiledProfile]
    negative: re.Pattern[str]


@dataclass
class RelevanceResult:
    relevance: int
    profile: str | None
    per_profile: dict[str, dict[str, Any]] = field(default_factory=dict)
    negative_hit: str | None = None


def compile_relevance(cfg: Any) -> CompiledRelevance:
    profiles = [
        CompiledProfile(
            name=name,
            title_groups=[(g.weight, _rx(g.terms), g.ignore_negative) for g in p.title],
            skills=_rx(p.skills),
            require_title=p.require_title,
            negative=_rx(p.negative_title) if p.negative_title else None,
            exclude=_rx(p.exclude_title) if p.exclude_title else None,
        )
        for name, p in cfg.profiles.items()
    ]
    return CompiledRelevance(cfg, profiles, _rx(cfg.negative_title))


def _hits(rx: re.Pattern[str], text: str) -> list[str]:
    return list(dict.fromkeys(m.group(0) for m in rx.finditer(text)))


def score(
    title: str, tags: list[str], description: str, comp: CompiledRelevance
) -> RelevanceResult:
    cfg = comp.cfg
    title_l = title.lower()
    tags_l = " , ".join(t.lower() for t in tags)
    desc_l = description.lower()
    neg = comp.negative.search(title_l)
    best = RelevanceResult(0, None)
    for p in comp.profiles:
        title_hits: list[str] = []
        t = 0.0
        exempt = False  # a full-weight or ignore_negative title hit is not hit by negative_title
        for weight, rx, ignore_neg in p.title_groups:
            h = _hits(rx, title_l)
            if h:
                title_hits += h
                t = max(t, weight)
                exempt = exempt or weight >= 1.0 or ignore_neg
        tag_hits = _hits(p.skills, tags_l) if tags else []
        desc_hits = _hits(p.skills, desc_l)
        g = min(1.0, len(tag_hits) / cfg.tags_saturation)
        d = min(1.0, len(desc_hits) / cfg.description_saturation)
        if tags:
            w = cfg.weights
            sc = 100 * (w.title * t + w.tags * g + w.description * d)
        else:
            w = cfg.weights_no_tags
            sc = 100 * (w.title * t + w.description * d)
        if (p.require_title and t == 0.0) or (p.exclude and p.exclude.search(title_l)):
            sc = 0.0
        if p.negative and p.negative.search(title_l):
            sc *= cfg.negative_title_multiplier  # profile-only soft penalty, never exempted
        if neg and not exempt:
            sc *= cfg.negative_title_multiplier
        per = {
            "score": round(sc, 1),
            "title": title_hits,
            "tags": tag_hits,
            "description": desc_hits,
        }
        best.per_profile[p.name] = per
        if round(sc) > best.relevance or best.profile is None:
            best.relevance = round(sc)
            best.profile = p.name
    best.negative_hit = neg.group(0) if neg else None
    return best
