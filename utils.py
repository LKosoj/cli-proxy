import html
import os
import re
import tempfile
from typing import List, Optional, Tuple

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[mK]")
_LOOSE_ANSI_RE = re.compile(r"\[(?:\d{1,3};)*\d{1,3}m")
_TICK_OR_TIME_RE = re.compile(
    r"\b\d{2}:\d{2}:\d{2}\b|\b\d{1,6}\s*(?:s|sec|сек)\b",
    re.IGNORECASE,
)


def strip_ansi(text: str) -> str:
    text = _ANSI_RE.sub("", text)
    return _LOOSE_ANSI_RE.sub("", text)


def has_ansi(text: str) -> bool:
    return _ANSI_RE.search(text) is not None


def extract_tick_tokens(text: str) -> List[str]:
    cleaned = strip_ansi(text)
    return [m.group(0) for m in _TICK_OR_TIME_RE.finditer(cleaned)]


def ansi_to_html(text: str) -> str:
    from ansi2html import Ansi2HTMLConverter

    if has_ansi(text):
        conv = Ansi2HTMLConverter(inline=True, scheme="xterm")
        body = conv.convert(text, full=False)
    else:
        body = html.escape(strip_ansi(text)).replace("\n", "<br>")
    return (
        "<!doctype html>\n"
        "<html><head><meta charset=\"utf-8\"></head>"
        "<body><pre style=\"white-space: pre-wrap;\">"
        f"{body}"
        "</pre></body></html>"
    )


def build_command(
    cmd_template: List[str],
    prompt: str,
    resume: Optional[str] = None,
    image: Optional[str] = None,
) -> Tuple[List[str], bool]:
    replaced = False
    cmd: List[str] = []
    skip_next = False
    skip_continue = resume is not None
    for part in cmd_template:
        if skip_next:
            skip_next = False
            continue
        if skip_continue and part == "--continue":
            continue
        if "{resume}" in part:
            if resume is None:
                skip_next = part == "{resume}"
                continue
            cmd.append(part.replace("{resume}", resume))
            continue
        if "{image}" in part:
            if image is None:
                if part == "{image}":
                    continue
                cmd.append(part.replace("{image}", ""))
                continue
            cmd.append(part.replace("{image}", image))
            continue
        if part == "--resume" and resume is None:
            skip_next = True
            continue
        if "{prompt}" in part:
            cmd.append(part.replace("{prompt}", prompt))
            replaced = True
        else:
            cmd.append(part)
    use_stdin = not replaced
    return cmd, use_stdin


def detect_prompt_regex(lines: List[str]) -> Optional[str]:
    # Use last non-empty line; if it repeats in tail, treat as prompt.
    cleaned = [strip_ansi(l).rstrip("\n") for l in lines]
    cleaned = [l for l in cleaned if l.strip()]
    if not cleaned:
        return None
    tail = cleaned[-6:]
    candidate = tail[-1]
    if len(candidate) > 80:
        return None
    occurrences = sum(1 for l in tail if l == candidate)
    if occurrences >= 2:
        return re.escape(candidate) + r"\s*$"
    return None


def detect_resume_regex(text: str) -> Optional[str]:
    cleaned = strip_ansi(text)
    patterns = [
        (r'\"thread_id\"\\s*:\\s*\"([^\"]+)\"', r'\"thread_id\"\\s*:\\s*\"([^\"]+)\"'),
        (r'\"conversation_id\"\\s*:\\s*\"([^\"]+)\"', r'\"conversation_id\"\\s*:\\s*\"([^\"]+)\"'),
        (r'\"session_id\"\\s*:\\s*\"([^\"]+)\"', r'\"session_id\"\\s*:\\s*\"([^\"]+)\"'),
        (r'resume\\s*id\\s*[:=]\\s*([A-Za-z0-9_-]+)', r'resume\\s*id\\s*[:=]\\s*([A-Za-z0-9_-]+)'),
    ]
    import re

    for pattern, regex in patterns:
        if re.search(pattern, cleaned):
            return regex
    return None


def make_html_file(html_text: str, prefix: str) -> str:
    fd, path = tempfile.mkstemp(prefix=f"{prefix}-", suffix=".html")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(html_text)
    return path


def build_preview(text: str, max_chars: int) -> str:
    plain = strip_ansi(text)
    if len(plain) <= max_chars:
        return plain
    return plain[:max_chars]


def escape_html_text(text: str) -> str:
    return html.escape(text)


def is_within_root(path: str, root: str) -> bool:
    try:
        real_path = os.path.realpath(path)
        real_root = os.path.realpath(root)
        return os.path.commonpath([real_path, real_root]) == real_root
    except Exception:
        return False


def resolve_env_value(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    return os.path.expandvars(value)
