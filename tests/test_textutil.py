from __future__ import annotations

from cvboost.textutil import clean_text, html_to_text


def test_html_to_text_no_glued_words_and_blocks():
    assert html_to_text("<p>Hello <b>wor</b>ld</p><ul><li>a</li><li>b</li></ul>") == (
        "Hello world\na\nb"
    )


def test_html_to_text_drops_script_and_style_and_decodes_entities():
    html = (
        "<style>p{color:red}</style><p>Tom &amp; Jerry &lt;3 &eacute;</p><script>alert(1)</script>"
    )
    out = html_to_text(html)
    assert out == "Tom & Jerry <3 é"
    assert "alert" not in out and "color" not in out


def test_clean_text_keeps_newline_and_tab():
    assert clean_text("a\tb\nc\x00​d‮") == "a\tb\ncd"
    assert clean_text(None) == ""
