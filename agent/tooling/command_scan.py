"""Нормализация shell-команды перед прогоном через regex-политику безопасности.

Портирование ``scannableCommand()`` из TypeScript-референса (qm: ``src/policy/command-policy.ts``).
Идея: сырая строка команды может прятать опасный текст за кавычками (``su""do``),
ANSI-C escape-последовательностями (``$'\\x73\\x75\\x64\\x6f'``), обёртками
(``bash -c '...'``, ``eval``, ``sudo``, ``env``) и command substitution (``$(...)``, `` `...` ``).
``scannable_command()`` разворачивает всё это в "плоский" текст, по которому regex-паттерны
уже не обманешь квотингом, и одновременно вырезает текст, который шелл никогда не исполнит
(например, тело heredoc, записываемого в файл, а не скармливаемого шеллу).

Осознанные отличия/ограничения относительно референса:

1. ``_unquote_bare_word`` восстанавливает содержимое кавычек, ТОЛЬКО если оно выглядит как
   одно "голое слово" (``[\\w@%+=:,./-]+``, ASCII). Если внутри кавычек несколько слов или
   спецсимволы шелла (пробелы, ``;``, ``&`` и т.п.) — оно схлопывается в пустые кавычки
   (``''``/``""``), а НЕ восстанавливается как есть. Это намеренный trade-off: если бы
   многословный текст восстанавливался дословно, из безобидного текста коммит-мессаджа можно
   было бы случайно собрать опасную с виду подстроку. Из-за этого ``$'\\x72m -rf /x'``
   (многословная ANSI-C строка) даст ``''``, а не ``rm -rf /x`` — это ожидаемо, а не баг.
2. ``_decode_ansi_c`` в референсе (JS ``String.fromCodePoint``) кидает исключение на
   недопустимом codepoint (например, ``\\UFFFFFFFF`` — за пределами ``0x10FFFF``). Здесь это
   обёрнуто в try/except (``ValueError`` и ``OverflowError`` — Python по-разному реагирует
   на "просто недопустимый" и "слишком большой для C int" codepoint): недопустимая
   escape-последовательность возвращается как есть, без падения всего сканирования.
   Это осознанное расхождение с референсом ради устойчивости.
3. Питоновский модуль ``re`` не имеет встроенного таймаута на матчинг (в отличие от некоторых
   других рантаймов) — это известное ограничение. Защититься от катастрофического backtracking
   полностью нельзя; вместо этого вход ограничен по длине (``MAX_SCAN_INPUT_CHARS``), а сами
   паттерны в этом модуле написаны без вложенных квантификаторов над пересекающимися классами.

Бюджет и рекурсия (защита от "широкой бомбы", которой нет в референсе):

- ``MAX_RECURSION_DEPTH`` ограничивает глубину цепочки разворачиваний (``bash -c 'bash -c ...'``).
- ``MAX_EXPANSION_NODES`` — общий бюджет разворачиваний на весь вызов ``scannable_command``,
  декрементируется на каждое рекурсивное разворачивание независимо от глубины. Он нужен
  отдельно от ограничения глубины: 50 ``$(...)`` на одном уровне, каждый с 50 вложенными —
  это комбинаторный взрыв, который счётчик глубины не ловит вообще (глубина цепочки та же).
- При исчерпании бюджета/глубины возвращается уже собранный текст, без исключений.
"""

from __future__ import annotations

import logging
import re
from bisect import bisect_left
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)

MAX_RECURSION_DEPTH: int = 8
MAX_SCAN_INPUT_CHARS: int = 20_000
MAX_SCAN_OUTPUT_CHARS: int = 40_000
MAX_EXPANSION_NODES: int = 64
# Разворачивание ``env -S "..."`` ре-токенизирует значение целиком, поэтому каждый шаг
# стоит O(остатка строки), а список слов может стать длиннее исходного — единственная
# ветка разбора обёрток, которая не уменьшает вход. Без лимита цепочка из 2000 вложенных
# ``env -S`` на 18 КБ занимала 6 секунд синхронно в event loop. Реальные команды глубже
# одного-двух уровней не бывают.
MAX_SPLIT_STRING_UNWRAPS: int = 8

_SHELL_EXECUTABLES = {"bash", "sh", "dash", "zsh", "ksh"}
_STDIN_SCRIPTS = {"-", "/dev/stdin", "/dev/fd/0", "/proc/self/fd/0"}
_SUDO_VALUE_OPTIONS = {
    "-u", "--user", "-g", "--group", "-h", "--host", "-p", "--prompt",
    "-C", "--chdir", "-T", "--command-timeout", "-R", "--chroot", "-t", "--type",
}


@dataclass
class _Budget:
    remaining: int = MAX_EXPANSION_NODES


@dataclass
class ShellScan:
    commands: List[List[str]] = field(default_factory=list)
    nested: List[str] = field(default_factory=list)


def _char_at(text: str, i: int) -> str:
    """``text[i]``, но вне диапазона тихо возвращает "" (как JS ``String.charAt``)."""
    if 0 <= i < len(text):
        return text[i]
    return ""


# ==== Layer 1: базовые regex-преобразования ====

_SUBS_RE = re.compile(r"\$\([^)]*\)|`[^`]*`")
_BACKSLASH_ESCAPE_RE = re.compile(r"\\([\w@%+=:,./-])", re.ASCII)
_BARE_WORD_RE = re.compile(r"^[\w@%+=:,./-]*$", re.ASCII)

