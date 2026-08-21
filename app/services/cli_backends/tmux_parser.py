from __future__ import annotations

import re
from typing import Optional

from utils.text import strip_ansi

from .terminal_screen import TerminalScreen, render_terminal_output


# Оборванная последовательность длиннее этого предела считается обычным
# текстом: реальные CSI/OSC от TUI укладываются в сотню байт.
_INCOMPLETE_ESCAPE_MAX = 256
_COMPLETE_ESCAPE_RE = re.compile(
    r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\)|[()*+].|[^\[\]()*+])"
)
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_ANSI_ESCAPE_RE = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\)|[@-Z\\-_])")
_CLAUDE_SCREEN_READER_EVENT_RE = re.compile(
    r"(?im)(?:"
    r"(?:^[ \t]*|\$+(?:\(B)?)(?P<role>claude|tool):[ \t]*"
    r"|\$+(?:\(B)?[A-Za-z]+(?:…|\.{3})"
    r")"
)
_CLAUDE_SCREEN_READER_UI_LINE_RE = re.compile(
    r"(?i)^(?:"
    r"[A-Za-z][A-Za-z-]*(?:…|\.{3})(?:\s|$)"
    r"|.+(?:…|\.{3})\s*\(\s*"
    r"\d+(?:\.\d+)?\s*[hms](?:\s+\d+(?:\.\d+)?\s*[hms])*"
    r"\s+·\s+\d+(?:\.\d+)?k?\s+tokens(?:\s+·[^)]*)?\s*\)"
    r"|(?:don't ask|[a-z][a-z -]*permissions) on(?:\s|$)"
    r"|effort:\s*"
    r"|\$?(?:Baked|Cogitated|Cooked) for(?:\s|$)"
    r"|\(ctrl\+b\s+ctrl\+b\b"
    r"|\$(?:\(B)*\s*$"
    r")"
)
_CODEX_COMPOSER_RE = re.compile(r"^[›❯]\s")
_CODEX_STATUS_RE = re.compile(r"(?i)esc to interrupt")
_BOX_DRAWING_ONLY_RE = re.compile(r"^[─-╿\s]+$")


def _clean_rendered_text(rendered: str) -> str:
    stripped = _ANSI_ESCAPE_RE.sub("", rendered)
    stripped = strip_ansi(stripped)
    return _CONTROL_RE.sub("", stripped)


def normalize_terminal_text(text: str) -> str:
    # TUI рисуют экран через абсолютное позиционирование курсора, поэтому поток
    # проигрывается на модели экрана: без этого символы склеиваются в кашу
    # вида "WWoorrkkiinngg".
    return _clean_rendered_text(render_terminal_output(str(text or "")))


def _is_tui_chrome_line(line: str) -> bool:
    stripped = str(line).strip()
    if not stripped:
        return False
    if _CODEX_STATUS_RE.search(stripped):
        return True
    return bool(_BOX_DRAWING_ONLY_RE.match(stripped))


def strip_tui_chrome(text: str) -> str:
    """Убирает элементы интерфейса codex: поле ввода, футер, статус, рамки."""

    lines = str(text or "").splitlines()
    for idx in range(len(lines) - 1, -1, -1):
        if _CODEX_COMPOSER_RE.match(lines[idx].strip()):
            # Поле ввода всегда внизу экрана, под ним — только футер.
            lines = lines[:idx]
            break
    kept = [line for line in lines if not _is_tui_chrome_line(line)]
    return "\n".join(kept).strip()


def _extract_claude_screen_reader_message(text: str) -> str:
    events = list(_CLAUDE_SCREEN_READER_EVENT_RE.finditer(text))
    for idx in range(len(events) - 1, -1, -1):
        event = events[idx]
        if str(event.group("role") or "").lower() != "claude":
            continue
        end = events[idx + 1].start() if idx + 1 < len(events) else len(text)
        candidate = text[event.end():end].strip()
        if candidate:
            lines = candidate.splitlines()
            for line_idx, line in enumerate(lines):
                if _CLAUDE_SCREEN_READER_UI_LINE_RE.match(line.strip()):
                    lines = lines[:line_idx]
                    break
            candidate = "\n".join(lines).strip()
            if candidate:
                return candidate
    return ""


class TmuxDeltaReader:
    """Инкрементально проигрывает поток pane.log.

    Экран терминала — накопительное состояние, поэтому переигрывать всю историю
    запроса на каждом опросе не нужно: при выводе в сотни мегабайт один такой
    разбор занимал десятки секунд и блокировал event loop. Ридер хранит экран
    между вызовами и получает только новые байты.

    Завершение ответа читатель не определяет: это делает журнал CLI (JSONL).
    """

    def __init__(self) -> None:
        self._screen = TerminalScreen()
        self._pending = ""
        self._cache: dict[tuple[bool, bool], str] = {}
        self._rendered: Optional[str] = None

    def feed(self, chunk: str) -> None:
        text = self._pending + str(chunk or "")
        text, self._pending = _split_incomplete_escape(text)
        if not text:
            return
        self._cache.clear()
        self._rendered = None
        self._screen.feed(text)

    def parse(self, *, claude_screen_reader: bool = False, tui_chrome: bool = False) -> str:
        key = (bool(claude_screen_reader), bool(tui_chrome))
        cached = self._cache.get(key)
        if cached is not None:
            return cached
        if self._rendered is None:
            self._rendered = _clean_rendered_text(self._screen.to_text())
        if claude_screen_reader:
            result = _extract_claude_screen_reader_message(self._rendered)
        elif tui_chrome:
            result = strip_tui_chrome(self._rendered)
        else:
            result = self._rendered.strip()
        self._cache[key] = result
        return result


def _split_incomplete_escape(text: str) -> tuple[str, str]:
    """Отделяет хвост с оборванной escape-последовательностью.

    Чанк pane.log может закончиться посреди CSI/OSC. Если отдать такой хвост
    эмулятору, он либо потеряет последовательность, либо съест начало
    следующего чанка.
    """

    idx = text.rfind("\x1b")
    if idx == -1:
        return text, ""
    tail = text[idx:]
    if len(tail) > _INCOMPLETE_ESCAPE_MAX or _COMPLETE_ESCAPE_RE.match(tail):
        return text, ""
    return text[:idx], tail


def parse_tmux_delta(
    delta: str,
    *,
    claude_screen_reader: bool = False,
    tui_chrome: bool = False,
) -> str:
    reader = TmuxDeltaReader()
    reader.feed(delta)
    return reader.parse(claude_screen_reader=claude_screen_reader, tui_chrome=tui_chrome)
