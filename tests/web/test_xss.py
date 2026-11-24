from __future__ import annotations

import re

from cvboost.web.security import safe_url, sanitize_html
from tests.web.conftest import Ids


def test_sanitize_html_unit():
    out = str(
        sanitize_html('<a href="http://e.com" onclick="x()" hx-get="/s">a</a><script>1</script>')
    )
    assert 'rel="noopener noreferrer nofollow"' in out and "onclick" not in out
    assert "hx-get" not in out and "<script" not in out
    assert "href" not in str(sanitize_html('<a href="/sources">r</a>'))
    assert "<img" not in str(sanitize_html('<img src="http://e.com/x.png">'))


def test_safe_url():
    assert safe_url("https://example.com/a") == "https://example.com/a"
    for bad in (
        "javascript:alert(1)",
        "data:text/html,x",
        "//evil.example",
        "/x",
        "",
        None,
        "http://a b",
    ):
        assert safe_url(bad) is None


def _active_markup(html: str) -> list[str]:
    """Elements/attributes that could execute or drive HTMX, found by really parsing the page."""
    from selectolax.lexbor import LexborHTMLParser

    found = []
    for node in LexborHTMLParser(html).css("*"):
        if node.tag == "script" and "src" not in node.attributes:
            found.append("inline script")
        for name, value in node.attributes.items():
            if (
                name.startswith(("on", "hx-"))
                and not (node.tag == "body" and name == "hx-headers")
                and not (
                    node.tag in ("form",)
                    and name
                    in ("hx-post", "hx-get", "hx-target", "hx-swap", "hx-push-url", "hx-trigger")
                )
            ):
                found.append(f"{node.tag}[{name}]")
            if name in ("href", "src", "action") and (value or "").strip().lower().startswith(
                ("javascript:", "data:")
            ):
                found.append(f"{node.tag}[{name}={value}]")
    return found


def test_pages_have_no_active_markup(client, seeded: Ids):
    for path in (
        f"/jobs/{seeded.xss}",
        "/inbox",
        "/sources",
        "/usage",
        "/inbox?decision=all&eligible=any",
    ):
        text = client.get(path).text
        assert _active_markup(text) == [], path
        assert "<script>alert" not in text and "<img" not in text
    job = client.get(f"/jobs/{seeded.xss}").text
    assert "&lt;script&gt;t&lt;/script&gt;" in job
    assert "kept paragraph" in job
    assert "&lt;img src=x onerror=alert(1)&gt;" in client.get("/sources").text
    assert not re.search(r'href="javascript', job, re.I)
    # the injected hx-* on the sanitised description are gone, our own hx-* are on forms only
    desc = job.split('class="desc"')[1].split("</div>")[0]
    assert "hx-" not in desc and "<script" not in desc


# --- #70b: the new English chrome must not change escaping of the stored data ---


def test_script_in_summary_and_fit_notes_is_escaped(client, seeded: Ids, settings):
    from tests.web.conftest import db, llm_verdict, verdict

    s = db(settings)
    llm_verdict(s, seeded.b, verdict(summary_ru="<script>alert(1)</script> sum", fit=50))
    d = settings.kits_dir / str(seeded.b)
    d.mkdir(parents=True)
    (d / "fit_notes.md").write_text(
        "# Job analysis\n\nRecommendation: **skip**\n\n<script>alert(2)</script>\n",
        encoding="utf-8",
    )
    (d / "resume.md").write_text("# CV")
    for page in (client.get(f"/jobs/{seeded.b}").text, client.get("/inbox?decision=all").text):
        assert "<script>alert(" not in page
    job = client.get(f"/jobs/{seeded.b}").text
    assert "&lt;script&gt;alert(1)&lt;/script&gt; sum" in job
