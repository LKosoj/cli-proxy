from app.services.cli_backends.tmux_parser import (
    TmuxDeltaReader,
    done_marker,
    parse_tmux_delta,
    request_marker,
)


def _tui_stream(request_id: str, *, done: bool = True) -> str:
    """Поток с перерисовкой экрана: курсор ходит по позициям, строки затираются."""

    parts = [
        f"\x1b[2J\x1b[H{request_marker(request_id)}\r\n",
        "\x1b[1;1HWorking\x1b[K\r\n",
        "\x1b[2;1Hthinking...\x1b[K",
        "\x1b[2;1H\x1b[2Kanswer line one\r\n",
        "answer line two\r\n",
        "\x1b[3;1H\x1b[Kanswer line two (final)\r\n",
    ]
    if done:
        parts.append(f"{done_marker(request_id)}\r\n")
    return "".join(parts)


def _feed_in_chunks(reader: TmuxDeltaReader, text: str, size: int) -> None:
    for start in range(0, len(text), size):
        reader.feed(text[start:start + size])


def test_incremental_feed_matches_single_pass() -> None:
    request_id = "req-inc"
    stream = _tui_stream(request_id)
    expected = parse_tmux_delta(stream, request_id)

    for size in (1, 3, 7, 64, 4096):
        reader = TmuxDeltaReader(request_id)
        _feed_in_chunks(reader, stream, size)
        result = reader.parse()
        assert result.text == expected.text, f"размер чанка {size}"
        assert result.complete == expected.complete, f"размер чанка {size}"


def test_incremental_feed_matches_single_pass_for_claude_screen_reader() -> None:
    request_id = "req-claude"
    stream = (
        f"{request_marker(request_id)}\r\n"
        "\x1b[1;1Hclaude: первый ответ\x1b[K\r\n"
        "tool: Read(file.py)\r\n"
        "\x1b[2;1H\x1b[2Kclaude: итоговый ответ\r\n"
        f"{done_marker(request_id)}\r\n"
    )
    expected = parse_tmux_delta(stream, request_id, claude_screen_reader=True)

    reader = TmuxDeltaReader(request_id)
    _feed_in_chunks(reader, stream, 5)
    result = reader.parse(claude_screen_reader=True)

    assert result.text == expected.text
    assert result.complete == expected.complete


def test_escape_split_across_chunks_is_not_lost() -> None:
    request_id = "req-split"
    # Разрез приходится на середину CSI-последовательности очистки строки.
    head = "первая строка\r\nвторая строка\x1b[1;1H\x1b[2K"
    tail = "перерисованная первая\r\n"
    expected = parse_tmux_delta(head + tail, request_id)

    reader = TmuxDeltaReader(request_id)
    reader.feed("первая строка\r\nвторая строка\x1b[1;1H\x1b")
    reader.feed("[2K" + tail)

    assert reader.parse().text == expected.text
    assert "перерисованная первая" in reader.parse().text


def test_done_marker_detected_when_split_across_chunks() -> None:
    request_id = "req-done"
    stream = _tui_stream(request_id)
    reader = TmuxDeltaReader(request_id)
    _feed_in_chunks(reader, stream, 11)

    assert reader.parse().complete is True


def test_reader_is_not_complete_without_done_marker() -> None:
    request_id = "req-open"
    reader = TmuxDeltaReader(request_id)
    _feed_in_chunks(reader, _tui_stream(request_id, done=False), 17)

    result = reader.parse()
    assert result.complete is False
    assert "answer line two (final)" in result.text


def test_parse_result_is_cached_until_new_data_arrives() -> None:
    request_id = "req-cache"
    reader = TmuxDeltaReader(request_id)
    reader.feed(f"{request_marker(request_id)}\r\nтекст\r\n")

    first = reader.parse()
    assert reader.parse() is first

    reader.feed("ещё текст\r\n")
    assert reader.parse() is not first


def test_fallback_tail_is_bounded_for_huge_single_feed() -> None:
    from app.services.cli_backends.tmux_parser import _FALLBACK_TAIL_LIMIT

    request_id = "req-huge"
    reader = TmuxDeltaReader(request_id)
    reader.feed("x" * (_FALLBACK_TAIL_LIMIT * 2) + "\n")

    assert reader._fallback_len <= _FALLBACK_TAIL_LIMIT