# Lookaround отсекает here-string: в `bash <<<EOF` жадный `[^\n]*` иначе находил `<<` внутри
# `<<<`, следующая строка объявлялась телом heredoc и вырезалась — вместе с командой, которую
# bash на самом деле исполняет отдельной строкой.
_HEREDOC_OPEN_RE = re.compile(r"([^\n]*)(?<!<)<<-?(?!<)\s*([\"']?)([A-Za-z_]\w*)\2([^\n]*)$", re.ASCII)
_HEREDOC_MARKER_RE = re.compile(r"[A-Za-z_]\w*", re.ASCII)
# `\s` в ASCII-режиме минус `\n`, который внутри строки не встречается.
_HEREDOC_LINE_WS = " \t\r\f\v"

_ANSI_HEX2_RE = re.compile(r"\\x([0-9a-fA-F]{1,2})")
_ANSI_U4_RE = re.compile(r"\\u([0-9a-fA-F]{4})")
_ANSI_U8_RE = re.compile(r"\\U([0-9a-fA-F]{8})")
_ANSI_OCTAL_RE = re.compile(r"\\([0-7]{1,3})")
_ANSI_NAMED_RE = re.compile(r"\\([\\'\"abefnrtv])")
_ANSI_NAMED_MAP = {"a": "\x07", "b": "\b", "e": "\x1b", "f": "\f", "n": "\n", "r": "\r", "t": "\t", "v": "\v"}


def _safe_chr(codepoint: int, fallback: str) -> str:
    try:
        return chr(codepoint)
    except (ValueError, OverflowError):
        # За пределами 0x10FFFF Python бросает ValueError, а для очень больших чисел
        # (например, \Uffffffff) — OverflowError (JS кинул бы RangeError в обоих случаях).
        # Осознанное отличие от референса: не роняем всё сканирование, оставляем как есть.
        return fallback


def _decode_ansi_c(value: str) -> str:
    """Раскодировать ANSI-C escape-последовательности (``$'...'`` содержимое).

    Порядок замен важен и совпадает с референсом: \\xNN, \\uNNNN, \\UNNNNNNNN, восьмеричные,
    затем именованные (\\n, \\t и т.д.). Недопустимый codepoint (см. модульный docstring,
    пункт 2) не роняет функцию — соответствующий escape остаётся в исходном виде.
    """
    value = _ANSI_HEX2_RE.sub(lambda m: _safe_chr(int(m.group(1), 16), m.group(0)), value)
    value = _ANSI_U4_RE.sub(lambda m: _safe_chr(int(m.group(1), 16), m.group(0)), value)
    value = _ANSI_U8_RE.sub(lambda m: _safe_chr(int(m.group(1), 16), m.group(0)), value)
    value = _ANSI_OCTAL_RE.sub(lambda m: _safe_chr(int(m.group(1), 8), m.group(0)), value)
    value = _ANSI_NAMED_RE.sub(lambda m: _ANSI_NAMED_MAP.get(m.group(1), m.group(1)), value)
    return value


def _unquote_bare_word(inner: str) -> Optional[str]:
    """См. модульный docstring, пункт 1: восстанавливает только однословные раскрытия."""
    return inner if _BARE_WORD_RE.match(inner) else None


def _collapse_double_quoted(inner: str) -> str:
    subs = _SUBS_RE.findall(inner)
    if subs:
        return " ".join(subs)
    unquoted = _unquote_bare_word(inner)
    return unquoted if unquoted is not None else '""'


def _collapse_ansi_c_quoted(inner: str) -> str:
    unquoted = _unquote_bare_word(_decode_ansi_c(inner))
    return unquoted if unquoted is not None else "''"


def _collapse_single_quoted(inner: str) -> str:
    unquoted = _unquote_bare_word(inner)
    return unquoted if unquoted is not None else "''"


@dataclass(frozen=True)
class _QuotedSpan:
    start: int  # индекс открывающего символа (`$` для `$'...'`)
    end: int    # индекс за концом участка
    kind: str   # "'" | '"' | "$'"
    closed: bool


def _closing_quote(text: str, start: int, quote: str) -> int:
    """Индекс закрывающей кавычки с учётом ``\\``-экранирования; -1 если не закрыта."""
    i = start
    total = len(text)
    while i < total:
        char = text[i]
        if char == "\\" and i + 1 < total:
            i += 2
            continue
        if char == quote:
            return i
        i += 1
    return -1


def _quoted_spans(text: str) -> List[_QuotedSpan]:
    """Границы кавычечных участков одним проходом слева направо.

    Единая точка для всех, кому нужны кавычки: раньше двойные, ANSI-C и одинарные разбирались
    тремя независимыми regex'ами, которые не видели друг друга. Из-за этого ``"`` внутри
    ``'...'`` спаривалась с настоящей закрывающей ``"`` ниже по тексту, и всё между ними
    схлопывалось — вместе с реально исполняемыми командами.

    Незакрытая кавычка тянется до конца текста (``closed=False``): так участок не теряется
    и не даёт следующей кавычке спариться «через» него.
    """
    spans: List[_QuotedSpan] = []
    i = 0
    total = len(text)
    while i < total:
        char = text[i]
        if char == "\\" and i + 1 < total:
            i += 2
            continue
        if char == "$" and text[i + 1:i + 2] == "'":
            kind, close = "$'", _closing_quote(text, i + 2, "'")
        elif char == "'":
            # Внутри ``'...'`` экранирования нет — ближайшая одинарная кавычка и закрывает.
            kind, close = "'", text.find("'", i + 1)
        elif char == '"':
            kind, close = '"', _closing_quote(text, i + 1, '"')
        else:
            i += 1
            continue
        if close < 0:
            spans.append(_QuotedSpan(start=i, end=total, kind=kind, closed=False))
            break
        spans.append(_QuotedSpan(start=i, end=close + 1, kind=kind, closed=True))
        i = close + 1
    return spans


