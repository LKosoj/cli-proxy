from __future__ import annotations

import re
from dataclasses import dataclass

from utils.text import strip_ansi


_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_ANSI_ESCAPE_RE = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\)|[@-Z\\-_])")
_DONE_INSTRUCTION = "When you are completely finished, print this exact marker on its own line:"
_DONE_INSTRUCTION_COMPACT = "".join(_DONE_INSTRUCTION.split())
_DONE_INSTRUCTION_PREFIX_COMPACT = "completelyfinish"
_DONE_INSTRUCTION_MARKER_COMPACT = "exactmarker"
_DONE_INSTRUCTION_START_COMPACT = "whenyouarecompletelyfinished"
_DONE_INSTRUCTION_PRINT_MARKER_COMPACT = "printthisexactmarker"
_DONE_INSTRUCTION_CONTINUATION_COMPACTS = {"ownline:", "onitsownline:"}
_CLAUDE_SCREEN_READER_EVENT_RE = re.compile(
    r"(?im)(?:"
    r"(?:^[ \t]*|\$+(?:\(B)?)(?P<role>claude|tool):[ \t]*"
    r"|\$+(?:\(B)?[A-Za-z]+(?:…|\.{3})"
    r")"
)
_CLAUDE_SCREEN_READER_UI_LINE_RE = re.compile(
    r"(?i)^(?:"
    r"[A-Za-z][A-Za-z-]*(?:…|\.{3})(?:\s|$)"
    r"|(?:don't ask|[a-z][a-z -]*permissions) on(?:\s|$)"
    r"|effort:\s*"
    r"|Cooked for(?:\s|$)"
    r"|\(ctrl\+b\s+ctrl\+b\b"
    r"|<{2,3}DONE:"
    r"|\$\s*$"
    r")"
)


@dataclass(frozen=True)
class TmuxParseResult:
    text: str
    complete: bool


def request_marker(request_id: str) -> str:
    return f"<<<CLI_PROXY_REQUEST:{request_id}>>>"


def done_marker(request_id: str) -> str:
    return f"<<<DONE:{request_id}>>>"


def _is_done_line(line: str, request_id: str) -> bool:
    compact = _compact_line(line)
    return request_id in compact and "DONE:" in compact and "<<" in compact and ">>" in compact


def _looks_like_done_line(line: str) -> bool:
    compact = _compact_line(line)
    return "DONE:" in compact and "<<" in compact and ">>" in compact


def _is_request_line(line: str, request_id: str) -> bool:
    compact = _compact_line(line)
    return request_id in compact and "CLI_PROXY_REQUEST:" in compact


def _is_done_instruction_line(line: str) -> bool:
    compact = _compact_line(line).lower()
    return (
        _DONE_INSTRUCTION_COMPACT.lower() in compact
        or (_DONE_INSTRUCTION_PREFIX_COMPACT in compact and _DONE_INSTRUCTION_MARKER_COMPACT in compact)
    )


def _is_done_instruction_fragment_line(line: str) -> bool:
    return _has_done_instruction_fragment(line)


def _is_done_instruction_continuation_line(line: str) -> bool:
    return _compact_line(line).lower() in _DONE_INSTRUCTION_CONTINUATION_COMPACTS


def _compact_line(line: str) -> str:
    return "".join(str(line or "").strip().split())


def _has_done_instruction_fragment(text: str) -> bool:
    compact = _compact_line(text).lower()
    return (
        _DONE_INSTRUCTION_START_COMPACT in compact
        or _DONE_INSTRUCTION_PRINT_MARKER_COMPACT in compact
        or (_DONE_INSTRUCTION_MARKER_COMPACT in compact and "done:" in compact)
    )


def _parse_flat_delta(text: str, request_id: str) -> TmuxParseResult | None:
    request = request_marker(request_id)
    done = done_marker(request_id)
    request_pos = text.rfind(request)
    if request_pos >= 0:
        text = text[request_pos + len(request):]
    if done not in text:
        return None

    first_done = text.find(done)
    if first_done >= 0 and _has_done_instruction_fragment(text[:first_done + len(done)]):
        text = text[first_done + len(done):]
    if done not in text:
        return None

    done_pos = text.rfind(done)
    return TmuxParseResult(text=text[:done_pos].strip(), complete=True)


