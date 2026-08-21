from app.services.cli_backends.tmux_parser import TmuxDeltaReader, parse_tmux_delta


def _tui_stream() -> str:
    """Поток с перерисовкой экрана: курсор ходит по позициям, строки затираются."""

    return "".join(
        [
            "\x1b[2J\x1b[Hdo work\r\n",
            "\x1b[1;1HWorking\x1b[K\r\n",
            "\x1b[2;1Hthinking...\x1b[K",
            "\x1b[2;1H\x1b[2Kanswer line one\r\n",
            "answer line two\r\n",
            "\x1b[3;1H\x1b[Kanswer line two (final)\r\n",
        ]
    )


def _feed_in_chunks(reader: TmuxDeltaReader, text: str, size: int) -> None:
    for start in range(0, len(text), size):
        reader.feed(text[start:start + size])


def test_incremental_feed_matches_single_pass() -> None:
    stream = _tui_stream()
    expected = parse_tmux_delta(stream)

    for size in (1, 3, 7, 64, 4096):
        reader = TmuxDeltaReader()
        _feed_in_chunks(reader, stream, size)
        assert reader.parse() == expected, f"размер чанка {size}"


def test_incremental_feed_matches_single_pass_for_claude_screen_reader() -> None:
    stream = (
        "do work\r\n"
        "\x1b[1;1Hclaude: первый ответ\x1b[K\r\n"
        "tool: Read(file.py)\r\n"
        "\x1b[2;1H\x1b[2Kclaude: итоговый ответ\r\n"
    )
    expected = parse_tmux_delta(stream, claude_screen_reader=True)

    reader = TmuxDeltaReader()
    _feed_in_chunks(reader, stream, 5)

    assert reader.parse(claude_screen_reader=True) == expected
    assert expected == "итоговый ответ"


def test_escape_split_across_chunks_is_not_lost() -> None:
    # Разрез приходится на середину CSI-последовательности очистки строки.
    head = "первая строка\r\nвторая строка\x1b[1;1H\x1b[2K"
    tail = "перерисованная первая\r\n"
    expected = parse_tmux_delta(head + tail)

    reader = TmuxDeltaReader()
    reader.feed("первая строка\r\nвторая строка\x1b[1;1H\x1b")
    reader.feed("[2K" + tail)

    assert reader.parse() == expected
    assert "перерисованная первая" in reader.parse()


def test_reader_keeps_last_repaint_of_the_line() -> None:
    reader = TmuxDeltaReader()
    _feed_in_chunks(reader, _tui_stream(), 17)

    result = reader.parse()

    assert "answer line two (final)" in result
    assert "thinking..." not in result


def test_parse_result_is_cached_until_new_data_arrives() -> None:
    reader = TmuxDeltaReader()
    reader.feed("текст\r\n")

    first = reader.parse()
    assert reader.parse() is first

    reader.feed("ещё текст\r\n")
    assert reader.parse() is not first