def _span_inner(text: str, span: _QuotedSpan) -> str:
    start = span.start + (2 if span.kind == "$'" else 1)
    return text[start:span.end - 1] if span.closed else text[start:span.end]


def _collapse_quoted_spans(text: str) -> str:
    """Схлопнуть кавычки: ``su""do`` -> ``sudo``, ``$'\\x73u'`` -> ``su``, многословные -> ``""``."""
    out: List[str] = []
    cursor = 0
    for span in _quoted_spans(text):
        out.append(text[cursor:span.start])
        inner = _span_inner(text, span)
        if span.kind == '"':
            out.append(_collapse_double_quoted(inner))
        elif span.kind == "$'":
            out.append(_collapse_ansi_c_quoted(inner))
        else:
            out.append(_collapse_single_quoted(inner))
        cursor = span.end
    out.append(text[cursor:])
    return "".join(out)


def _strip_redirections(words: List[str]) -> List[str]:
    """Убрать перенаправления: токенайзер отдаёт ``>`` и его цель обычными словами.

    Различение «цель отдельным словом» / «цель приклеена» — теми же двумя регулярками,
    что и в ``_command_start`` (определены ниже, обращение происходит в рантайме).
    """
    out: List[str] = []
    i = 0
    total = len(words)
    while i < total:
        if _REDIRECT_EXACT_RE.match(words[i]):
            i += 2  # ``> file``
        elif _REDIRECT_PREFIX_RE.match(words[i]):
            i += 1  # ``>file``, ``2>&1``
        else:
            out.append(words[i])
            i += 1
    return out


def _heredoc_runs_shell(command_line: str) -> bool:
    """Читает ли команда строки тело heredoc как shell-скрипт.

    Разбор переиспользует ``_segment_consumes_shell_stdin`` (определён ниже — обращение
    происходит в рантайме). Отдельная regex-эвристика видела ``bash`` только в начале
    сегмента, поэтому ``timeout 30 bash > out.log <<EOF`` считался обычной записью в файл:
    тело вырезалось до раскрытия кавычек, и спрятанный в нём ``su""do`` не находился.
    """
    return any(
        _segment_consumes_shell_stdin(_strip_redirections(words))
        for words in _scan_shell(command_line).commands
    )


def _mask_quoted_spans(command: str) -> str:
    """Заменить содержимое кавычек на ``x``, сохранив длину, переводы строк и сами кавычки.

    Нужно для поиска ``<<``: без этого ``echo "пример <<EOF" > f`` считался открытием
    heredoc, и всё до ближайшей строки-``EOF`` вырезалось из скана — вместе с настоящими
    командами между ними. Кавычки остаются на месте, потому что маркер бывает закавычен
    (``<<'EOF'``); сам маркер потом берётся из исходной строки по span'у совпадения.

    Состояние кавычек тянется через переводы строк: незакрытая кавычка внутри тела heredoc
    гасит распознавание следующих ``<<``, то есть ошибка синхронизации работает в сторону
    «не вырезать» — текст остаётся в скане.
    """
    out: List[str] = []
    quote = ""
    i = 0
    total = len(command)
    while i < total:
        char = command[i]
        if char == "\\" and i + 1 < total and quote != "'":
            following = command[i + 1]
            out.append("\\")
            out.append(following if following == "\n" else "x")
            i += 2
            continue
        if quote:
            out.append(char if char in (quote, "\n") else "x")
            if char == quote:
                quote = ""
            i += 1
            continue
        if char in "'\"":
            quote = char
        out.append(char)
        i += 1
    return "".join(out)


def _strip_written_heredocs(command: str) -> str:
    """Выбросить heredoc'и, тело которых просто пишется в файл (``cat <<EOF > f``).

    Разбор идёт построчно, а не одним regex с ленивым ``[\\s\\S]*?`` и обратной ссылкой:
    на входе из тысяч незакрытых ``<<EOF`` такой regex перебирал тело до конца текста для
    каждой строки — O(n²), ~1 секунда синхронно в event loop на 20 КБ. Позиции возможных
    закрывающих маркеров считаются один раз, поиск закрытия — бинарным по ним.
    """
    lines = command.split("\n")
    masked_lines = _mask_quoted_spans(command).split("\n")
    marker_lines: Dict[str, List[int]] = {}
    for index, line in enumerate(lines):
        stripped = line.strip(_HEREDOC_LINE_WS)
        if _HEREDOC_MARKER_RE.fullmatch(stripped):
            marker_lines.setdefault(stripped, []).append(index)

    out: List[str] = []
    i = 0
    total = len(lines)
    while i < total:
        # Открывашка ищется в замаскированной строке, а содержимое групп берётся из исходной.
        opening = _HEREDOC_OPEN_RE.match(masked_lines[i])
        if opening is None:
            out.append(lines[i])
            i += 1
            continue
        marker = lines[i][opening.start(3):opening.end(3)]
        candidates = marker_lines.get(marker, ())
        position = bisect_left(candidates, i + 1)
        if position >= len(candidates):
            # Маркер не закрыт — heredoc'а нет, строка идёт как обычная.
            out.append(lines[i])
            i += 1
            continue
        close = candidates[position]
        combined = lines[i][opening.start(1):opening.end(1)] + lines[i][opening.start(4):opening.end(4)]
        if ">" in combined and not _heredoc_runs_shell(combined):
            out.append("")
        else:
            out.extend(lines[i:close + 1])
        i = close + 1
    return "\n".join(out)


