from sessions.session_run_service import SessionRunService


def test_working_status_is_not_a_choice_question() -> None:
    # Индикатор работы codex приходил каждую секунду и распознавался как вопрос,
    # из-за чего в чат сыпались новые сообщения.
    text = "• Working (12s • esc to interrupt)\n\n› Write tests for @filename"

    assert SessionRunService._is_cli_choice_question(text) is False


def test_footer_hint_is_not_a_choice_question() -> None:
    assert SessionRunService._is_cli_choice_question("bypass permissions on (shift+tab to cycle)") is False


def test_real_choice_question_is_still_detected() -> None:
    text = "Do you trust the files in this folder?\n  1. Yes, proceed\n  2. No, exit\nEnter selection [1-2]:"

    assert SessionRunService._is_cli_choice_question(text) is True


def test_numbered_options_are_parsed() -> None:
    text = "Выберите вариант:\n1. Первый\n2) Второй"

    question, options = SessionRunService._parse_cli_choice_question(text)

    assert question == "Выберите вариант:"
    assert options == ["1. Первый", "2. Второй"]


def test_unrecognized_options_do_not_produce_placeholders() -> None:
    question, options = SessionRunService._parse_cli_choice_question("Enter selection [1-2]:")

    assert options == []
    assert "Option 1" not in question


def test_question_id_is_stable_for_repeated_frames() -> None:
    first = SessionRunService._tmux_choice_question_id("s5", "Выбор?", ["1. Да", "2. Нет"])
    second = SessionRunService._tmux_choice_question_id("s5", "Выбор?", ["1. Да", "2. Нет"])

    assert first == second


def test_question_id_differs_for_different_questions() -> None:
    first = SessionRunService._tmux_choice_question_id("s5", "Выбор?", ["1. Да"])
    other = SessionRunService._tmux_choice_question_id("s5", "Другой выбор?", ["1. Да"])

    assert first != other
