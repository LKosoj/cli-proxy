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


def test_command_output_with_timestamps_is_not_a_choice_question() -> None:
    # Экран codex с выводом `find -printf '%T@ %p\n'`: строки начинаются с числа и
    # точки, и старая эвристика делала из них меню с кнопками.
    text = (
        "• Ran ps -eo pid,ppid,etimes,cmd | rg 'pytest' | tail -n 8; find /tmp -name 'host_bwrap'?\n"
        "  1754161234.5678900 /tmp/pytest-of-root/pytest-242/host_bwrap\n"
        "  1754161200.1234500 /tmp/pytest-of-root/pytest-241/host_bwrap\n"
        "  1754161100.7654300 /tmp/pytest-of-root/pytest-240/host_bwrap\n"
        "+122 lines (ctrl + t to view transcript)"
    )

    assert SessionRunService._is_cli_choice_question(text) is False
    assert SessionRunService._parse_cli_choice_question(text)[1] == []


def test_agent_numbered_list_without_menu_cursor_is_not_a_choice_question() -> None:
    # Агент печатает нумерованные списки в обычном ответе — это не меню TUI.
    text = "Что делать дальше?\n1. Починить тест\n2. Удалить кейс"

    assert SessionRunService._is_cli_choice_question(text) is False


def test_quoted_numbered_questions_are_not_a_menu() -> None:
    text = (
        "Пример упражнения:\n"
        "> 1. Какие варианты вы рассматриваете?\n"
        "> 2. Что для вас важнее всего?\n"
        "> 3. Какой шаг поможет проверить вариант?"
    )

    assert SessionRunService._is_cli_choice_question(text) is False
    assert SessionRunService._parse_cli_choice_question(text)[1] == []


def test_options_do_not_merge_across_separate_lists() -> None:
    text = "План:\n1. Первый пункт\n2. Второй пункт\n\nДругой список:\n3. Третий пункт"

    assert SessionRunService._parse_cli_choice_question(text)[1] == ["1. Первый пункт", "2. Второй пункт"]


def test_menu_after_agent_list_uses_its_own_question_and_options() -> None:
    text = "План:\n1. Проверить\n2. Исправить\n\nПродолжить?\n  1. Да\n❯ 2. Нет\n\n3. Лишний пункт"

    assert SessionRunService._is_cli_choice_question(text) is True
    assert SessionRunService._parse_cli_choice_question(text) == ("Продолжить?", ["1. Да", "2. Нет"])


def test_checkbox_in_prose_does_not_turn_a_numbered_list_into_a_menu() -> None:
    text = "Символ ☐ означает флажок.\n\nПлан:\n1. Проверить\n2. Исправить"

    assert SessionRunService._is_cli_choice_question(text) is False


def test_numbered_checkbox_menu_is_detected() -> None:
    text = "Выберите варианты:\n☐ 1. Первый\n☐ 2. Второй"

    assert SessionRunService._is_cli_choice_question(text) is True
    assert SessionRunService._parse_cli_choice_question(text) == (
        "Выберите варианты:", ["1. Первый", "2. Второй"],
    )


def test_tui_menu_with_cursor_is_detected() -> None:
    text = "Do you want to proceed?\n❯ 1. Yes\n  2. No, tell Claude what to do differently"

    assert SessionRunService._is_cli_choice_question(text) is True
    question, options = SessionRunService._parse_cli_choice_question(text)
    assert question == "Do you want to proceed?"
    assert options == ["1. Yes", "2. No, tell Claude what to do differently"]


def test_question_keeps_only_lines_next_to_options() -> None:
    # Раньше вопросом становился весь буфер экрана над первым вариантом.
    noise = "\n".join(f"log line {i}" for i in range(40))
    text = f"{noise}\n\nВыберите вариант:\n❯ 1. Первый\n  2. Второй"

    question, options = SessionRunService._parse_cli_choice_question(text)

    assert question == "Выберите вариант:"
    assert options == ["1. Первый", "2. Второй"]


def test_question_without_blank_separator_is_capped() -> None:
    noise = "\n".join(f"log line {i}" for i in range(40))
    text = f"{noise}\nВыберите вариант:\n❯ 1. Первый\n  2. Второй"

    question, _ = SessionRunService._parse_cli_choice_question(text)

    assert question.splitlines()[-1] == "Выберите вариант:"
    assert len(question.splitlines()) <= 12


def test_out_of_order_numbers_are_not_options() -> None:
    text = "Отчёт:\n3. третий пункт\n7. седьмой пункт"

    assert SessionRunService._parse_cli_choice_question(text)[1] == []


def test_question_id_is_stable_for_repeated_frames() -> None:
    first = SessionRunService._tmux_choice_question_id("s5", "Выбор?", ["1. Да", "2. Нет"])
    second = SessionRunService._tmux_choice_question_id("s5", "Выбор?", ["1. Да", "2. Нет"])

    assert first == second


def test_question_id_differs_for_different_questions() -> None:
    first = SessionRunService._tmux_choice_question_id("s5", "Выбор?", ["1. Да"])
    other = SessionRunService._tmux_choice_question_id("s5", "Другой выбор?", ["1. Да"])

    assert first != other