def _scannable_command_at_depth(command: str, depth: int, budget: _Budget) -> str:
    stripped = _strip_written_heredocs(command)
    base = _BACKSLASH_ESCAPE_RE.sub(r"\1", _collapse_quoted_spans(stripped))
    if depth >= MAX_RECURSION_DEPTH:
        return base
    executed = _executed_shell_payloads(stripped)
    if not executed:
        return base
    parts = [base]
    for payload in executed:
        if budget.remaining <= 0:
            break
        budget.remaining -= 1
        parts.append(_scannable_command_at_depth(payload, depth + 1, budget))
    return "\n".join(parts)


def scannable_command(command: str) -> str:
    """Развернуть shell-команду в текст, безопасный для regex-сканирования политикой.

    Если вход длиннее ``MAX_SCAN_INPUT_CHARS`` — нормализация пропускается (fail-safe):
    возвращается исходная строка как есть, чтобы raw-сканирование в ``check_command``
    всё равно отработало.

    Раскрытие обёрток дописывает payload к исходному тексту, поэтому результат всегда
    длиннее входа. Патологический вход (тысячи вложенных ``$(``) раздувает его на порядок,
    а паттерны с несколькими ``.*`` уходят на такой строке в backtracking — секунды
    синхронно в event loop. При превышении ``MAX_SCAN_OUTPUT_CHARS`` результат
    отбрасывается: раздутая строка не несёт смысла, которого не было бы в исходной.
    """
    if len(command) > MAX_SCAN_INPUT_CHARS:
        logger.warning(
            "command_scan: вход длиной %d символов превышает лимит %d, нормализация пропущена",
            len(command), MAX_SCAN_INPUT_CHARS,
        )
        return command
    normalized = _scannable_command_at_depth(command, 0, _Budget())
    if len(normalized) > MAX_SCAN_OUTPUT_CHARS:
        logger.warning(
            "command_scan: нормализация раздула вход с %d до %d символов (лимит %d), результат отброшен",
            len(command), len(normalized), MAX_SCAN_OUTPUT_CHARS,
        )
        return command
    return normalized


# ==== Layer 2: посимвольный токенизатор и обёртки ====

_VAR_ASSIGN_RE = re.compile(r"^[A-Za-z_]\w*=", re.ASCII)
_KEYWORD_RE = re.compile(r"^(?:if|then|elif|else|while|until|do|!)$")
# Альтернативы от длинных к коротким: без этого `<<<` разбирался как `<<` с приклеенной
# целью, и цель утекала в разбор отдельным словом.
_REDIRECT_OPERATOR = r"\d*(?:<<<|>>|<<|<>|>&|<&|>|<)"
_REDIRECT_EXACT_RE = re.compile(rf"^{_REDIRECT_OPERATOR}$", re.ASCII)
_REDIRECT_PREFIX_RE = re.compile(rf"^{_REDIRECT_OPERATOR}.+", re.ASCII)
_DASH_C_RE = re.compile(r"^-[^-]*c", re.ASCII)
# bash принимает длинные опции и с одним дефисом, а `^-[^-]*c` видит в `-norc`/`-restricted`
# флаг `-c`. Из-за этого `bash -norc <<EOF ... EOF` считался не читающим stdin, и тело
# heredoc не сканировалось вовсе — при том что реальный bash его исполняет.
_SHELL_LONG_OPTIONS = {
    "debugger", "dump-po-strings", "dump-strings", "help", "init-file", "login",
    "noediting", "noprofile", "norc", "posix", "pretty-print", "protected", "rcfile",
    "restricted", "rpm-requires", "verbose", "version", "wordexp",
}
# Опции bash, забирающие следующее слово как значение (в обоих написаниях дефисов).
_SHELL_VALUE_OPTIONS = ("-O", "-o", "--rcfile", "--init-file", "-rcfile", "-init-file")
_WS_RE = re.compile(r"\s")


def _is_dash_c(arg: str) -> bool:
    """``-c``/``-xc``, но не однодефисная длинная опция bash (``-norc``, ``-restricted``)."""
    return arg[1:] not in _SHELL_LONG_OPTIONS and _DASH_C_RE.match(arg) is not None


