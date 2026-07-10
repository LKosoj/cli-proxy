from app.services.cli_backends.tmux_parser import (
    _DONE_INSTRUCTION,
    build_prompt_with_markers,
    done_marker,
    parse_tmux_delta,
    request_marker,
)


def test_parse_tmux_delta_strips_ansi_and_requires_matching_done_marker() -> None:
    request_id = "req-1"
    other_id = "req-2"
    delta = (
        f"\x1b[31m{request_marker(request_id)}\x1b[0m\n"
        "question echo\n"
        "answer\n"
        f"{done_marker(other_id)}\n"
    )

    parsed = parse_tmux_delta(delta, request_id)

    assert parsed.complete is False
    assert parsed.text == f"question echo\nanswer\n{done_marker(other_id)}"


def test_parse_tmux_delta_returns_text_before_done_marker() -> None:
    request_id = "req-1"
    delta = f"{request_marker(request_id)}\nanswer\n{done_marker(request_id)}\nold scrollback"

    parsed = parse_tmux_delta(delta, request_id)

    assert parsed.complete is True
    assert parsed.text == "answer"


def test_parse_tmux_delta_strips_cursor_control_inside_done_marker() -> None:
    request_id = "req-cursor"
    delta = f"{request_marker(request_id)}\nanswer\n<<<DO\x1b[4GNE:{request_id}>>>\n"

    parsed = parse_tmux_delta(delta, request_id)

    assert parsed.complete is True
    assert parsed.text == "answer"


def test_parse_tmux_delta_accepts_tmux_rendered_short_done_marker() -> None:
    request_id = "req-rendered"
    delta = f"{request_marker(request_id)}\nanswer\n <<DONE:{request_id}>>\nspinner text"

    parsed = parse_tmux_delta(delta, request_id)

    assert parsed.complete is True
    assert parsed.text == "answer"


def test_parse_tmux_delta_accepts_done_marker_with_repaint_tail() -> None:
    request_id = "req-tail"
    delta = f"{request_marker(request_id)}\nanswer\n <<DONE:{request_id}>> ▐▛███▜▌Claude Code\n"

    parsed = parse_tmux_delta(delta, request_id)

    assert parsed.complete is True
    assert parsed.text == "answer"


def test_parse_tmux_delta_drops_corrupted_echoed_done_marker_instruction() -> None:
    request_id = "req-echo-corrupt"
    delta = (
        f"{request_marker(request_id)}\n"
        "user prompt\n"
        f"{_DONE_INSTRUCTION}\n"
        f"<<<DONE:{request_id[1:]}>>>\n"
        "\n"
        "answer\n"
        f"<<DONE:{request_id}>>\n"
    )

    parsed = parse_tmux_delta(delta, request_id)

    assert parsed.complete is True
    assert parsed.text == "answer"


def test_parse_tmux_delta_drops_compacted_echoed_done_marker_instruction() -> None:
    request_id = "req-echo-compact"
    delta = (
        f"{request_marker(request_id)}\n"
        "user prompt\n"
        "ui-prefix Whenyouarecompletelyfinished,printthisexactmarkeron its own line: ui-suffix\n"
        f"{done_marker(request_id)}\n"
        "answer\n"
        f"<<DONE:{request_id}>>\n"
    )

    parsed = parse_tmux_delta(delta, request_id)

    assert parsed.complete is True
    assert parsed.text == "answer"


def test_parse_tmux_delta_finds_request_marker_with_tui_prefix_and_cursor_control() -> None:
    request_id = "req-tui-request"
    delta = (
        f"❯ <<<CLI_PROXY_\x1b[12GREQUEST:{request_id}>>>\n"
        "user prompt\n"
        f"{_DONE_INSTRUCTION}\n"
        f"{done_marker(request_id)}\n"
        "answer\n"
        f"<<DONE:{request_id}>>\n"
    )

    parsed = parse_tmux_delta(delta, request_id)

    assert parsed.complete is True
    assert parsed.text == "answer"


def test_parse_tmux_delta_drops_repainted_echo_blocks_before_answer() -> None:
    request_id = "req-repaint"
    echo_block = (
        f"❯ {request_marker(request_id)}\n"
        "user prompt\n"
        f"{_DONE_INSTRUCTION}\n"
        f"{done_marker(request_id)}\n"
        "\n"
    )
    delta = f"{echo_block}{echo_block}✶ Working...\nanswer\n<<DONE:{request_id}>>\n"

    parsed = parse_tmux_delta(delta, request_id)

    assert parsed.complete is True
    assert parsed.text == "✶ Working...\nanswer"


