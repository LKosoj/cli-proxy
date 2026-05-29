from modes.sdk.runtime.ask_user_schema import (
    apply_ask_schema,
    validate_ask_payload,
)


def test_validate_ask_payload_detects_multi_aspect_question_and_placeholders() -> None:
    issues = validate_ask_payload(
        "Уточните:\n1. Сроки\n2. Ограничения\n3. Бюджет",
        ["a", "b", "c", "d"],
    )
    assert "multi_aspect_question" in issues
    assert "placeholder_options" in issues


def test_apply_ask_schema_drops_placeholder_options() -> None:
    question, options, issues = apply_ask_schema("Какой вариант нужен?", ["a", "b"])
    assert question == "Какой вариант нужен?"
    assert options == []
    assert "placeholder_options" in issues


def test_apply_ask_schema_normalizes_and_deduplicates_options() -> None:
    question, options, issues = apply_ask_schema("Какой вариант нужен?", [" Web ", "web", "Mobile"])
    assert question == "Какой вариант нужен?"
    assert options == ["Web", "Mobile"]
    assert issues == []