def _find_command_substitution(text: str, start: int) -> Optional[Tuple[str, int]]:
    """Найти конец ``$(...)``, начинающегося в ``start``. Возвращает (тело, индекс_после).

    Без закрывающей скобки вызывающий откатывается на один символ и на следующем ``$(``
    сканирует остаток заново — на строке из тысяч несбалансированных ``$(`` это даёт
    квадратичное время. Ранний выход отсекает такой вход: закрыть подстановку без
    единой ``)`` впереди невозможно, поэтому весь линейный проход не нужен.
    """
    if text.find(")", start + 2) < 0:
        return None
    depth = 1
    quote = ""
    j = start + 2
    length = len(text)
    while j < length:
        c = text[j]
        if c == "\\":
            j += 2
            continue
        if quote:
            if c == quote:
                quote = ""
            j += 1
            continue
        if c == "'" or c == '"':
            quote = c
            j += 1
            continue
        if c == "$" and _char_at(text, j + 1) == "(":
            depth += 1
            j += 2
            continue
        if c == ")":
            depth -= 1
            if depth == 0:
                return text[start + 2:j], j + 1
            j += 1
            continue
        j += 1
    return None


def _scan_shell(text: str) -> ShellScan:
    """Посимвольный токенизатор shell-текста: список команд (списков слов) + nested-подстановки."""
    commands: List[List[str]] = []
    nested: List[str] = []
    words: List[str] = []
    i = 0
    length = len(text)

    def flush() -> None:
        nonlocal words
        if words:
            commands.append(words)
        words = []

    while i < length:
        ch = _char_at(text, i)
        if _WS_RE.match(ch):
            if ch == "\n":
                flush()
            i += 1
            continue
        if ch == "#" and not words:
            while i < length and _char_at(text, i) != "\n":
                i += 1
            continue
        if ch in ";|&(){}":
            flush()
            while i < length and _char_at(text, i) in ";|&(){}":
                i += 1
            continue
        word = ""
        word_started = False
        while i < length and not _WS_RE.match(_char_at(text, i)) and (
            _char_at(text, i) not in ";|&(){}"
            or (_char_at(text, i) == "&" and word.endswith(("<", ">")))
        ):
            c = _char_at(text, i)
            if c == "\\":
                if _char_at(text, i + 1) == "\n":
                    i += 2
                elif i + 1 < length:
                    word_started = True
                    word += _char_at(text, i + 1)
                    i += 2
                else:
                    i += 1
                continue
            if c == "'":
                word_started = True
                end = text.find("'", i + 1)
                if end < 0:
                    word += text[i + 1:]
                    i = length
                else:
                    word += text[i + 1:end]
                    i = end + 1
                continue
            if c == "$" and _char_at(text, i + 1) == "'":
                word_started = True
                end = text.find("'", i + 2)
                if end < 0:
                    word += text[i + 2:]
                    i = length
                else:
                    word += _decode_ansi_c(text[i + 2:end])
                    i = end + 1
                continue
            if c == '"':
                word_started = True
                i += 1
                while i < length and _char_at(text, i) != '"':
                    if _char_at(text, i) == "\\" and i + 1 < length:
                        word += _char_at(text, i + 1)
                        i += 2
                    elif _char_at(text, i) == "$" and _char_at(text, i + 1) == "(":
                        sub = _find_command_substitution(text, i)
                        if sub is None:
                            word += _char_at(text, i)
                            i += 1
                        else:
                            body, end = sub
                            nested.append(body)
                            i = end
                    elif _char_at(text, i) == "`":
                        end = text.find("`", i + 1)
                        if end < 0:
                            i += 1
                        else:
                            nested.append(text[i + 1:end])
                            i = end + 1
                    else:
                        word += _char_at(text, i)
                        i += 1
                if _char_at(text, i) == '"':
                    i += 1
                continue
            if c == "$" and _char_at(text, i + 1) == "(":
                word_started = True
                sub = _find_command_substitution(text, i)
                if sub is None:
                    word += _char_at(text, i)
                    i += 1
                else:
                    body, end = sub
                    nested.append(body)
                    i = end
                continue
            if c == "`":
                word_started = True
                end = text.find("`", i + 1)
                if end < 0:
                    i += 1
                else:
                    nested.append(text[i + 1:end])
                    i = end + 1
                continue
            word += c
            word_started = True
            i += 1
        if word_started:
            words.append(word)
    flush()
    return ShellScan(commands=commands, nested=nested)


def _command_start(words: List[str]) -> int:
    i = 0
    n = len(words)
    while i < n:
        word = words[i]
        if _VAR_ASSIGN_RE.match(word) or _KEYWORD_RE.match(word):
            i += 1
        elif _REDIRECT_EXACT_RE.match(word):
            i += 2
        elif _REDIRECT_PREFIX_RE.match(word):
            i += 1
        else:
            break
    return i


def _option_command(words: List[str], start: int, value_options: Set[str]) -> int:
    i = start
    n = len(words)
    while i < n:
        word = words[i]
        if word == "--":
            return i + 1
        if not word.startswith("-") or word == "-":
            return i
        name = word.split("=", 1)[0]
        if name in value_options and "=" not in word:
            i += 1
        i += 1
    return i


def _split_string_payload(args: List[str], split: int) -> Tuple[Optional[str], List[str]]:
    arg = args[split]
    compact = arg.startswith("-S") and len(arg) > 2
    value: Optional[str] = args[split + 1] if split + 1 < len(args) else None
    if "=" in arg:
        value = arg[arg.index("=") + 1:]
    elif compact:
        value = arg[2:]
    rest = args[split + (1 if ("=" in arg or compact) else 2):]
    return value, rest