def _next_nonblank_index(lines: list[str], start: int) -> int | None:
    for idx in range(start, len(lines)):
        if str(lines[idx]).strip():
            return idx
    return None


def _has_later_response_line(lines: list[str], start: int) -> bool:
    for idx in range(start, len(lines)):
        raw = str(lines[idx]).strip()
        if raw and not _looks_like_done_line(raw):
            return True
    return False


def _latest_echo_guard_end_index(lines: list[str]) -> int | None:
    latest = None
    for idx, line in enumerate(lines):
        if not _is_done_instruction_line(line):
            continue
        marker_idx = _next_nonblank_index(lines, idx + 1)
        if marker_idx is None or not _looks_like_done_line(lines[marker_idx]):
            continue
        if _has_later_response_line(lines, marker_idx + 1):
            latest = marker_idx
    return latest


def build_prompt_with_markers(prompt: str, request_id: str, *, multiline: bool = True) -> str:
    if not multiline:
        return (
            f"{request_marker(request_id)} "
            f"{prompt.rstrip()} "
            "When you are completely finished, print this exact marker on a separate final line: "
            f"{done_marker(request_id)}\n"
        )
    return (
        f"{request_marker(request_id)}\n"
        f"{prompt.rstrip()}\n\n"
        "When you are completely finished, print this exact marker on its own line:\n"
        f"{done_marker(request_id)}\n"
    )


def normalize_terminal_text(text: str) -> str:
    stripped = _ANSI_ESCAPE_RE.sub("", str(text or ""))
    stripped = strip_ansi(stripped)
    stripped = stripped.replace("\r\n", "\n").replace("\r", "\n")
    stripped = _CONTROL_RE.sub("", stripped)
    return stripped


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


def parse_tmux_delta(
    delta: str,
    request_id: str,
    *,
    claude_screen_reader: bool = False,
) -> TmuxParseResult:
    text = normalize_terminal_text(delta)
    done = done_marker(request_id)
    flat = _parse_flat_delta(text, request_id)
    if flat is not None:
        if not claude_screen_reader:
            return flat
        return TmuxParseResult(
            text=_extract_claude_screen_reader_message(flat.text),
            complete=flat.complete,
        )

    raw_lines = text.splitlines()
    request_indexes = [idx for idx, line in enumerate(raw_lines) if _is_request_line(line, request_id)]
    if request_indexes:
        request_idx = request_indexes[-1]
        request = request_marker(request_id)
        request_line = raw_lines[request_idx]
        if request in request_line:
            suffix = request_line.split(request, 1)[1]
            raw_lines = ([suffix] if suffix.strip() else []) + raw_lines[request_idx + 1:]
        else:
            raw_lines = raw_lines[request_idx + 1:]
    echo_seen = False
    echo_guard_end = _latest_echo_guard_end_index(raw_lines)
    if echo_guard_end is not None:
        raw_lines = raw_lines[echo_guard_end + 1:]
        while raw_lines and (not raw_lines[0].strip() or _looks_like_done_line(raw_lines[0])):
            raw_lines = raw_lines[1:]
        echo_seen = True

    lines = []
    complete = False
    skip_echo_done_marker = False
    for line in raw_lines:
        raw = line.rstrip()
        if _is_request_line(raw, request_id):
            lines = []
            skip_echo_done_marker = False
            continue
        if _is_done_instruction_line(raw):
            if not echo_seen:
                lines = []
                skip_echo_done_marker = True
                echo_seen = True
                continue
        if skip_echo_done_marker:
            if not raw.strip() or _is_done_instruction_continuation_line(raw):
                continue
            if _looks_like_done_line(raw):
                skip_echo_done_marker = False
                echo_seen = True
                continue
            skip_echo_done_marker = False
        if raw == done or _is_done_line(raw, request_id):
            if not echo_seen and any(_is_done_instruction_fragment_line(line) for line in [*lines, raw]):
                lines = []
                echo_seen = True
                skip_echo_done_marker = False
                continue
            complete = True
            break
        lines.append(raw)

    cleaned = "\n".join(lines).strip()
    if claude_screen_reader:
        cleaned = _extract_claude_screen_reader_message(cleaned)
    return TmuxParseResult(text=cleaned, complete=complete)
