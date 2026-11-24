"""resume.md -> HTML -> PDF (WeasyPrint). The markdown comes from an LLM: it is never trusted.

- markdown-it with html=False: raw HTML in the text is escaped, not passed through.
- attrs plugin limited to `class`, so `{onclick=..}` / `{style=..}` are dropped.
- WeasyPrint gets a url fetcher that serves exactly one resource, the owner's photo; every other
  URL (file://, http(s)://, data:, ..) is refused: no local file reads, no network.
- weasyprint is imported lazily: it needs system libraries (pango); without them
  `RendererUnavailable` is raised and the kit is saved without a PDF.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from markdown_it import MarkdownIt
from mdit_py_plugins.attrs import attrs_plugin

from cvboost.cv.profile import CSS_NAME, PHOTO_NAME, RESUME_DIR

RENDER_VERSION = 1  # part of the prompt version: a layout change is a new version
BASE_URL = "https://cv.invalid/"  # never fetched: the fetcher only knows the photo
PHOTO_URL = BASE_URL + PHOTO_NAME

# WeasyPrint has no `:has()`: float the photo itself. Liberation Sans is metric-compatible with
# Arial (installed in the image); the original stack starts with Mac-only fonts.
CSS_OVERRIDE = """
body{font-family:"Liberation Sans",Arial,Helvetica,sans-serif}
p:has(> img.photo){float:none}
img.photo{float:right;margin:0 0 0 14px}
"""


# The cover letter (#32): A4, a name and contacts header with a rule, no photo, no resume styling.
LETTER_CSS = """
@page{size:A4;margin:22mm 22mm 20mm 22mm}
body{font-family:"Liberation Sans","Helvetica Neue",Helvetica,Arial,sans-serif;font-size:10.5pt;
line-height:1.5;color:#222}
.head{border-bottom:1px solid #ccc;padding-bottom:6pt;margin-bottom:18pt}
.name{font-size:15pt;font-weight:600}
.contacts{font-size:9.5pt;color:#555;margin-top:2pt}
p{margin:0 0 9pt}
ul{margin:0 0 9pt;padding-left:16pt}
li{margin-bottom:4pt}
a{color:#222;text-decoration:none}
"""
_BR = re.compile(r"<br\s*/?>", re.IGNORECASE)


class RendererUnavailable(RuntimeError):
    """weasyprint or its system libraries are missing."""


class RenderError(RuntimeError):
    pass


@dataclass(frozen=True)
class Rendered:
    pdf: bytes
    pages: int


class Renderer(Protocol):
    def render(self, resume_md: str) -> Rendered: ...

    def render_letter(self, letter_md: str) -> Rendered: ...


def _title() -> str:
    """The document title: the profile's name (profile.yaml), escaped; neutral without a profile."""
    from cvboost.llm.prompts import registry  # late: the registry imports the prompt modules

    ps = registry.loaded()
    return html.escape(ps.profile.name if ps else "Resume", quote=True)


def _split_label_lines(resume_md: str) -> str:
    """Core skills come as consecutive "**Label:** ..." lines with single newlines; commonmark
    would merge them into one paragraph. Each such line becomes its own paragraph."""
    out: list[str] = []
    for line in resume_md.split("\n"):
        if line.startswith("**") and out and out[-1].strip():
            out.append("")
        out.append(line)
    return "\n".join(out)


def markdown_to_html(resume_md: str) -> str:
    md = MarkdownIt("commonmark", {"html": False, "linkify": False}).use(
        attrs_plugin, allowed=["class"]
    )
    body = md.render(_split_label_lines(resume_md))
    return (
        '<!doctype html><html><head><meta charset="utf-8"><title>' + _title() + "</title>"
        f"</head><body>{body}</body></html>"
    )


def letter_to_html(letter_md: str) -> str:
    """cover_letter.md -> HTML: the first two lines are the header (name, contacts), the rest is
    the body; a single newline is a line break ("Best regards," / the name); raw HTML is escaped."""
    text = _BR.sub("", letter_md.replace("\r\n", "\n")).strip("\n")
    lines = text.split("\n")
    head = [ln.lstrip("# ").strip() for ln in lines[:2]]
    while len(head) < 2:
        head.append("")
    md = MarkdownIt("commonmark", {"html": False, "linkify": False, "breaks": True})
    body = md.render("\n".join(lines[2:]).strip("\n"))
    return (
        '<!doctype html><html><head><meta charset="utf-8"><title>' + _title() + "</title></head>"
        f'<body><div class="head"><div class="name">{md.renderInline(head[0])}</div>'
        f'<div class="contacts">{md.renderInline(head[1])}</div></div>{body}</body></html>'
    )


class WeasyRenderer:
    def __init__(self, resume_dir: Path = RESUME_DIR):
        self.resume_dir = resume_dir

    def _fetcher(self, photo: bytes):
        """A URLFetcher that serves the photo and refuses everything else (no network, no files)."""
        from weasyprint.urls import URLFetcher, URLFetcherResponse

        class PhotoOnly(URLFetcher):
            def fetch(self, url, headers=None):
                if url == PHOTO_URL:
                    return URLFetcherResponse(
                        url, body=photo, headers={"Content-Type": "image/jpeg"}
                    )
                raise ValueError("resource not allowed")

        return PhotoOnly(allowed_protocols=("https",), allow_redirects=False)

    def render_letter(self, letter_md: str) -> Rendered:
        try:
            from weasyprint import CSS, HTML
            from weasyprint.urls import URLFetcher
        except (ImportError, OSError) as exc:
            raise RendererUnavailable(str(exc)[:200]) from None

        class Nothing(URLFetcher):
            def fetch(self, url, headers=None):
                raise ValueError("resource not allowed")

        fetcher = Nothing(allowed_protocols=("https",), allow_redirects=False)
        try:
            doc = HTML(
                string=letter_to_html(letter_md), base_url=BASE_URL, url_fetcher=fetcher
            ).render(stylesheets=[CSS(string=LETTER_CSS, url_fetcher=fetcher)])
            return Rendered(pdf=doc.write_pdf(), pages=len(doc.pages))
        except Exception as exc:  # noqa: BLE001 - any WeasyPrint failure is a failed PDF
            raise RenderError(f"{type(exc).__name__}: {str(exc)[:200]}") from None

    def render(self, resume_md: str) -> Rendered:
        try:
            from weasyprint import CSS, HTML
        except (ImportError, OSError) as exc:  # missing pango / gobject shows up as OSError
            raise RendererUnavailable(str(exc)[:200]) from None
        try:
            css_text = (self.resume_dir / CSS_NAME).read_text(encoding="utf-8") + CSS_OVERRIDE
            photo = (self.resume_dir / PHOTO_NAME).read_bytes()
        except OSError as exc:
            raise RenderError(f"resume assets unreadable: {exc.strerror}") from None
        fetcher = self._fetcher(photo)
        try:
            doc = HTML(
                string=markdown_to_html(resume_md), base_url=BASE_URL, url_fetcher=fetcher
            ).render(stylesheets=[CSS(string=css_text, url_fetcher=fetcher)])
            return Rendered(pdf=doc.write_pdf(), pages=len(doc.pages))
        except Exception as exc:  # noqa: BLE001 - any WeasyPrint failure is a failed PDF
            raise RenderError(f"{type(exc).__name__}: {str(exc)[:200]}") from None
