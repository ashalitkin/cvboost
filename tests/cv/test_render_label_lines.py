from cvboost.cv.render import markdown_to_html


def test_core_skill_lines_become_separate_paragraphs():
    md = "## Core skills\n\n**Data:** dbt, BigQuery\n**Cloud:** GCP, AWS\n**AI:** Claude\n"
    html = markdown_to_html(md)
    assert html.count("<p><strong>") == 3


def test_bold_title_after_a_bullet_is_not_glued_to_the_list():
    md = "- Result: 5x faster.\n**(Data) Next case**\n- bullet\n"
    html = markdown_to_html(md)
    assert "<p><strong>(Data) Next case</strong></p>" in html


def test_inline_bold_inside_a_line_is_untouched():
    md = "Text with **bold** inside\nand a wrapped line.\n"
    assert markdown_to_html(md).count("<p>") == 1
