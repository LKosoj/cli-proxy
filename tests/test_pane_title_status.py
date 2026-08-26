from app.services.cli_backends.pane_title_status import classify_pane_title


def test_claude_idle_glyph() -> None:
    assert classify_pane_title("✳ claude", cli_name="claude") == "idle"


def test_claude_working_braille_spinner() -> None:
    assert classify_pane_title("⠋ claude", cli_name="claude") == "working"


def test_claude_working_quadrant_spinner() -> None:
    # Формат Claude Code 2.1.228+: спиннер квадрантами вместо braille.
    assert classify_pane_title("◐ claude", cli_name="claude") == "working"


def test_claude_unknown_glyph_is_none() -> None:
    assert classify_pane_title("claude — untitled", cli_name="claude") is None


def test_gemini_working_glyphs() -> None:
    assert classify_pane_title("✦ Generating", cli_name="gemini") == "working"
    assert classify_pane_title("⏲ Thinking", cli_name="gemini") == "working"


def test_gemini_idle_glyph() -> None:
    assert classify_pane_title("◇ gemini", cli_name="gemini") == "idle"


def test_gemini_permission_glyph() -> None:
    assert classify_pane_title("✋ gemini", cli_name="gemini") == "permission"


def test_generic_working_words() -> None:
    assert classify_pane_title("codex: working", cli_name="codex") == "working"
    assert classify_pane_title("qwen thinking...", cli_name="qwen") == "working"
    assert classify_pane_title("grok running", cli_name="grok") == "working"
    assert classify_pane_title("kimi: running", cli_name="kimi") == "working"


def test_generic_idle_words() -> None:
    assert classify_pane_title("codex: ready", cli_name="codex") == "idle"
    assert classify_pane_title("qwen idle", cli_name="qwen") == "idle"
    assert classify_pane_title("done", cli_name="codex") == "idle"


def test_generic_false_positive_reworking() -> None:
    # "working" внутри другого слова не должен считаться статусом.
    assert classify_pane_title("reworking something", cli_name="codex") is None


def test_generic_false_positive_path_with_ready() -> None:
    # "ready" — часть пути, а не статус.
    assert classify_pane_title("~/codex/ready", cli_name="codex") is None


def test_empty_title_is_none() -> None:
    assert classify_pane_title("", cli_name="claude") is None


def test_unknown_title_is_none() -> None:
    assert classify_pane_title("some random terminal title", cli_name="codex") is None


def test_generic_negation_before_idle_word_is_none() -> None:
    # F4: "NOT READY" — отрицание переворачивает смысл, это не "idle".
    assert classify_pane_title("NOT READY", cli_name="codex") is None


def test_generic_negation_survives_wide_whitespace() -> None:
    # F4: TUI выравнивает заголовок пробелами и табами - отрицание должно
    # исключать статус и там, где разделитель шире одного пробела.
    assert classify_pane_title("not  ready", cli_name="codex") is None
    assert classify_pane_title("not\tready", cli_name="codex") is None
    assert classify_pane_title("isn't  working", cli_name="codex") is None
    # Возврат каретки двигает курсор внутри строки, границей фразы он не является.
    assert classify_pane_title("not\rready", cli_name="codex") is None
    # Перевод строки - граница фразы: отрицание из прошлой строки не переносится.
    assert classify_pane_title("docs not\nready to ship", cli_name="codex") == "idle"


def test_generic_negation_requires_whole_word_not() -> None:
    # Лукбихайнд смотрит фиксированные 4 символа, поэтому без границы слова
    # любое слово, кончающееся на "not", гасило бы следующий за ним статус.
    assert classify_pane_title("cannot working", cli_name="codex") == "working"
    assert classify_pane_title("cannot ready", cli_name="codex") == "idle"
    # Само отрицание при этом по-прежнему работает.
    assert classify_pane_title("is not working", cli_name="codex") is None


def test_generic_working_directory_label_is_none() -> None:
    # F4: "working directory" — обычный лейбл пути, а не признак активности.
    assert classify_pane_title("current working directory: /tmp", cli_name="codex") is None


def test_generic_false_positive_regressions_unaffected() -> None:
    # F4-регрессия: границы слов, которые уже работали правильно, не сломаны.
    assert classify_pane_title("reworking something", cli_name="codex") is None
    assert classify_pane_title("~/codex/ready", cli_name="codex") is None
    assert classify_pane_title("~/src/gemini-working/app", cli_name="codex") is None
    assert classify_pane_title("idle-timeout", cli_name="codex") is None
    assert classify_pane_title("done.", cli_name="codex") == "idle"
    assert classify_pane_title("Ready", cli_name="codex") == "idle"


def test_gemini_permission_wins_over_working_glyph() -> None:
    # Gemini печатает "✦" пока рисует запрос разрешения, поэтому оба глифа в
    # заголовке соседствуют. Ожидание ввода важнее: порядок проверок значим.
    assert classify_pane_title("✦ ✋ allow write?", cli_name="gemini") == "permission"
    assert classify_pane_title("✋ ◇ gemini", cli_name="gemini") == "permission"