def _find_split_string_index(args: List[str]) -> int:
    for idx, arg in enumerate(args):
        if arg == "-S" or arg.startswith("-S") or arg == "--split-string" or arg.startswith("--split-string="):
            return idx
    return -1


def _env_split_words(args: List[str]) -> Optional[List[str]]:
    split = _find_split_string_index(args)
    if split < 0:
        return None
    value, rest = _split_string_payload(args, split)
    if value is None:
        return []
    commands = _scan_shell(" ".join([value, *rest])).commands
    return commands[0] if commands else []


def _segment_shell_payloads(words: List[str]) -> List[str]:
    """Развернуть цепочку обёрток (``sudo env nice bash -c ...``) до исполняемого payload.

    Обёртки разбираются циклом, а не рекурсией: каждый шаг отбрасывает как минимум одно
    слово, поэтому цикл конечен, а вход вида ``env A=1`` × 2500 не упирается в лимит
    стека Python. Рекурсивный вариант ронял разбор ``RecursionError`` на такой строке.
    """
    while True:
        start = _command_start(words)
        if start >= len(words):
            return []
        executable = words[start].rsplit("/", 1)[-1]
        args = words[start + 1:]
        if executable in _SHELL_EXECUTABLES:
            j = 0
            n = len(args)
            while j < n:
                if args[j] == "--" or not args[j].startswith("-"):
                    return []
                if args[j] in _SHELL_VALUE_OPTIONS:
                    j += 2
                    continue
                if _is_dash_c(args[j]):
                    return [args[j + 1]] if j + 1 < n else []
                j += 1
            return []
        if executable == "eval":
            return [" ".join(args)] if args else []
        if executable == "env":
            split = _find_split_string_index(args)
            if split >= 0:
                value, rest = _split_string_payload(args, split)
                return [] if value is None else [" ".join([value, *rest])]
            next_i = _option_command(args, 0, {"-u", "--unset", "-C", "--chdir", "-S", "--split-string"})
            while next_i < len(args) and _VAR_ASSIGN_RE.match(args[next_i]):
                next_i += 1
            words = args[next_i:]
            continue
        if executable == "command":
            next_i = 0
            n = len(args)
            while next_i < n:
                if args[next_i] == "--":
                    next_i += 1
                    break
                if args[next_i] in ("-v", "-V"):
                    return []
                if args[next_i] != "-p":
                    break
                next_i += 1
            words = args[next_i:]
            continue
        if executable == "exec":
            words = args[_option_command(args, 0, {"-a"}):]
            continue
        if executable == "sudo":
            words = args[_option_command(args, 0, _SUDO_VALUE_OPTIONS):]
            continue
        if executable == "nice":
            words = args[_option_command(args, 0, {"-n", "--adjustment"}):]
            continue
        if executable == "timeout":
            duration = _option_command(args, 0, {"-s", "--signal", "-k", "--kill-after"})
            words = args[duration + 1:]
            continue
        if executable == "time":
            words = args[_option_command(args, 0, {"-o", "--output", "-f", "--format"}):]
            continue
        if executable == "nohup":
            words = args[_option_command(args, 0, set()):]
            continue
        if executable == "coproc":
            words = args
            continue
        if executable == "xargs":
            next_i = _option_command(
                args, 0,
                {"-a", "--arg-file", "-d", "--delimiter", "-E", "--eof", "-I", "--replace",
                 "-L", "--max-lines", "-n", "--max-args", "-P", "--max-procs", "-s", "--max-chars"},
            )
            words = args[next_i:]
            continue
        return []


def _executed_shell_payloads(input_text: str) -> List[str]:
    scan = _scan_shell(input_text)
    payloads: List[str] = list(scan.nested)
    for words in scan.commands:
        payloads.extend(_segment_shell_payloads(words))
    payloads.extend(_piped_shell_payloads(input_text))
    payloads.extend(_here_string_shell_payloads(input_text))
    payloads.extend(_simple_variable_payloads(input_text))
    return payloads


# ==== Layer 3: трубы, here-strings, простые переменные ====

def _shell_pipelines(input_text: str) -> List[List[str]]:
    pipelines: List[List[str]] = []
    pipeline: List[str] = []
    start = 0
    quote = ""
    length = len(input_text)

    def finish_segment(end: int) -> None:
        segment = input_text[start:end].strip()
        if segment:
            pipeline.append(segment)

    def finish_pipeline(end: int) -> None:
        nonlocal pipeline
        finish_segment(end)
        if len(pipeline) > 1:
            pipelines.append(pipeline)
        pipeline = []

    i = 0
    while i < length:
        char = input_text[i]
        if char == "\\":
            i += 2
            continue
        if quote:
            if char == quote:
                quote = ""
            i += 1
            continue
        if char in "'\"`":
            quote = char
            i += 1
            continue
        if (char == "|" or char == "&") and _char_at(input_text, i + 1) == char:
            finish_pipeline(i)
            i += 1
            start = i + 1
            i += 1
            continue
        if char == "|":
            finish_segment(i)
            if _char_at(input_text, i + 1) == "&":
                i += 1
            start = i + 1
            i += 1
            continue
        if char == ";" or char == "\n" or char == "&":
            finish_pipeline(i)
            start = i + 1
        i += 1
    finish_pipeline(length)
    return pipelines


