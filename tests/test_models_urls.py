from __future__ import annotations

import pytest
from pydantic import ValidationError

from cvboost.textutil import canonical_url
from tests.conftest import make_job


def test_url_with_control_char_rejected():
    with pytest.raises(ValidationError):
        make_job(url="https://example.com/a\x1bb")
    with pytest.raises(ValidationError):
        make_job(url="https://example.com/a b")


@pytest.mark.parametrize("apply_url", ["https://example.com/a\x1b", "javascript:alert(1)"])
def test_bad_apply_url_becomes_none(apply_url):
    assert make_job(apply_url=apply_url).apply_url is None


def test_good_apply_url_kept_verbatim():
    u = "https://example.com/apply?utm_source=x&ref=1"
    assert make_job(apply_url=u).apply_url == u


def test_canonical_url_strips_utm_only():
    assert canonical_url("https://Example.com/p?utm_source=a&x=1&UTM_Medium=b&y=2#f") == (
        "https://example.com/p?x=1&y=2"
    )
    assert canonical_url("https://e.com/p?utm_source=a") == "https://e.com/p"
    u = "https://e.com/p?utm_source=a&x=1"
    assert canonical_url(u, strip_utm=False) == u
