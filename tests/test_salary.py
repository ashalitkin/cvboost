from __future__ import annotations

import pytest

from tests.rules_helpers import ev


@pytest.mark.parametrize(
    ("kw", "reject", "unknown"),
    [
        ({"salary_text": "$45/hr"}, True, False),
        ({"salary_text": "$55/hr"}, False, False),
        (
            {
                "salary_min": 120000,
                "salary_max": 120000,
                "salary_currency": "USD",
                "salary_period": "year",
            },
            False,
            False,
        ),
        ({"salary_text": "$120k/yr"}, False, False),
        ({}, False, True),
        ({"salary_text": "€40/hour"}, True, False),
        ({"salary_text": "€50/hour"}, False, False),
        ({"salary_text": "$40-60/hr"}, False, False),
        ({"salary_text": "$3.5k–$5k/month"}, True, False),
        ({"salary_text": "$35,3k- $52k"}, True, False),
        ({"salary_text": "$1000 per task"}, False, True),
        ({"description_text": "We give a $1,800 annually learning budget."}, False, True),
        ({"salary_min": 30, "salary_max": 36, "salary_period": "year"}, False, True),
        ({"description_text": "Pay: $20/hr for juniors or $80/hr for seniors."}, False, False),
        ({"salary_text": "70K-80K"}, True, False),
    ],
)
def test_rates(kw, reject, unknown):
    r = ev(**kw)
    assert ("rate_below_min" in r.reasons) is reject
    assert ("rate_unknown" in r.flags) is unknown
