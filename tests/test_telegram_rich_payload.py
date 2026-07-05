from tg.rich import (
    RICH_MARKDOWN_CHAR_LIMIT,
    build_input_rich_message,
    is_rich_markdown_eligible,
    rich_markdown_chars,
)


def test_build_input_rich_message_preserves_raw_markdown():
    markdown = r"**bold** _italic_ [link](https://example.com?a=1&b=2) \*raw\*"

    payload = build_input_rich_message(markdown)

    assert payload == {"markdown": markdown}


def test_build_input_rich_message_includes_optional_fields():
    payload = build_input_rich_message(
        "# title",
        skip_entity_detection=True,
        is_rtl=False,
    )

    assert payload == {
        "markdown": "# title",
        "skip_entity_detection": True,
        "is_rtl": False,
    }


def test_rich_markdown_eligibility_uses_character_limit():
    assert rich_markdown_chars("я") == 1
    assert is_rich_markdown_eligible("x" * RICH_MARKDOWN_CHAR_LIMIT)
    assert not is_rich_markdown_eligible("x" * (RICH_MARKDOWN_CHAR_LIMIT + 1))

    assert is_rich_markdown_eligible("🙂" * RICH_MARKDOWN_CHAR_LIMIT)
    assert not is_rich_markdown_eligible(("🙂" * RICH_MARKDOWN_CHAR_LIMIT) + "x")
