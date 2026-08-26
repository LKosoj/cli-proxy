"""Разбор escape-последовательностей сырого pane.log: заголовок терминала
(OSC 0/1/2) и настоящие BEL-звонки.

Сканер stateful и рассчитан на вызовы по кускам: PTY отдаёт данные
произвольными порциями, и escape-последовательность может оборваться на
границе чанка — состояние переживает вызов feed() и продолжается в
следующем. Пересоздаётся сканер целиком (см. `_PaneStream._restart`), метод
reset() не нужен.

Роль этих сигналов — диагностика и один узкий case в tmux_backend.py.
Транскрипт CLI остаётся единственным источником завершения хода: эти байты
и так попадают в pane.log и сами двигают таймер тишины, отдельный код для
этого не нужен.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

# В состоянии "text" интересны только начало escape (ESC) и голый звонок
# (BEL) — один re.search по классу символов, как _CONTROL_RE в
# terminal_screen.py, а не посимвольный цикл по всему чанку.
_TEXT_SCAN_RE = re.compile(r"[\x1b\x07]")
# Финальный байт CSI (ECMA-48): диапазон 0x40-0x7e.
_CSI_FINAL_RE = re.compile(r"[\x40-\x7e]")
# CAN/SUB по ECMA-48 отменяют незавершённую escape-последовательность. Без
# этого оборванный OSC от упавшего TUI навсегда зависает в состоянии "внутри
# OSC" и глотает все последующие звонки как терминаторы.
_CANCEL_CHARS = "\x18\x1a"
# Защита от мусорного потока: OSC без терминатора не должен копиться вечно.
_OSC_BUFFER_LIMIT = 512


def _extract_title(buffer: str) -> Optional[str]:
    """Заголовок принимается только для Ps 0/1/2 — прочие OSC (гиперссылки
    OSC 8, shell-integration OSC 133/633) пропускаются молча."""

    ps, sep, rest = buffer.partition(";")
    if not sep or ps not in ("0", "1", "2"):
        return None
    return rest


@dataclass(frozen=True)
class PaneSignals:
    title: Optional[str] = None
    bell_count: int = 0


class PaneSignalScanner:
    """Один проход по декодированному чанку pane.log."""

    def __init__(self) -> None:
        self._state = "text"
        self._osc_buffer = ""
        # Текущая OSC-последовательность испорчена переполнением буфера:
        # title из неё не восстанавливаем, даже когда терминатор придёт позже.
        self._osc_overflowed = False

    def feed(self, chunk: str) -> PaneSignals:
        title: Optional[str] = None
        bell_count = 0
        pos = 0
        length = len(chunk)
        while pos < length:
            if self._state == "text":
                match = _TEXT_SCAN_RE.search(chunk, pos)
                if match is None:
                    break
                pos = match.end()
                if match.group() == "\x07":
                    bell_count += 1
                else:
                    self._state = "esc"
                continue

            char = chunk[pos]
            pos += 1

            if char in _CANCEL_CHARS:
                self._state = "text"
                self._osc_buffer = ""
                self._osc_overflowed = False
                continue

            if self._state == "esc":
                if char == "\x1b":
                    # Повторный ESC до завершения последовательности не
                    # теряется: он сам начинает escape-последовательность
                    # заново (мы и так уже в "esc", состояние не меняется).
                    pass
                elif char == "\x07":
                    # C0 BEL — "execute" по ECMA-48: звенит немедленно и не
                    # прерывает уже начатую escape-последовательность.
                    bell_count += 1
                elif char == "]":
                    self._state = "osc"
                    self._osc_buffer = ""
                    self._osc_overflowed = False
                elif char == "[":
                    self._state = "csi"
                else:
                    self._state = "text"
            elif self._state == "csi":
                if char == "\x1b":
                    # ESC прерывает незавершённый CSI и сам начинает новую
                    # escape-последовательность (см. состояние "esc" выше).
                    self._state = "esc"
                elif char == "\x07":
                    # BEL в csi_param/csi_intermediate — тоже "execute":
                    # звенит немедленно, разбор CSI при этом не завершается.
                    bell_count += 1
                elif _CSI_FINAL_RE.match(char):
                    self._state = "text"
            elif self._state == "osc":
                if char == "\x07":
                    # BEL здесь — терминатор ST, а не звонок: не считаем его.
                    if not self._osc_overflowed:
                        extracted = _extract_title(self._osc_buffer)
                        if extracted is not None:
                            title = extracted
                    self._state = "text"
                elif char == "\x1b":
                    self._state = "osc_esc"
                elif len(self._osc_buffer) < _OSC_BUFFER_LIMIT:
                    self._osc_buffer += char
                else:
                    # Лимит превышен — последовательность испорчена: буфер
                    # отбрасываем и молча ждём терминатора, title из мусора
                    # не выдаём (реальный заголовок терминала короче лимита).
                    self._osc_buffer = ""
                    self._osc_overflowed = True
            elif self._state == "osc_esc":
                if char == "\\":
                    if not self._osc_overflowed:
                        extracted = _extract_title(self._osc_buffer)
                        if extracted is not None:
                            title = extracted
                    self._state = "text"
                elif char == "\x1b":
                    # Не ST: предыдущий OSC не был завершён корректно, этот
                    # ESC сам начинает новую escape-последовательность.
                    self._state = "esc"
                    self._osc_buffer = ""
                    self._osc_overflowed = False
                elif char == "\x07":
                    bell_count += 1
                else:
                    self._state = "text"

        return PaneSignals(title=title, bell_count=bell_count)
