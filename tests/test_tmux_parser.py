from app.services.cli_backends.tmux_parser import parse_tmux_delta


def test_parse_tmux_delta_extracts_final_claude_screen_reader_message() -> None:
    delta = (
        "check follow-ups"
        + " $Nesting… don't ask on (shift+tab to cycle)"
        + " $claude: Проверю состояние follow-up."
        + " $Scampering… (18s · thinking with xhigh effort)"
        + " $tool: Bash (grep -n follow-up docs/sdd.md) Waiting…"
        + " $Running…"
        + " 198: PromptBudgetBuilder — follow-up."
        + " $Scampering… (30s · 1.1k tokens)"
        + " $claude: Нет, не доделаны — и это ожидаемо.\n"
        + "1. PromptBudgetBuilder — не сделан.\n"
        + "2. quote_id — не сделан.\n"
        + "Skills в этом ответе не использовались.\n"
        + "$Baked for 1m 2s\n"
    )

    parsed = parse_tmux_delta(delta, claude_screen_reader=True)

    assert parsed == (
        "Нет, не доделаны — и это ожидаемо.\n"
        "1. PromptBudgetBuilder — не сделан.\n"
        "2. quote_id — не сделан.\n"
        "Skills в этом ответе не использовались."
    )


def test_parse_tmux_delta_filters_intermediate_claude_screen_reader_events() -> None:
    delta = (
        "check follow-ups"
        " $Nesting… don't ask on (shift+tab to cycle)"
        " $Scampering… (11s · thinking with xhigh effort)"
    )

    assert parse_tmux_delta(delta, claude_screen_reader=True) == ""

    delta += (
        " $claude: Проверю текущее состояние follow-up."
        " $Scampering… (18s · 424 tokens · thought for 6s)"
    )

    assert parse_tmux_delta(delta, claude_screen_reader=True) == "Проверю текущее состояние follow-up."

    delta += (
        " $tool: Bash (grep -n follow-up docs/sdd.md) Waiting…"
        " $Running…"
        " 198: PromptBudgetBuilder — follow-up."
        " $(BScampering… (20s · 538 tokens)"
    )

    assert parse_tmux_delta(delta, claude_screen_reader=True) == "Проверю текущее состояние follow-up."


def test_parse_tmux_delta_filters_real_claude_screen_reader_preview_status() -> None:
    for status in ("Musing…", "Unfurling…"):
        delta = (
            "run sleep"
            + "\x1b[2K\x1b[Gclaude: Выполняю команду sleep 10 и жду её завершения.\r\n"
            + f"{status}   ( 5s  ·  97 tokens )\r\n"
            + "don't ask on (shift+tab to cycle)  ·  esc to interrupt\r\n"
            + "effort: xhigh · /effort\r\n"
            + "$\x1b[4G"
        )

        parsed = parse_tmux_delta(delta, claude_screen_reader=True)

        assert parsed == "Выполняю команду sleep 10 и жду её завершения."


def test_parse_tmux_delta_filters_claude_localized_progress_and_repaint_tail() -> None:
    commentary = (
        "Монитор поставлен — жду события о завершении сьюта. "
        "Как только придёт итоговая строка pytest, дам финальный отчёт."
    )
    ui_lines = (
        "Строю PromptBudgetBuilder…   ( 1m 2s  ·  1.9k tokens )",
        "$Baked for 1m 2s · 1 shell, 1 monitor still running",
        "Cogitated for 12m 41s",
        "$(B(B",
    )

    for ui_line in ui_lines:
        delta = f"continue$claude: {commentary}\n{ui_line}\n"

        assert parse_tmux_delta(delta, claude_screen_reader=True) == commentary


def test_parse_tmux_delta_filters_real_claude_bypass_footer() -> None:
    delta = (
        "reply exactly"
        "$claude: BYPASS-TMUX-OK\r\n"
        "bypass permissions on (shift+tab to cycle)  ·  esc to interrupt\r\n"
        "$\x1b[4G"
    )

    assert parse_tmux_delta(delta, claude_screen_reader=True) == "BYPASS-TMUX-OK"


def _codex_tui_frame(body: str) -> str:
    """Кадр TUI codex: ответ, статус, поле ввода и футер с моделью."""
    return (
        f"{body}\n"
        "\n"
        "• Working (12s • esc to interrupt)\n"
        "\n"
        "› Write tests for @filename\n"
        "\n"
        "  gpt-5.6-sol xhigh · /tmp/work"
    )


def test_parse_tmux_delta_strips_codex_tui_chrome() -> None:
    parsed = parse_tmux_delta(_codex_tui_frame("• Полезный ответ модели."), tui_chrome=True)

    assert parsed == "• Полезный ответ модели."
    assert "esc to interrupt" not in parsed
    assert "gpt-5.6-sol" not in parsed
    assert "Write tests for" not in parsed


def test_parse_tmux_delta_keeps_codex_chrome_when_flag_disabled() -> None:
    parsed = parse_tmux_delta(_codex_tui_frame("• Ответ."))

    assert "esc to interrupt" in parsed


def test_parse_tmux_delta_renders_cursor_addressed_codex_status() -> None:
    # Реальный поток codex перерисовывает статус через абсолютное
    # позиционирование: раньше это давало "WWoorrkkiinngg".
    delta = "do work\r\n\x1b[3;1HWorking\x1b[3;1HWorking\r\n"

    parsed = parse_tmux_delta(delta)

    assert "WWoorrkkiinngg" not in parsed
    assert "Working" in parsed


def test_tui_chrome_covers_cli_without_linear_output_mode() -> None:
    """Фильтр интерфейса нужен всем TUI без режима линейного вывода.

    У claude такой режим есть (--ax-screen-reader), поэтому его экран разбирает
    отдельная ветка и вычищать интерфейс не требуется.
    """
    from types import SimpleNamespace

    from app.services.cli_backends.tmux_backend import TmuxExecutionBackend

    def _session(name: str) -> SimpleNamespace:
        return SimpleNamespace(tool=SimpleNamespace(name=name, interactive_cmd=[name]))

    for name in ("codex", "qwen", "gemini", "grok"):
        assert TmuxExecutionBackend._uses_tui_chrome(_session(name)) is True, name
    assert TmuxExecutionBackend._uses_tui_chrome(_session("claude")) is False


def test_find_qwen_transcript_path_tries_both_project_key_forms(tmp_path, monkeypatch) -> None:
    """Имя каталога с журналом qwen выводится из пути двумя разными способами.

    Первый вариант заменяет на дефис только разделители каталогов, второй — любой
    не-буквенно-цифровой символ. Из-за подчёркивания в `git_projects` они дают
    разные имена, поэтому проверять надо оба.
    """
    from types import SimpleNamespace

    from app.services.cli_backends import tmux_backend

    monkeypatch.setattr(tmux_backend, "QWEN_CHAT_BASE_DIR", tmp_path)
    workdir = "/nonexistent-root/git_projects/LLMApiGateway"

    # Каталога с "сырым" ключом (-nonexistent-root-git_projects-LLMApiGateway) нет,
    # журнал лежит только под вторым вариантом ключа.
    chat_dir = tmp_path / "-nonexistent-root-git-projects-LLMApiGateway" / "chats"
    chat_dir.mkdir(parents=True)
    transcript = chat_dir / "session.jsonl"
    transcript.write_text('{"uuid":"e1","sessionId":"s1","type":"user"}\n', encoding="utf-8")

    session = SimpleNamespace(workdir=workdir)

    found = tmux_backend.TmuxExecutionBackend._find_qwen_transcript_path(session)

    assert found == str(transcript)