def test_parse_tmux_delta_extracts_final_claude_screen_reader_message() -> None:
    request_id = "req-screen-reader"
    delta = (
        build_prompt_with_markers("check follow-ups", request_id)
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
        + "$claude: "
        + done_marker(request_id)
    )

    parsed = parse_tmux_delta(delta, request_id, claude_screen_reader=True)

    assert parsed.complete is True
    assert parsed.text == (
        "Нет, не доделаны — и это ожидаемо.\n"
        "1. PromptBudgetBuilder — не сделан.\n"
        "2. quote_id — не сделан.\n"
        "Skills в этом ответе не использовались."
    )


def test_parse_tmux_delta_filters_intermediate_claude_screen_reader_events() -> None:
    request_id = "req-screen-reader-progress"
    delta = (
        build_prompt_with_markers("check follow-ups", request_id)
        + " $Nesting… don't ask on (shift+tab to cycle)"
        + " $Scampering… (11s · thinking with xhigh effort)"
    )

    status_only = parse_tmux_delta(delta, request_id, claude_screen_reader=True)

    assert status_only.complete is False
    assert status_only.text == ""

    delta += (
        " $claude: Проверю текущее состояние follow-up."
        " $Scampering… (18s · 424 tokens · thought for 6s)"
    )
    commentary = parse_tmux_delta(delta, request_id, claude_screen_reader=True)

    assert commentary.complete is False
    assert commentary.text == "Проверю текущее состояние follow-up."

    delta += (
        " $tool: Bash (grep -n follow-up docs/sdd.md) Waiting…"
        " $Running…"
        " 198: PromptBudgetBuilder — follow-up."
        " $(BScampering… (20s · 538 tokens)"
    )
    tool_running = parse_tmux_delta(delta, request_id, claude_screen_reader=True)

    assert tool_running.complete is False
    assert tool_running.text == "Проверю текущее состояние follow-up."


def test_parse_tmux_delta_filters_real_claude_screen_reader_preview_status() -> None:
    request_id = "req-screen-reader-real-preview"
    for status in ("Musing…", "Unfurling…"):
        delta = (
            build_prompt_with_markers("run sleep", request_id)
            + "\x1b[2K\x1b[Gclaude: Выполняю команду sleep 10 и жду её завершения.\r\n"
            + f"{status}   ( 5s  ·  97 tokens )\r\n"
            + "don't ask on (shift+tab to cycle)  ·  esc to interrupt\r\n"
            + "effort: xhigh · /effort\r\n"
            + "$\x1b[4G"
        )

        parsed = parse_tmux_delta(delta, request_id, claude_screen_reader=True)

        assert parsed.complete is False
        assert parsed.text == "Выполняю команду sleep 10 и жду её завершения."


def test_parse_tmux_delta_filters_claude_localized_progress_and_repaint_tail() -> None:
    request_id = "req-screen-reader-localized-progress"
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
        delta = (
            build_prompt_with_markers("continue", request_id)
            + f"$claude: {commentary}\n"
            + f"{ui_line}\n"
        )

        parsed = parse_tmux_delta(delta, request_id, claude_screen_reader=True)

        assert parsed.complete is False
        assert parsed.text == commentary


def test_parse_tmux_delta_filters_real_claude_bypass_footer_and_partial_done_marker() -> None:
    request_id = "req-screen-reader-bypass"
    delta = (
        build_prompt_with_markers("reply exactly", request_id)
        + "$claude: BYPASS-TMUX-OK\r\n"
        + "<<<DONE:req-screen\r\n"
        + "bypass permissions on (shift+tab to cycle)  ·  esc to interrupt\r\n"
        + "$\x1b[4G"
    )

    preview = parse_tmux_delta(delta, request_id, claude_screen_reader=True)

    assert preview.complete is False
    assert preview.text == "BYPASS-TMUX-OK"

    complete = parse_tmux_delta(
        delta + f"\r\n$claude: BYPASS-TMUX-OK\r\n<<DONE:{request_id}>>\r\n",
        request_id,
        claude_screen_reader=True,
    )

    assert complete.complete is True
    assert complete.text == "BYPASS-TMUX-OK"


def test_parse_tmux_delta_drops_corrupted_repaint_before_second_answer() -> None:
    request_id = "req-repeat-real"
    delta = (
        "●REAL_CLAUDE_TMUX_REPEAT_ONE\n"
        f"<<DONE:{request_id}>>\n\n"
        "✻Cooked for 8s\n\n"
        "Ответь одной строкой ровно так: REAL_CLAUDE_TMUX_REPEAT_TWO\n\n"
        "When you are completely finishd, print this exact marker on its own line:\n"
        f"<<DONE:{request_id}>>\n"
        "●REAL_CLAUDE_TMUX_REPEAT_TWO\n"
        f"<<DONE:{request_id}>>\n"
    )

    parsed = parse_tmux_delta(delta, request_id)

    assert parsed.complete is True
    assert parsed.text == "●REAL_CLAUDE_TMUX_REPEAT_TWO"


