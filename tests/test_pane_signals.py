from app.services.cli_backends.pane_signals import _OSC_BUFFER_LIMIT, PaneSignalScanner


def test_osc_title_with_bel_terminator() -> None:
    scanner = PaneSignalScanner()

    signals = scanner.feed("\x1b]0;claude working\x07")

    assert signals.title == "claude working"
    assert signals.bell_count == 0


def test_osc_title_with_st_terminator() -> None:
    scanner = PaneSignalScanner()

    signals = scanner.feed("\x1b]0;claude working\x1b\\")

    assert signals.title == "claude working"
    assert signals.bell_count == 0


def test_bel_terminator_then_real_bell() -> None:
    # Первый BEL закрывает OSC (терминатор, не звонок), второй — настоящий звонок.
    scanner = PaneSignalScanner()

    signals = scanner.feed("\x1b]0;✳ claude idle\x07\x07")

    assert signals.title == "✳ claude idle"
    assert signals.bell_count == 1


def test_osc8_hyperlink_bel_not_counted_as_bell() -> None:
    scanner = PaneSignalScanner()

    signals = scanner.feed("\x1b]8;;http://example.com\x07link\x1b]8;;\x07")

    assert signals.title is None
    assert signals.bell_count == 0


def test_title_and_bell_split_across_feed_calls() -> None:
    scanner = PaneSignalScanner()

    first = scanner.feed("\x1b]0;wor")
    second = scanner.feed("king\x07\x07")

    assert first.title is None
    assert first.bell_count == 0
    assert second.title == "working"
    # Первый BEL закрывает OSC, второй после него — настоящий звонок.
    assert second.bell_count == 1


def test_can_aborts_unterminated_osc() -> None:
    scanner = PaneSignalScanner()

    # CAN (0x18) обрывает незавершённый OSC: следующий BEL — уже настоящий звонок,
    # а не залипший терминатор.
    signals = scanner.feed("\x1b]0;stuck\x18\x07")

    assert signals.title is None
    assert signals.bell_count == 1


def test_sub_aborts_unterminated_osc() -> None:
    scanner = PaneSignalScanner()

    signals = scanner.feed("\x1b]0;stuck\x1a\x07")

    assert signals.title is None
    assert signals.bell_count == 1


def test_multiple_titles_in_one_chunk_returns_last() -> None:
    scanner = PaneSignalScanner()

    signals = scanner.feed("\x1b]0;first\x07\x1b]0;second\x07")

    assert signals.title == "second"


def test_plain_csi_gives_no_false_signals() -> None:
    scanner = PaneSignalScanner()

    signals = scanner.feed("\x1b[31mHello\x1b[0m")

    assert signals.title is None
    assert signals.bell_count == 0


def test_bare_bells_are_counted() -> None:
    scanner = PaneSignalScanner()

    signals = scanner.feed("a\x07b\x07c")

    assert signals.title is None
    assert signals.bell_count == 2


def test_repeated_esc_restarts_sequence_instead_of_eating_byte() -> None:
    # F2: повторный ESC до "]"/"[" — не мусорный байт, а начало новой
    # escape-последовательности; старой обработкой он терялся, а следующий
    # OSC читался как обычный текст с ложным звонком на завершающем BEL.
    scanner = PaneSignalScanner()

    signals = scanner.feed("\x1b\x1b]0;working\x07")

    assert signals.title == "working"
    assert signals.bell_count == 0


def test_bel_inside_csi_rings_without_aborting_sequence() -> None:
    # F3: BEL внутри CSI — C0 "execute" по ECMA-48, звенит немедленно. Что при
    # этом разбор CSI не прерывается, снаружи не наблюдаемо (после сброса в
    # "text" оставшиеся байты CSI тоже ничего не меняют), поэтому проверяется
    # именно счёт звонка — он и отличает "execute" от "звонок проглочен".
    scanner = PaneSignalScanner()

    signals = scanner.feed("\x1b[3\x07m")

    assert signals.title is None
    assert signals.bell_count == 1


def test_osc_buffer_overflow_discards_title_and_recovers_to_text() -> None:
    # F5: переполненный OSC не должен выдавать мусор как title, а терминатор
    # после переполнения — штатно возвращать сканер в "text".
    scanner = PaneSignalScanner()

    overflow = scanner.feed("\x1b]0;" + "x" * 5000)
    terminated = scanner.feed("\x07")

    assert overflow.title is None
    assert terminated.title is None

    # Автомат правда вернулся в "text": следующий голый BEL — настоящий звонок.
    after = scanner.feed("\x07")
    assert after.bell_count == 1


def test_osc_title_accepts_window_title_ps2() -> None:
    # OSC 2 задаёт только заголовок окна (без иконки) - его шлют многие TUI,
    # и он такой же источник статуса, как привычный OSC 0.
    scanner = PaneSignalScanner()

    signals = scanner.feed("\x1b]2;claude working\x07")

    assert signals.title == "claude working"


def test_osc_title_ignores_non_title_ps() -> None:
    # OSC 8 (гиперссылки) и OSC 133 (shell integration) заголовком не являются.
    scanner = PaneSignalScanner()

    assert scanner.feed("\x1b]8;;https://example.com\x07").title is None
    assert scanner.feed("\x1b]133;A\x07").title is None


def test_osc_buffer_boundary_is_exact() -> None:
    # Лимит проверяется до записи символа, поэтому заголовок ровно в лимит
    # ещё извлекается, а следующий за ним символ уже роняет всю посылку.
    head = "0;"
    at_limit = PaneSignalScanner()
    body = "x" * (_OSC_BUFFER_LIMIT - len(head))
    assert at_limit.feed("\x1b]" + head + body + "\x07").title == body

    over_limit = PaneSignalScanner()
    assert over_limit.feed("\x1b]" + head + body + "x\x07").title is None
