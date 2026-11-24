"""#28: dashboards/BI, analytics consultants, analytics architects/leads and tracking roles pass at
every funnel level; roles that OPERATE campaigns (#14) and non-analytics consultants stay out."""

from __future__ import annotations

import pytest

from cvboost.connectors.ats import title_matches
from cvboost.connectors.lever import LeverConnector
from tests.rules_helpers import COMPILED, RULES_PATH, ev

KEEP = [
    # dashboards / BI
    "BI Developer", "Looker Developer", "LookML Developer", "Looker Studio Developer",
    "Dashboard Developer", "Dashboard Engineer", "Reporting Analyst", "Reporting Engineer",
    "BI Engineer", "Power BI Developer", "Tableau Developer",
    # consultants
    "Analytics Consultant", "Data Consultant", "BI Consultant", "Marketing Analytics Consultant",
    "Measurement Consultant", "Tracking Consultant", "Freelance Analytics Consultant",
    "Business Intelligence Consultant", "Looker Consultant",
    # tech lead / architect
    "Analytics Lead", "Analytics Tech Lead", "Analytics Architect", "Data Architect",
    "BI Architect", "Head of Analytics", "Head of Data", "Principal Analytics Engineer",
    "Paid Media Analytics Lead", "Marketing Measurement Architect", "Marketing Data Architect",
    "Measurement Lead", "Tracking & Attribution Engineer", "Ads Data Engineer",
    "Paid Media Data Architect", "PPC Analytics Architect", "Head of BI", "Technical Lead - Data",
    "Architect, Data Engineering", "Ad Tech Architect", "Marketing Technology Architect",
    "Data Visualization Developer", "Data Visualization Engineer", "Reporting Manager",
    "Web Analyst", "Digital Analyst", "Paid Media Analyst", "Media Reporting & Insights Analyst",
    "Reporting & Insights Analyst", "PowerBI Developer", "Power BI Specialist",
    "Marketing Technologist",
    # tracking
    "Tracking Specialist", "Tracking Engineer", "Web Analytics Specialist", "GA4 Specialist",
    "Server-side GTM Engineer", "Measurement Engineer",
    "Conversion Tracking Specialist", "Conversions API Engineer", "CAPI Specialist",
    "Tagging Specialist", "Analytics Implementation Specialist", "Attribution Engineer",
    "Tag Management Specialist",
]  # fmt: skip
REJECT = [
    "SAP Consultant", "Sales Consultant", "Database Administrator", "IT Consultant",
    "Management Consultant", "Customer Success Consultant", "Implementation Specialist",
    "GTM Strategy Manager", "GTM Engineer", "SAP BI Specialist", "SAP BI Manager",
    "SAP Data Lead", "Salesforce Data Lead", "Clinical Data Lead", "Sales Data Lead",
    "Security Data Lead", "Data Lead, Machine Learning", "Java Reporting Engineer",
    "Regulatory Reporting Developer", "Sales Tag Manager", "Master Data Consultant",
    "Financial Reporting Specialist", "Tax Reporting Lead", "ESG Reporting Specialist",
    "Financial Reporting Lead", "Tax Reporting Specialist", "ESG Reporting Lead",
    "SAP Analytics Consultant",
    "Solution Architect (SharePoint, Power Platform, Power BI, Azure)",
]  # fmt: skip
UNWANTED = [
    "Media Buyer", "Senior Media Buyer", "PPC Manager", "Paid Media Manager", "Campaign Manager",
    "Performance Marketer", "Performance Marketing Manager", "Growth Marketing Manager",
    "Paid Social Specialist", "Head of Paid Media", "Performance Marketers",
    "Senior Media Buyer / Reporting", "PPC Manager - Reporting", "Paid Media Manager & Reporting",
    "Performance Marketing Manager, BI", "Paid Media Architect",
    "Paid Social Specialist (Dashboards)",
]  # fmt: skip


@pytest.mark.parametrize("title", KEEP)
def test_new_roles_pass_rules_and_prefilter(title):
    r = ev(title=title, description_text="")
    assert r.decision == "keep", (title, r.reasons)
    assert title_matches(title, LeverConnector(rules_path=RULES_PATH)._title_re)


@pytest.mark.parametrize("title", REJECT)
def test_non_analytics_roles_are_low_relevance(title):
    r = ev(title=title, description_text="")
    assert "low_relevance" in r.reasons


@pytest.mark.parametrize("title", UNWANTED)
def test_campaign_operation_stays_unwanted(title):
    assert "unwanted_role" in ev(title=title).reasons


def test_media_buyer_with_analytics_stack_stays_unwanted():
    desc = (
        "Launch and optimise Meta Ads and Google Ads campaigns, manage the budget, test creatives. "
        "You work with GA4, GTM, Conversions API and Looker Studio dashboards to read results."
    )
    r = ev(title="Remote Senior Media Buyer", description_text=desc)
    assert "unwanted_role" in r.reasons and r.decision == "reject"


def test_power_bi_specialist_scores_below_looker():
    from cvboost.core.relevance import score

    rel = COMPILED.relevance
    assert (
        score("Power BI Specialist", [], "", rel).relevance
        < score("Looker Developer", [], "", rel).relevance
    )


def test_tableau_scores_below_looker_but_passes():
    from cvboost.core.relevance import score

    rel = COMPILED.relevance
    tab = score("Tableau Developer", [], "", rel).relevance
    assert 30 <= tab < score("Looker Developer", [], "", rel).relevance
