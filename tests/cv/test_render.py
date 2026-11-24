from __future__ import annotations

import pytest

from cvboost.cv import render as R
from tests.cv.helpers import RESUME_DIR, RESUME_MD


def _weasy_ok() -> bool:
    try:
        import weasyprint  # noqa: F401
    except (ImportError, OSError):
        return False
    return True


needs_weasy = pytest.mark.skipif(
    not _weasy_ok(), reason="weasyprint/pango not available here (QA runs it in Docker)"
)


def test_raw_html_is_escaped_and_attributes_are_limited_to_class():
    md = (
        "![](photo.jpg){onclick=evil()}\n\n![](photo.jpg){.photo}\n\n# Jordan\n\n"
        "<script>alert(1)</script> "
        "<img src=x onerror=y>\n\n[a](javascript:alert(1)) [b](https://example.com)"
    )
    html = R.markdown_to_html(md)
    assert "<script" not in html and "<img src=x" not in html
    assert "&lt;script&gt;" in html
    assert 'class="photo"' in html and "onclick=" not in html.replace("{onclick=evil()}", "")
    assert '<img src="photo.jpg" alt="" onclick' not in html
    assert "javascript:" not in html.replace("[a](javascript:alert(1))", "")  # no href with it
    assert 'href="https://example.com"' in html and 'href="javascript' not in html


def test_photo_is_the_only_resource_the_fetcher_serves():
    fetch = R.WeasyRenderer(RESUME_DIR)._fetcher(b"JPEG") if _weasy_ok() else None
    if fetch is None:
        pytest.skip("weasyprint not available")
    assert fetch(R.PHOTO_URL).read() == b"JPEG"
    for url in (
        "file:///etc/passwd",
        "https://example.com/x.png",
        "http://169.254.169.254/latest",
        "data:image/png;base64,AAAA",
        R.BASE_URL + "other.jpg",
    ):
        with pytest.raises(ValueError):
            fetch(url)


def test_missing_weasyprint_is_reported_as_unavailable(monkeypatch):
    import builtins

    real = builtins.__import__

    def fake(name, *a, **k):
        if name == "weasyprint":
            raise OSError("cannot load library 'libgobject-2.0-0'")
        return real(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", fake)
    with pytest.raises(R.RendererUnavailable):
        R.WeasyRenderer(RESUME_DIR).render("# x")


@needs_weasy
def test_real_render_gives_a_short_pdf():
    out = R.WeasyRenderer(RESUME_DIR).render(RESUME_MD)
    assert out.pdf.startswith(b"%PDF") and 1 <= out.pages <= 3
    assert b"/Subtype /Image" in out.pdf  # the photo made it in


@needs_weasy
def test_hostile_resources_are_not_fetched(monkeypatch):
    asked: list[str] = []
    real = R.WeasyRenderer._fetcher

    def spy(self, photo):
        fetcher = real(self, photo)
        inner = fetcher.fetch

        def fetch(url, headers=None):
            asked.append(url)
            return inner(url, headers)

        fetcher.fetch = fetch
        return fetcher

    monkeypatch.setattr(R.WeasyRenderer, "_fetcher", spy)
    md = RESUME_MD + "\n![x](https://evil.example/p.png)\n![y](file:///etc/passwd)\n"
    out = R.WeasyRenderer(RESUME_DIR).render(md)  # pytest-socket would fail on any connection
    assert out.pdf.startswith(b"%PDF")
    assert set(asked) <= {R.PHOTO_URL, "https://evil.example/p.png"}
    assert not any(u.startswith("file:") for u in asked)


@needs_weasy
def test_render_letter_header_br_and_no_resources_fetched(monkeypatch):
    import socket

    def no_net(*a, **k):
        raise AssertionError("the letter renderer touched the network")

    monkeypatch.setattr(socket, "create_connection", no_net)
    md = (
        "Jordan Example, PhD\nBerlin, Germany · jordan@example.com\n\n"
        "Dear team,\n\n![x](https://evil.example.net/p.png)\n\n"
        '<link rel="stylesheet" href="https://evil.example.net/a.css"> <img src="https://evil.example.net/i.png">\n\n'
        "Best regards,<br>\nJordan Example\n"
    )
    html = R.letter_to_html(md)
    assert '<div class="name">Jordan Example, PhD</div>' in html
    assert 'class="contacts">Berlin, Germany' in html
    assert "<link" not in html and "&lt;link" in html and "&lt;img" in html
    assert "<br" in html and "&lt;br" not in html  # a hard break, not escaped text
    rendered = R.WeasyRenderer(RESUME_DIR).render_letter(md)
    assert rendered.pdf.startswith(b"%PDF") and rendered.pages == 1
    assert b"/Subtype /Image" not in rendered.pdf and b"evil.example.net" not in rendered.pdf


def test_letter_html_needs_no_weasyprint():
    html = R.letter_to_html("# Name\nContacts\n\nBody<br>\nline")
    assert '<div class="name">Name</div>' in html and "Body" in html
