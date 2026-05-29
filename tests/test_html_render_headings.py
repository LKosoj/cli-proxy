from utils import ansi_to_html


def test_markdown_headings_render_as_html_tags_not_text():
    src = "# Title\n\n## Problems\n\n### Step 10"
    out = ansi_to_html(src)

    assert "<h1>" in out
    assert "<h2>" in out
    assert "<h3>" in out

    # Regression guard: headings must not be escaped into visible text.
    assert "&lt;h1&gt;" not in out
    assert "&lt;h2&gt;" not in out
    assert "&lt;h3&gt;" not in out
