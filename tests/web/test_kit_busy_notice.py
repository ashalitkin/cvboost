"""The kit is read-only while a CV request is queued or running: the page must say so and poll."""

from pathlib import Path

TEMPLATE = Path(__file__).parents[2] / "src/cvboost/web/templates/partials/kit.html"


def test_busy_notice_explains_read_only_and_polls():
    t = TEMPLATE.read_text()
    assert 'id="kit-busy"' in t
    assert 'hx-trigger="every 20s"' in t and 'hx-select="#kit"' in t
    assert "read-only" in t
