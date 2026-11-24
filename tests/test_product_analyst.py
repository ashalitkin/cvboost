"""Owner 2026-11-23: product analyst roles are not his field, a hard reject by rules (no LLM).
The analytics-word exemption that keeps "Paid Media Analytics Lead" must not rescue them."""

from __future__ import annotations

import pytest

from tests.rules_helpers import ev

UNWANTED = [
    "Product Analyst", "Senior Product Analyst", "Staff Product Analyst, Marketplace",
    "Senior Product Data Analyst", "Lead Product Analyst (Growth)", "Product Analysts",
    "Senior Product Analyst, Digital Measurement",
]  # fmt: skip
KEEP = [
    "Senior Web Tracking Engineer (Product Analytics)", "Product Analytics Engineer",
    "Marketing Analyst", "Analytics Engineer", "Data Analyst",
]  # fmt: skip


@pytest.mark.parametrize("title", UNWANTED)
def test_product_analyst_is_unwanted(title):
    r = ev(title=title)
    assert r.decision == "reject" and "unwanted_role" in r.reasons, (title, r.reasons)


@pytest.mark.parametrize("title", KEEP)
def test_neighbours_are_not_caught(title):
    assert "unwanted_role" not in ev(title=title).reasons, title