def test_parse_tmux_delta_keeps_answer_that_quotes_done_instruction() -> None:
    request_id = "req-quotes-instruction"
    delta = (
        f"{request_marker(request_id)}\n"
        "Print the guard instruction verbatim.\n\n"
        f"{_DONE_INSTRUCTION}\n"
        f"{done_marker(request_id)}\n"
        f"{_DONE_INSTRUCTION}\n"
        f"{done_marker(request_id)}\n"
    )

    parsed = parse_tmux_delta(delta, request_id)

    assert parsed.complete is True
    assert parsed.text == _DONE_INSTRUCTION


def test_parse_tmux_delta_ignores_echoed_done_marker_instruction() -> None:
    request_id = "req-echo"
    delta = build_prompt_with_markers("do work", request_id)

    echoed = parse_tmux_delta(delta, request_id)

    assert echoed.complete is False
    assert echoed.text == ""

    complete = parse_tmux_delta(f"{delta}\nanswer\n{done_marker(request_id)}\n", request_id)

    assert complete.complete is True
    assert complete.text == "answer"


def test_parse_tmux_delta_ignores_wrapped_qwen_echo_without_answer() -> None:
    request_id = "req-qwen-echo"
    delta = (
        f"> {request_marker(request_id)}\n"
        "  Ответь ровно одной строкой: OK_TMUX_QWEN\n\n"
        "  When you are completely finished, print this exact marker on its\n"
        "  own line:\n"
        f"  {done_marker(request_id)}\n"
        "  .   Инициализация...\n"
    )

    parsed = parse_tmux_delta(delta, request_id)

    assert parsed.complete is False
    assert parsed.text == ".   Инициализация..."


def test_parse_tmux_delta_ignores_single_line_envelope_echo() -> None:
    request_id = "req-single-line"
    delta = build_prompt_with_markers("do work", request_id, multiline=False)

    echoed = parse_tmux_delta(delta, request_id)

    assert echoed.complete is False
    assert echoed.text == ""

    complete = parse_tmux_delta(f"{delta}\nanswer\n{done_marker(request_id)}\n", request_id)

    assert complete.complete is True
    assert complete.text == "answer"


def test_parse_tmux_delta_ignores_terminal_wrapped_single_line_envelope_echo() -> None:
    request_id = "req-single-wrap"
    delta = (
        f"> {request_marker(request_id)} Reply with exactly this single line\n"
        "  and do not use tools: OK_TMUX When you are completely finished,\n"
        f"  print this exact marker on a separate final line: {done_marker(request_id)}\n"
        "  Working...\n"
        "  answer\n"
        f"  {done_marker(request_id)}\n"
    )

    parsed = parse_tmux_delta(delta, request_id)

    assert parsed.complete is True
    assert parsed.text == "Working...\n  answer"


def test_parse_tmux_delta_preserves_request_line_suffix_for_echo_detection() -> None:
    request_id = "req-suffix-wrap"
    delta = (
        f"> {request_marker(request_id)} Reply with exactly this single line and do not use tools: OK "
        "When you are completely finished, print this exact marker on a separate final\n"
        f"  line: {done_marker(request_id)}\n"
    )

    parsed = parse_tmux_delta(delta, request_id)

    assert parsed.complete is False
    assert parsed.text == ""


def test_parse_tmux_delta_handles_grok_flat_stream_with_quoted_marker() -> None:
    request_id = "reqgrokflat"
    delta = (
        f"│❯│<<<CLI_PROXY_REQUEST:{request_id}>>>Reply exactly OK "
        f"When you are completely finished, print this exact marker: {done_marker(request_id)}"
        "┃◆Thinking… The user wants OK and marker "
        f'"{done_marker(request_id)}" '
        f"OK_TMUX_GROK{done_marker(request_id)} Turn completed"
    )

    parsed = parse_tmux_delta(delta, request_id)

    assert parsed.complete is True
    assert "OK_TMUX_GROK" in parsed.text


def test_build_prompt_with_markers_contains_unique_request_and_done() -> None:
    prompt = build_prompt_with_markers("do work", "abc")

    assert request_marker("abc") in prompt
    assert done_marker("abc") in prompt
    assert "do work" in prompt


def test_build_prompt_with_markers_keeps_boundaries_when_tui_flattens_newlines() -> None:
    request_id = "req-flattened-prompt"

    prompt = build_prompt_with_markers("Исходная задача:\nпродолжи", request_id)

    assert "продолжи\n\n --- CLI-PROXY COMPLETION PROTOCOL --- \n" in prompt
    assert f"{_DONE_INSTRUCTION} \n{done_marker(request_id)}\n" in prompt

    flattened = prompt.replace("\n", "")
    assert "продолжи --- CLI-PROXY COMPLETION PROTOCOL --- When" in flattened
    assert f"own line: {done_marker(request_id)}" in flattened
