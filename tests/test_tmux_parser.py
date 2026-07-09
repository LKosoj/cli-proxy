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