def _segment_consumes_shell_stdin(words: List[str]) -> bool:
    """Читает ли левая часть пайпа/here-string stdin как shell-скрипт.

    Обёртки разбираются циклом по тем же соображениям, что и в ``_segment_shell_payloads``:
    каждый шаг отбрасывает как минимум одно слово (ветка ``env -S`` — как минимум один
    символ), поэтому цикл конечен, а длинная цепочка ``nice -n1 `` не упирается в лимит
    стека Python.
    """
    split_string_unwraps = 0
    while True:
        start = _command_start(words)
        if start >= len(words):
            return False
        executable = words[start].rsplit("/", 1)[-1]
        args = words[start + 1:]
        if executable in _SHELL_EXECUTABLES:
            i = 0
            n = len(args)
            while i < n:
                arg = args[i]
                if _is_dash_c(arg):
                    return False
                if arg == "-s":
                    return True
                if arg in _SHELL_VALUE_OPTIONS:
                    i += 2
                    continue
                if arg == "--":
                    nxt = args[i + 1] if i + 1 < n else None
                    return nxt is None or nxt in _STDIN_SCRIPTS
                if not arg.startswith("-") or arg == "-":
                    return arg in _STDIN_SCRIPTS
                i += 1
            return True
        if executable == "env":
            split = _env_split_words(args)
            if split is not None:
                if split_string_unwraps >= MAX_SPLIT_STRING_UNWRAPS:
                    return False
                split_string_unwraps += 1
                words = split
                continue
            next_i = _option_command(args, 0, {"-u", "--unset", "-C", "--chdir", "-S", "--split-string"})
            while next_i < len(args) and _VAR_ASSIGN_RE.match(args[next_i]):
                next_i += 1
            words = args[next_i:]
            continue
        if executable == "command":
            words = args[_option_command(args, 0, set()):]
            continue
        if executable == "exec":
            words = args[_option_command(args, 0, {"-a"}):]
            continue
        if executable == "sudo":
            words = args[_option_command(args, 0, _SUDO_VALUE_OPTIONS):]
            continue
        if executable == "nice":
            words = args[_option_command(args, 0, {"-n", "--adjustment"}):]
            continue
        if executable == "timeout":
            duration = _option_command(args, 0, {"-s", "--signal", "-k", "--kill-after"})
            words = args[duration + 1:]
            continue
        if executable == "time":
            words = args[_option_command(args, 0, {"-o", "--output", "-f", "--format"}):]
            continue
        if executable == "nohup":
            words = args[_option_command(args, 0, set()):]
            continue
        if executable == "stdbuf":
            words = args[_option_command(args, 0, {"-i", "--input", "-o", "--output", "-e", "--error"}):]
            continue
        return False


def _literal_producer_payload(words: List[str]) -> Optional[str]:
    """Вытащить литеральный текст, который команда пишет в stdout (``echo``/``printf``/…).

    Обёртки разбираются циклом — см. ``_segment_consumes_shell_stdin``: рекурсивный
    вариант ронял разбор ``RecursionError`` на длинной цепочке обёрток.
    """
    split_string_unwraps = 0
    while True:
        start = _command_start(words)
        if start >= len(words):
            return None
        executable = words[start].rsplit("/", 1)[-1]
        args = words[start + 1:]
        if executable == "command":
            next_i = 0
            n = len(args)
            while next_i < n:
                if args[next_i] == "--":
                    next_i += 1
                    break
                if args[next_i] in ("-v", "-V"):
                    return None
                if args[next_i] != "-p":
                    break
                next_i += 1
            words = args[next_i:]
            continue
        if executable == "builtin":
            first = args[0] if args else None
            if first is not None and first.startswith("-") and first != "--":
                return None
            words = args[1:] if first == "--" else args
            continue
        if executable == "exec":
            words = args[_option_command(args, 0, {"-a"}):]
            continue
        if executable == "env":
            split = _env_split_words(args)
            if split is not None:
                if split_string_unwraps >= MAX_SPLIT_STRING_UNWRAPS:
                    return None
                split_string_unwraps += 1
                words = split
                continue
            next_i = _option_command(args, 0, {"-u", "--unset", "-C", "--chdir"})
            while next_i < len(args) and _VAR_ASSIGN_RE.match(args[next_i]):
                next_i += 1
            words = args[next_i:]
            continue
        if executable == "sudo":
            words = args[_option_command(args, 0, _SUDO_VALUE_OPTIONS):]
            continue
        if executable == "nice":
            words = args[_option_command(args, 0, {"-n", "--adjustment"}):]
            continue
        if executable == "timeout":
            duration = _option_command(args, 0, {"-s", "--signal", "-k", "--kill-after"})
            words = args[duration + 1:]
            continue
        if executable == "time":
            words = args[_option_command(args, 0, {"-o", "--output", "-f", "--format"}):]
            continue
        if executable == "nohup":
            words = args[_option_command(args, 0, set()):]
            continue
        if executable == "stdbuf":
            words = args[_option_command(args, 0, {"-i", "--input", "-o", "--output", "-e", "--error"}):]
            continue
        break

    if args and args[0] == "--":
        args = args[1:]
    if executable == "echo":
        decode_escapes = False
        while args and _ECHO_FLAGS_RE.match(args[0]):
            for option in args[0][1:]:
                if option == "e":
                    decode_escapes = True
                if option == "E":
                    decode_escapes = False
            args = args[1:]
        payload = " ".join(args)
        return _decode_ansi_c(payload) if decode_escapes else payload
    if executable != "printf" or not args:
        return None
    fmt, values = args[0], args[1:]
    value_index_holder = [0]

    def _conv_sub(m: "re.Match[str]") -> str:
        conversion = m.group(1)
        if conversion == "%":
            return "%"
        idx = value_index_holder[0]
        value = values[idx] if idx < len(values) else ""
        value_index_holder[0] += 1
        return _decode_ansi_c(value) if conversion == "b" else value

    rendered = _PRINTF_CONV_RE.sub(_conv_sub, _decode_ansi_c(fmt))
    remainder = values[value_index_holder[0]:]
    return "\n".join([rendered, *remainder, " ".join(args)])


