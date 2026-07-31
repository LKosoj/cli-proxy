"""Minimal terminal emulator for tmux pane logs.

TUI-приложения (codex, claude) рисуют экран через абсолютное позиционирование
курсора, поэтому простое вырезание ANSI-последовательностей склеивает символы
в нечитаемую кашу ("WWoorrkkiinngg"). Здесь поток проигрывается на модели
экрана: строки, выпавшие вверх при прокрутке, попадают в scrollback, а поверх
них отдаётся текущее содержимое экрана.
"""

from __future__ import annotations

import re
from typing import List

DEFAULT_WIDTH = 200
DEFAULT_HEIGHT = 50
DEFAULT_SCROLLBACK_LIMIT = 20000

_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
_CSI_RE = re.compile(r"\x1b\[([0-?]*)([ -/]*)([@-~])")
_OSC_RE = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")


class TerminalScreen:
    """Проигрывает поток терминала и отдаёт видимый текст."""

    def __init__(
        self,
        width: int = DEFAULT_WIDTH,
        height: int = DEFAULT_HEIGHT,
        *,
        scrollback_limit: int = DEFAULT_SCROLLBACK_LIMIT,
    ) -> None:
        self.width = max(1, int(width))
        self.height = max(1, int(height))
        self._scrollback_limit = max(0, int(scrollback_limit))
        self._scrollback: List[str] = []
        self._reset()

    def _reset(self) -> None:
        self._screen: List[List[str]] = [self._blank_line() for _ in range(self.height)]
        self._row = 0
        self._col = 0
        self._scroll_top = 0
        self._scroll_bottom = self.height - 1
        self._saved_cursor = (0, 0)
        self._pending_wrap = False

    def _blank_line(self) -> List[str]:
        return [" "] * self.width

    # --- прокрутка ---------------------------------------------------

    def _scroll_up(self, count: int = 1) -> None:
        for _ in range(max(1, count)):
            dropped = self._screen.pop(self._scroll_top)
            # В scrollback уходят только строки, вытесненные сверху экрана.
            if self._scroll_top == 0 and self._scrollback_limit:
                self._scrollback.append("".join(dropped).rstrip())
                if len(self._scrollback) > self._scrollback_limit:
                    del self._scrollback[: len(self._scrollback) - self._scrollback_limit]
            self._screen.insert(self._scroll_bottom, self._blank_line())

    def _scroll_down(self, count: int = 1) -> None:
        for _ in range(max(1, count)):
            self._screen.pop(self._scroll_bottom)
            self._screen.insert(self._scroll_top, self._blank_line())

    def _line_feed(self) -> None:
        if self._row == self._scroll_bottom:
            self._scroll_up()
        elif self._row < self.height - 1:
            self._row += 1

    def _reverse_line_feed(self) -> None:
        if self._row == self._scroll_top:
            self._scroll_down()
        elif self._row > 0:
            self._row -= 1

    # --- вывод символов ----------------------------------------------

    def _write_text(self, text: str) -> None:
        for char in text:
            if self._pending_wrap:
                self._col = 0
                self._line_feed()
                self._pending_wrap = False
            self._screen[self._row][self._col] = char
            if self._col == self.width - 1:
                # Автоперенос откладывается до следующего символа — так курсор,
                # переставленный escape-последовательностью, не съезжает.
                self._pending_wrap = True
            else:
                self._col += 1

    # --- разбор потока -----------------------------------------------

    def feed(self, data: str) -> "TerminalScreen":
        text = str(data or "")
        pos = 0
        length = len(text)
        while pos < length:
            match = _CONTROL_RE.search(text, pos)
            if match is None:
                self._write_text(text[pos:])
                break
            if match.start() > pos:
                self._write_text(text[pos:match.start()])
            char = match.group()
            pos = match.end()
            if char == "\x1b":
                pos = self._handle_escape(text, pos)
            elif char == "\n" or char == "\x0b" or char == "\x0c":
                # pipe-pane отдаёт "\r\n", но обычный текст приходит с голым
                # "\n" — трактуем перевод строки как CR+LF, иначе строки
                # выстраиваются лесенкой.
                self._pending_wrap = False
                self._col = 0
                self._line_feed()
            elif char == "\r":
                self._pending_wrap = False
                self._col = 0
            elif char == "\x08":
                self._pending_wrap = False
                self._col = max(0, self._col - 1)
            elif char == "\t":
                self._pending_wrap = False
                self._col = min(self.width - 1, (self._col // 8 + 1) * 8)
            # Остальные управляющие символы (BEL, SO/SI и пр.) игнорируются.
        return self

    def _handle_escape(self, text: str, pos: int) -> int:
        if pos >= len(text):
            return pos
        char = text[pos]
        if char == "[":
            match = _CSI_RE.match(text, pos - 1)
            if match is None:
                return pos + 1
            self._handle_csi(match.group(1), match.group(3))
            return match.end()
        if char == "]":
            match = _OSC_RE.match(text, pos - 1)
            return match.end() if match is not None else len(text)
        if char in "()*+":
            return pos + 2  # выбор набора символов
        if char == "D":
            self._pending_wrap = False
            self._line_feed()
        elif char == "M":
            self._pending_wrap = False
            self._reverse_line_feed()
        elif char == "E":
            self._pending_wrap = False
            self._col = 0
            self._line_feed()
        elif char == "7":
            self._saved_cursor = (self._row, self._col)
        elif char == "8":
            self._row, self._col = self._saved_cursor
            self._clamp_cursor()
        elif char == "c":
            self._reset()
        return pos + 1

    def _handle_csi(self, params_raw: str, final: str) -> None:
        if params_raw.startswith("?") or params_raw.startswith(">") or params_raw.startswith("<"):
            return  # приватные режимы DEC (курсор, alt-screen, sync) не влияют на текст
        params = []
        for chunk in params_raw.split(";"):
            try:
                params.append(int(chunk))
            except ValueError:
                params.append(0)
        if not params:
            params = [0]

        def arg(index: int, default: int = 1) -> int:
            value = params[index] if index < len(params) else 0
            return value if value > 0 else default

        self._pending_wrap = False
        if final == "H" or final == "f":
            self._row = arg(0) - 1
            self._col = arg(1) - 1
            self._clamp_cursor()
        elif final == "A":
            self._row = max(0, self._row - arg(0))
        elif final == "B":
            self._row = min(self.height - 1, self._row + arg(0))
        elif final == "C":
            self._col = min(self.width - 1, self._col + arg(0))
        elif final == "D":
            self._col = max(0, self._col - arg(0))
        elif final == "E":
            self._col = 0
            self._row = min(self.height - 1, self._row + arg(0))
        elif final == "F":
            self._col = 0
            self._row = max(0, self._row - arg(0))
        elif final == "G" or final == "`":
            self._col = arg(0) - 1
            self._clamp_cursor()
        elif final == "d":
            self._row = arg(0) - 1
            self._clamp_cursor()
        elif final == "J":
            self._erase_display(params[0] if params else 0)
        elif final == "K":
            self._erase_line(params[0] if params else 0)
        elif final == "L":
            self._insert_lines(arg(0))
        elif final == "M":
            self._delete_lines(arg(0))
        elif final == "P":
            self._delete_chars(arg(0))
        elif final == "@":
            self._insert_chars(arg(0))
        elif final == "X":
            self._erase_chars(arg(0))
        elif final == "S":
            self._scroll_up(arg(0))
        elif final == "T":
            self._scroll_down(arg(0))
        elif final == "r":
            top = arg(0) - 1
            bottom = (params[1] - 1) if len(params) > 1 and params[1] > 0 else self.height - 1
            if 0 <= top < bottom < self.height:
                self._scroll_top = top
                self._scroll_bottom = bottom
            else:
                self._scroll_top = 0
                self._scroll_bottom = self.height - 1
            self._row = self._scroll_top
            self._col = 0
        # SGR ("m") и прочее на текст не влияют.

    def _clamp_cursor(self) -> None:
        self._row = max(0, min(self.height - 1, self._row))
        self._col = max(0, min(self.width - 1, self._col))

    # --- операции стирания/вставки ------------------------------------

    def _erase_display(self, mode: int) -> None:
        if mode == 0:
            self._erase_line(0)
            for row in range(self._row + 1, self.height):
                self._screen[row] = self._blank_line()
        elif mode == 1:
            self._erase_line(1)
            for row in range(0, self._row):
                self._screen[row] = self._blank_line()
        else:
            for row in range(self.height):
                self._screen[row] = self._blank_line()
            if mode == 3:
                self._scrollback.clear()

    def _erase_line(self, mode: int) -> None:
        line = self._screen[self._row]
        if mode == 0:
            for col in range(self._col, self.width):
                line[col] = " "
        elif mode == 1:
            for col in range(0, min(self._col + 1, self.width)):
                line[col] = " "
        else:
            self._screen[self._row] = self._blank_line()

    def _insert_lines(self, count: int) -> None:
        if not self._scroll_top <= self._row <= self._scroll_bottom:
            return
        for _ in range(min(count, self._scroll_bottom - self._row + 1)):
            self._screen.pop(self._scroll_bottom)
            self._screen.insert(self._row, self._blank_line())

    def _delete_lines(self, count: int) -> None:
        if not self._scroll_top <= self._row <= self._scroll_bottom:
            return
        for _ in range(min(count, self._scroll_bottom - self._row + 1)):
            self._screen.pop(self._row)
            self._screen.insert(self._scroll_bottom, self._blank_line())

    def _delete_chars(self, count: int) -> None:
        line = self._screen[self._row]
        for _ in range(min(count, self.width - self._col)):
            del line[self._col]
            line.append(" ")

    def _insert_chars(self, count: int) -> None:
        line = self._screen[self._row]
        for _ in range(min(count, self.width - self._col)):
            line.insert(self._col, " ")
            del line[-1]

    def _erase_chars(self, count: int) -> None:
        line = self._screen[self._row]
        for col in range(self._col, min(self._col + count, self.width)):
            line[col] = " "

    # --- результат ----------------------------------------------------

    def display_lines(self) -> List[str]:
        lines = list(self._scrollback)
        lines.extend("".join(row).rstrip() for row in self._screen)
        while lines and not lines[-1]:
            lines.pop()
        return lines

    def to_text(self) -> str:
        return "\n".join(self.display_lines())


def render_terminal_output(
    data: str,
    *,
    width: int = DEFAULT_WIDTH,
    height: int = DEFAULT_HEIGHT,
) -> str:
    """Проигрывает поток терминала и возвращает получившийся текст."""

    return TerminalScreen(width, height).feed(data).to_text()
