"""Статус CLI-агента по заголовку терминала (OSC 0/1/2 в pane.log).

Заголовок обновляется TUI отдельно от текста экрана и не режется
скринридером Claude, поэтому это самостоятельный диагностический сигнал:
"работает" / "ждёт разрешения" / "простаивает".
"""

from __future__ import annotations

import re
from typing import Literal, Optional

PaneTitleStatus = Literal["working", "permission", "idle"]

# ✳ (U+2733) — статус ожидания ввода в Claude Code.
_CLAUDE_IDLE_RE = re.compile("✳")
# Braille-спиннер (U+2800–U+28FF) и квадранты (U+25D0–U+25D3, формат Claude
# Code 2.1.228+) — оба означают "агент работает".
_CLAUDE_WORKING_RE = re.compile("[⠀-⣿◐-◓]")

# ✦ (U+2726) и ⏲ (U+23F2) — Gemini печатает/думает.
_GEMINI_WORKING_RE = re.compile("[✦⏲]")
# ◇ (U+25C7) — Gemini простаивает.
_GEMINI_IDLE_RE = re.compile("◇")
# ✋ (U+270B) — Gemini ждёт разрешения пользователя.
_GEMINI_PERMISSION_RE = re.compile("✋")

# Для CLI без своей таблицы глифов (codex/qwen/grok/kimi и будущие) статус
# ищем по словам в заголовке. Границы обязательны: без них "reworking" и
# "~/codex/ready" дают ложное срабатывание. Отрицание перед словом ("not
# ready", "isn't working") переворачивает смысл — исключаем его же
# лукбихайндом. "working directory" — обычный лейбл пути, а не признак
# активности, поэтому тоже исключён.
# Этот разбор эвристичен и годится только для диагностики: он по построению
# не может вернуть "permission", поэтому на удержание хода не влияет.
# Лукбихайнд фиксированной длины видит ровно один пробел, поэтому перед
# разбором пробельные серии схлопываются: иначе "not  ready" (два пробела
# после выравнивания TUI) или "not\tready" обошли бы исключение отрицания.
# Перевод строки при этом сохраняется: он разделяет разные фразы, и
# схлопывать его значило бы переносить отрицание из одной строки в другую.
# Одиночный возврат каретки к таким границам не относится - он двигает
# курсор внутри той же строки, поэтому схлопывается наравне с пробелом.
_HORIZONTAL_SPACE_RE = re.compile(r"[^\S\n]+")
_GENERIC_WORKING_RE = re.compile(
    r"(?<!\bnot )(?<!n't )(?<![\w./\\-])(?:working(?!\s+directory\b)|thinking|running)(?![\w-])",
    re.IGNORECASE,
)
_GENERIC_IDLE_RE = re.compile(
    r"(?<!\bnot )(?<!n't )(?<![\w./\\-])(ready|idle|done)(?![\w-])",
    re.IGNORECASE,
)


def classify_pane_title(title: str, *, cli_name: str) -> Optional[PaneTitleStatus]:
    """Статус агента по заголовку терминала (OSC 0/2). None — определить не удалось.

    Для CLI без своей таблицы глифов используется generic-разбор по словам
    (см. _GENERIC_WORKING_RE/_GENERIC_IDLE_RE) — он эвристичен и годится
    только для диагностики. По построению он не умеет возвращать
    "permission", поэтому на удержание хода не влияет.
    """

    text = str(title or "")
    if not text:
        return None
    name = str(cli_name or "").strip().lower()
    if name == "claude":
        if _CLAUDE_WORKING_RE.search(text):
            return "working"
        if _CLAUDE_IDLE_RE.search(text):
            return "idle"
        return None
    if name == "gemini":
        if _GEMINI_PERMISSION_RE.search(text):
            return "permission"
        if _GEMINI_WORKING_RE.search(text):
            return "working"
        if _GEMINI_IDLE_RE.search(text):
            return "idle"
        return None
    probe = _HORIZONTAL_SPACE_RE.sub(" ", text)
    if _GENERIC_WORKING_RE.search(probe):
        return "working"
    if _GENERIC_IDLE_RE.search(probe):
        return "idle"
    return None
