"""Explicit connector registry."""

from __future__ import annotations

from pathlib import Path

from cvboost.connectors.adzuna import AdzunaConnector
from cvboost.connectors.arbeitnow import ArbeitnowConnector
from cvboost.connectors.ashby import AshbyConnector
from cvboost.connectors.ats import ATSConnector
from cvboost.connectors.base import Connector
from cvboost.connectors.email_alerts import EmailAlertsConnector
from cvboost.connectors.fractionaljobs import FractionalJobsConnector
from cvboost.connectors.gem import GemConnector
from cvboost.connectors.greenhouse import GreenhouseConnector
from cvboost.connectors.himalayas import HimalayasConnector
from cvboost.connectors.hn_hiring import HNHiringConnector
from cvboost.connectors.hn_jobs import HNJobsConnector
from cvboost.connectors.itjobs import ITJobsConnector
from cvboost.connectors.jobicy import JobicyConnector
from cvboost.connectors.jobindex_dk import JobindexDKConnector
from cvboost.connectors.jobtech_se import JobTechSEConnector
from cvboost.connectors.lever import LeverConnector
from cvboost.connectors.mastodon import MastodonConnector
from cvboost.connectors.personio import PersonioConnector
from cvboost.connectors.realworkfromanywhere import RealWorkFromAnywhereConnector
from cvboost.connectors.recruitee import RecruiteeConnector
from cvboost.connectors.reed import ReedConnector
from cvboost.connectors.remotefirstjobs import RemoteFirstJobsConnector
from cvboost.connectors.remoteok import RemoteOKConnector
from cvboost.connectors.remotive import RemotiveConnector
from cvboost.connectors.teamtailor import TeamtailorConnector
from cvboost.connectors.workable import WorkableConnector
from cvboost.connectors.workingnomads import WorkingNomadsConnector
from cvboost.connectors.wwr import WWRConnector
from cvboost.mail.imap import ImapCreds

REGISTRY: dict[str, type[Connector]] = {
    c.name: c
    for c in (
        RemotiveConnector,
        RemoteOKConnector,
        WWRConnector,
        HimalayasConnector,
        JobicyConnector,
        WorkingNomadsConnector,
        ArbeitnowConnector,
        HNHiringConnector,
        GreenhouseConnector,
        LeverConnector,
        AshbyConnector,
        RemoteFirstJobsConnector,
        RealWorkFromAnywhereConnector,
        HNJobsConnector,
        JobTechSEConnector,
        JobindexDKConnector,
        MastodonConnector,
        WorkableConnector,
        RecruiteeConnector,
        TeamtailorConnector,
        PersonioConnector,
        GemConnector,
        EmailAlertsConnector,
        AdzunaConnector,
        ReedConnector,
        ITJobsConnector,
        FractionalJobsConnector,
    )
}


def _make(
    cls: type[Connector],
    companies_path: Path | str | None,
    rules_path: Path | str | None,
    imap: ImapCreds | None,
    forwarders: tuple[str, ...],
) -> Connector:
    if issubclass(cls, ATSConnector):
        return cls(companies_path, rules_path)
    if issubclass(cls, EmailAlertsConnector):
        return cls(imap, forwarders=forwarders)
    return cls()


def all_source_names() -> list[str]:
    """Every `jobs.source` value: connector names plus the sub-sources of e-mail connectors."""
    return sorted({n for c in REGISTRY.values() for n in (c.name, *c.job_sources)})


def get(
    names: list[str] | None = None,
    *,
    companies_path: Path | str | None = None,
    rules_path: Path | str | None = None,
    imap: ImapCreds | None = None,
    forwarders: tuple[str, ...] = (),
) -> list[Connector]:
    if not names:
        return [
            _make(cls, companies_path, rules_path, imap, forwarders) for cls in REGISTRY.values()
        ]
    unknown = [n for n in names if n not in REGISTRY]
    if unknown:
        raise ValueError(
            f"unknown source(s): {', '.join(unknown)}; known: {', '.join(sorted(REGISTRY))}"
        )
    return [_make(REGISTRY[n], companies_path, rules_path, imap, forwarders) for n in names]
