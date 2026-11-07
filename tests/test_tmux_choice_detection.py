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


def _claude_permission_frame(timer: str, command: str) -> str:
    # Кадр без пустой строки над меню: так Claude рисует разрешение на команду.
    return (
        "… +22 lines (ctrl+o to expand)\n"
        "tool: Bash (cd /work && ls)\n"
        "ok tests\n"
        f"Running…  ({timer} · timeout 10m)\n"
        "(ctrl+b ctrl+b (twice) to run in background)\n"
        "Permission Required: Bash command · from the general-purpose agent\n"
        "> Run shell command\n"
        f"> {command}\n"
        "Do you want to proceed?\n"
        "❯ 1. Yes\n"
        "  2. No\n"
    )


def test_ticking_permission_timer_does_not_change_question_or_id() -> None:
    early = _claude_permission_frame("5s", "rm -f Tests/LacEditorTests/*")
    later = _claude_permission_frame("1m 27s", "rm -f Tests/LacEditorTests/*")

    early_question, early_options = SessionRunService._parse_cli_choice_question(early)
    later_question, later_options = SessionRunService._parse_cli_choice_question(later)

    assert early_options == ["1. Yes", "2. No"]
    assert later_options == early_options
    assert "timeout" not in early_question
    assert "rm -f Tests/LacEditorTests/*" in early_question
    assert early_question == later_question
    assert SessionRunService._tmux_choice_question_id("s45", early_question, early_options) == (
        SessionRunService._tmux_choice_question_id("s45", later_question, later_options)
    )


def test_different_permission_commands_keep_different_question_ids() -> None:
    first_question, first_options = SessionRunService._parse_cli_choice_question(
        _claude_permission_frame("5s", "rm -f Tests/LacEditorTests/*")
    )
    second_question, second_options = SessionRunService._parse_cli_choice_question(
        _claude_permission_frame("6s", "rm -rf /tmp/other")
    )

    assert first_question != second_question
    assert SessionRunService._tmux_choice_question_id("s45", first_question, first_options) != (
        SessionRunService._tmux_choice_question_id("s45", second_question, second_options)
    )


def _claude_ask_user_frame() -> str:
    # Вопрос Claude без курсора: длинный пункт перенесён, внизу подсказка про номер.
    return (
        "tool: Bash (sed -n 1,40p src/store.py)\n"
        "        self.directory = store.generation_dir(project.uid, record.uid)\n"
        "Question: Очистка\n"
        "> Как очистить тестовый проект?\n"
        "> История на 70 МБ, плюс настройки проекта.\n"
        "1. Удалить историю (Рекомендую) — Сохранить input.txt рядом с проектом,\n"
        " удалить папку истории целиком.\n"
        "2. Удалить и ресурсы проекта — То же, плюс фон и заставку.\n"
        "3. Только медиа истории — Удалить media и workspace.\n"
        "4. Other\n"
        "5. Chat about this\n"
        "Select with numbers [1-5]. Then Enter to submit or Escape to cancel:\n"
    )


def _claude_question_list_frame() -> str:
    # Несколько вопросов Claude: заголовок «Questions:», пункт перенесён по краю
    # экрана без отступа. Подсказка «Select with numbers» на этом кадре есть.
    return (
        "claude: Порты 80 и 443 на bots уже заняты Caddy.\n"
        "Questions: Прокси, Публикация, Старый код\n"
        "> Порты 80/443 на bots занимает Caddy. Как организовать удалённый доступ к\n"
        "> video_explainer?\n"
        "1. Angie вместо Caddy (Рекомендую) — Ставлю Angie и переношу маршруты. Будет короткий перерыв ezobot (секунды)\n"
        "при переключении портов; откат — запустить Caddy обратно.\n"
        "2. Оставить Caddy — Angie не ставим, добавляю video_explainer в Caddyfile.\n"
        "3. Angie за Caddy — Caddy остаётся на 80/443 и передаёт домен в Angie.\n"
        "4. Other\n"
        "5. Chat about this\n"
        "Select with numbers [1-5]. Then Enter to submit or Escape to cancel:\n"
    )


def test_claude_question_list_keeps_wrapped_options_and_round_trips() -> None:
    text = _claude_question_list_frame()

    assert SessionRunService._is_cli_choice_question(text) is True
    question, options = SessionRunService._parse_cli_choice_question(text)

    assert question.startswith("Questions: Прокси, Публикация, Старый код")
    assert "Порты 80 и 443 на bots уже заняты" not in question
    assert options[0].startswith("1. Angie вместо Caddy (Рекомендую)")
    assert options[0].endswith("запустить Caddy обратно.")
    assert "при переключении портов" in options[0]
    assert options[1:] == [
        "2. Оставить Caddy — Angie не ставим, добавляю video_explainer в Caddyfile.",
        "3. Angie за Caddy — Caddy остаётся на 80/443 и передаёт домен в Angie.",
        "4. Other",
        "5. Chat about this",
    ]
    # Монитор отдаёт в превью уже разобранный блок, без подсказки внизу.
    # Повторный разбор должен снова увидеть меню, иначе кнопки не уйдут.
    preview = "\n".join([question, f"❯ {options[0]}", *options[1:]])
    assert SessionRunService._is_cli_choice_question(preview) is True
    assert SessionRunService._parse_cli_choice_question(preview)[1] == options


def test_claude_ask_user_menu_without_cursor_is_a_choice() -> None:
    text = _claude_ask_user_frame()

    assert SessionRunService._is_cli_choice_question(text) is True
    question, options = SessionRunService._parse_cli_choice_question(text)

    assert question.startswith("Question: Очистка")
    assert "store.generation_dir" not in question
    assert options == [
        "1. Удалить историю (Рекомендую) — Сохранить input.txt рядом с проектом, удалить папку истории целиком.",
        "2. Удалить и ресурсы проекта — То же, плюс фон и заставку.",
        "3. Только медиа истории — Удалить media и workspace.",
        "4. Other",
        "5. Chat about this",
    ]


def test_question_id_is_stable_for_repeated_frames() -> None:
    first = SessionRunService._tmux_choice_question_id("s5", "Выбор?", ["1. Да", "2. Нет"])
    second = SessionRunService._tmux_choice_question_id("s5", "Выбор?", ["1. Да", "2. Нет"])

    assert first == second


def test_question_id_differs_for_different_questions() -> None:
    first = SessionRunService._tmux_choice_question_id("s5", "Выбор?", ["1. Да"])
    other = SessionRunService._tmux_choice_question_id("s5", "Другой выбор?", ["1. Да"])

    assert first != other