_ECHO_FLAGS_RE = re.compile(r"^-[neE]+$")
_PRINTF_CONV_RE = re.compile(r"%([%sb])")


def _piped_shell_payloads(input_text: str) -> List[str]:
    payloads: List[str] = []
    for pipeline in _shell_pipelines(input_text):
        for i in range(1, len(pipeline)):
            consumer_commands = _scan_shell(pipeline[i]).commands
            consumer = consumer_commands[0] if consumer_commands else None
            if not consumer or not _segment_consumes_shell_stdin(consumer):
                continue
            producer_commands = _scan_shell(pipeline[i - 1]).commands
            producer = producer_commands[-1] if producer_commands else None
            if not producer:
                continue
            payload = _literal_producer_payload(producer)
            if payload:
                payloads.append(payload)
    return payloads


def _here_string_shell_payloads(input_text: str) -> List[str]:
    payloads: List[str] = []
    spaced_parts: List[str] = []
    quote = ""
    i = 0
    length = len(input_text)
    while i < length:
        char = input_text[i]
        if char == "\\":
            spaced_parts.append(input_text[i:i + 2])
            i += 2
            continue
        if quote:
            spaced_parts.append(char)
            if char == quote:
                quote = ""
            i += 1
            continue
        if char in "'\"`":
            quote = char
            spaced_parts.append(char)
            i += 1
            continue
        if input_text.startswith("<<<", i):
            spaced_parts.append(" <<< ")
            i += 3
            continue
        spaced_parts.append(char)
        i += 1
    spaced = "".join(spaced_parts)
    for words in _scan_shell(spaced).commands:
        if "<<<" not in words:
            continue
        redirect = words.index("<<<")
        if redirect <= 0 or not _segment_consumes_shell_stdin(words[:redirect]):
            continue
        if redirect + 1 < len(words):
            payload = words[redirect + 1]
            if payload:
                payloads.append(payload)
    return payloads


_ASSIGN_CAPTURE_RE = re.compile(r"^([A-Za-z_]\w*)=([\w./-]+)$", re.ASCII)
_VAR_REF_RE = re.compile(r"^\$(?:\{([A-Za-z_]\w*)\}|([A-Za-z_]\w*))$", re.ASCII)


def _simple_variable_payloads(input_text: str) -> List[str]:
    values: Dict[str, str] = {}
    payloads: List[str] = []

    def executable_index(words: List[str], offset: int = 0) -> Optional[int]:
        """Индекс исполняемого слова за цепочкой обёрток; цикл вместо рекурсии — см.
        ``_segment_shell_payloads``, длинная цепочка ``env A=1`` иначе исчерпывает стек."""
        while True:
            start = _command_start(words)
            if start >= len(words):
                return None
            executable = words[start].rsplit("/", 1)[-1]
            args = words[start + 1:]
            next_i: Optional[int] = None
            if executable in ("command", "nohup"):
                next_i = _option_command(args, 0, set())
            elif executable == "exec":
                next_i = _option_command(args, 0, {"-a"})
            elif executable == "env":
                next_i = _option_command(args, 0, {"-u", "--unset", "-C", "--chdir", "-S", "--split-string"})
                while next_i < len(args) and _VAR_ASSIGN_RE.match(args[next_i]):
                    next_i += 1
            elif executable == "sudo":
                next_i = _option_command(args, 0, _SUDO_VALUE_OPTIONS)
            elif executable == "nice":
                next_i = _option_command(args, 0, {"-n", "--adjustment"})
            elif executable == "timeout":
                next_i = _option_command(args, 0, {"-s", "--signal", "-k", "--kill-after"}) + 1
            elif executable == "time":
                next_i = _option_command(args, 0, {"-o", "--output", "-f", "--format"})
            elif executable == "stdbuf":
                next_i = _option_command(args, 0, {"-i", "--input", "-o", "--output", "-e", "--error"})
            if next_i is None:
                return offset + start
            offset += start + 1 + next_i
            words = args[next_i:]

    for words in _scan_shell(input_text).commands:
        start = _command_start(words)
        if start >= len(words):
            for word in words:
                match = _ASSIGN_CAPTURE_RE.match(word)
                if match:
                    values[match.group(1)] = match.group(2)
            continue
        index = executable_index(words)
        if index is None:
            continue
        match = _VAR_REF_RE.match(words[index])
        name = (match.group(1) or match.group(2)) if match else None
        value = values.get(name) if name else None
        if value:
            new_words = [*words[:index], value, *words[index + 1:]]
            payloads.append(" ".join(new_words))
    return payloads
