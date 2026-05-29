from utils import ansi_to_html


def test_ansi_to_html_does_not_escape_quotes_but_escapes_angles():
    src = '{"a": "b"} <x>'
    out = ansi_to_html(src)

    # Quotes should remain human-readable in the HTML payload.
    assert '"a": "b"' in out
    assert "&quot;" not in out
    assert "&amp;quot;" not in out

    # Angle brackets must still be escaped for safety.
    assert "&lt;x&gt;" in out
    assert "<x>" not in out


def test_ansi_to_html_does_not_double_escape_html_entities():
    # Users may paste already-escaped HTML snippets. We should not turn &gt; into &amp;gt;.
    src = "Приём видео или аудиофайлов &gt;10 МБ"
    out = ansi_to_html(src)

    assert "&gt;10" in out
    assert "&amp;gt;10" not in out
