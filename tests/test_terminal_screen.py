from app.services.cli_backends.terminal_screen import TerminalScreen, render_terminal_output


def test_cursor_addressing_does_not_interleave_characters() -> None:
    # codex перерисовывает статус посимвольно с абсолютным позиционированием:
    # без проигрывания потока получалось "WWoorrkkiinngg".
    stream = "\x1b[1;1HWorking\x1b[1;1HWorking"

    assert render_terminal_output(stream, width=20, height=3) == "Working"


def test_carriage_return_overwrites_line() -> None:
    stream = "Working (0s)\rWorking (1s)"

    assert render_terminal_output(stream, width=40, height=3) == "Working (1s)"


def test_backspace_removes_previous_character() -> None:
    assert render_terminal_output("abcX\x08Y", width=20, height=3) == "abcY"


def test_plain_text_keeps_line_structure() -> None:
    text = "первая строка\nвторая строка\nтретья"

    assert render_terminal_output(text, width=40, height=10) == text


def test_erase_line_clears_stale_content() -> None:
    stream = "старый длинный текст\r\x1b[Kновый"

    assert render_terminal_output(stream, width=40, height=3) == "новый"


def test_erase_display_clears_screen() -> None:
    stream = "мусор\n\x1b[2J\x1b[1;1Hчисто"

    assert render_terminal_output(stream, width=40, height=5) == "чисто"


def test_scrolled_lines_are_preserved_in_scrollback() -> None:
    screen = TerminalScreen(width=20, height=2)
    screen.feed("строка1\r\nстрока2\r\nстрока3\r\n")

    assert screen.display_lines()[:3] == ["строка1", "строка2", "строка3"]


def test_scroll_region_does_not_lose_output() -> None:
    # Приложение задаёт регион прокрутки, чтобы не трогать нижнюю панель.
    screen = TerminalScreen(width=20, height=4)
    screen.feed("\x1b[1;3r\x1b[1;1Hодин\r\nдва\r\nтри\r\nчетыре")

    assert "один" in screen.to_text()
    assert "четыре" in screen.to_text()


def test_autowrap_splits_long_line() -> None:
    assert render_terminal_output("abcdef", width=3, height=4) == "abc\ndef"
