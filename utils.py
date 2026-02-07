import html
import os
import re
import tempfile
from base64 import urlsafe_b64encode
from typing import List, Optional, Tuple

import requests

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[mK]")
_LOOSE_ANSI_RE = re.compile(r"\[(?:\d{1,3};)*\d{1,3}m")
_TICK_OR_TIME_RE = re.compile(
    r"\b\d{2}:\d{2}:\d{2}\b|\b\d{1,6}\s*(?:s|sec|сек)\b",
    re.IGNORECASE,
)
_MERMAID_BLOCK_RE = re.compile(r"```mermaid\s*\n(.*?)\n```", re.DOTALL | re.IGNORECASE)
_HTML_TAG_RE = re.compile(r"(<[^>]+>)")
_MCP_LINE_RE = re.compile(r"^mcp:\s+", re.IGNORECASE)

_ANSI_FG_COLORS = {
    30: "#000000",
    31: "#cc0000",
    32: "#00aa00",
    33: "#aa8800",
    34: "#0000cc",
    35: "#aa00aa",
    36: "#00aaaa",
    37: "#cccccc",
    90: "#555555",
    91: "#ff4444",
    92: "#44ff44",
    93: "#ffff44",
    94: "#4444ff",
    95: "#ff44ff",
    96: "#44ffff",
    97: "#ffffff",
}


def sandbox_root(workdir: str) -> str:
    return os.path.join(workdir, "_sandbox")


def sandbox_shared_dir(workdir: str) -> str:
    return os.path.join(sandbox_root(workdir), "_shared")


def sandbox_session_dir(workdir: str, session_id: str) -> str:
    return os.path.join(sandbox_root(workdir), "sessions", session_id)


def strip_ansi(text: str) -> str:
    text = _ANSI_RE.sub("", text)
    return _LOOSE_ANSI_RE.sub("", text)


def has_ansi(text: str) -> bool:
    return _ANSI_RE.search(text) is not None


def extract_tick_tokens(text: str) -> List[str]:
    cleaned = strip_ansi(text)
    return [m.group(0) for m in _TICK_OR_TIME_RE.finditer(cleaned)]


def ansi_to_html(text: str) -> str:
    cleaned = normalize_text(text, strip_ansi=False)
    rendered = _render_mermaid_blocks(cleaned)
    html_body = _markdown_to_html(rendered)
    html_body = _apply_ansi_to_html(html_body)
    return _wrap_html(html_body)


def _wrap_html(body: str) -> str:
    return (
        "<!doctype html>\n"
        "<html><head><meta charset=\"utf-8\">"
        "<style>"
        "body{font-family:system-ui,-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;"
        "line-height:1.5;color:#111;background:#fff;padding:16px;}"
        "pre,code{font-family:ui-monospace,SFMono-Regular,Consolas,Monaco,Menlo,monospace;}"
        "pre{background:#f6f8fa;padding:12px;border-radius:6px;overflow:auto;}"
        "code{background:#f6f8fa;padding:2px 4px;border-radius:4px;}"
        "table{border-collapse:collapse;margin:12px 0;}"
        "th,td{border:1px solid #ddd;padding:6px 10px;vertical-align:top;}"
        "th{background:#f3f4f6;}"
        "blockquote{border-left:4px solid #e5e7eb;padding-left:12px;color:#555;}"
        "ul,ol{padding-left:24px;}"
        ".mermaid-diagram{margin:12px 0;}"
        ".mermaid-diagram svg{max-width:100%;height:auto;}"
        "</style></head><body>"
        f"{body}"
        "</body></html>"
    )


def _markdown_to_html(text: str) -> str:
    from markdown_it import MarkdownIt
    from mdit_py_plugins.tasklists import tasklists_plugin

    md = (
        MarkdownIt("commonmark", {"html": True, "linkify": True, "breaks": True})
        .enable("table")
        .enable("strikethrough")
        .use(tasklists_plugin, enabled=True)
    )
    return md.render(text)


def _render_mermaid_blocks(text: str) -> str:
    def replacer(match: re.Match) -> str:
        source = match.group(1).strip()
        svg = _render_mermaid_svg(source)
        if not svg:
            return match.group(0)
        return f"<div class=\"mermaid-diagram\">{svg}</div>"

    return _MERMAID_BLOCK_RE.sub(replacer, text)


def normalize_text(text: str, strip_ansi: bool = True) -> str:
    if not text:
        return text
    if strip_ansi:
        text = strip_ansi_codes(text)
    text = _remove_mcp_lines(text)
    return _dedupe_repeated_blocks(text)


def strip_ansi_codes(text: str) -> str:
    return strip_ansi(text)


def _remove_mcp_lines(text: str) -> str:
    lines = text.splitlines()
    first_mcp_idx = None
    startup_idx = None
    for idx, line in enumerate(lines):
        stripped = line.strip()
        if first_mcp_idx is None and _MCP_LINE_RE.match(stripped):
            first_mcp_idx = idx
        if first_mcp_idx is not None and stripped.lower().startswith("mcp startup:"):
            startup_idx = idx
            break
    if first_mcp_idx is None or startup_idx is None:
        return text
    filtered: list[str] = []
    for idx, line in enumerate(lines):
        if first_mcp_idx <= idx < startup_idx:
            if _MCP_LINE_RE.match(line.strip()):
                continue
        filtered.append(line)
    return "\n".join(filtered)


def _dedupe_repeated_blocks(text: str) -> str:
    lines = text.splitlines()
    total = len(lines)
    if total == 0:
        return text
    min_block = 1
    changed = True
    while changed:
        changed = False
        total = len(lines)
        for i in range(total - min_block):
            if lines[i].strip() == "":
                continue
            j = i + 1
            while j <= total - min_block:
                if lines[j].strip() == "":
                    j += 1
                    continue
                k = 0
                while i + k < total and j + k < total and lines[i + k] == lines[j + k]:
                    k += 1
                if k >= min_block:
                    del lines[j : j + k]
                    changed = True
                    total = len(lines)
                    break
                j += 1
            if changed:
                break
    return "\n".join(lines)


def _render_mermaid_svg(source: str) -> Optional[str]:
    if not source:
        return None
    payload = urlsafe_b64encode(source.encode("utf-8")).decode("ascii").rstrip("=")
    url = f"https://mermaid.ink/svg/{payload}"
    try:
        resp = requests.get(url, timeout=10)
    except Exception:
        return None
    if not resp.ok:
        return None
    text = resp.text.strip()
    if not text.startswith("<svg"):
        return None
    return text


def _apply_ansi_to_html(html_text: str) -> str:
    parts = _HTML_TAG_RE.split(html_text)
    out: List[str] = []
    for part in parts:
        if not part:
            continue
        if part.startswith("<") and part.endswith(">"):
            out.append(part)
        else:
            out.append(_ansi_to_html_fragment(part))
    return "".join(out)


def _ansi_to_html_fragment(text: str) -> str:
    if "\x1b[" not in text:
        return html.escape(text)
    out: List[str] = []
    fg_color: Optional[str] = None
    bold = False
    open_span = False

    def style() -> Optional[str]:
        styles = []
        if fg_color:
            styles.append(f"color:{fg_color}")
        if bold:
            styles.append("font-weight:600")
        if not styles:
            return None
        return ";".join(styles)

    def update_span() -> None:
        nonlocal open_span
        current = style()
        if open_span:
            out.append("</span>")
            open_span = False
        if current:
            out.append(f"<span style=\"{current}\">")
            open_span = True

    idx = 0
    for match in _ANSI_RE.finditer(text):
        chunk = text[idx : match.start()]
        if chunk:
            out.append(html.escape(chunk))
        codes = match.group(0)[2:-1]
        if codes == "":
            codes = "0"
        for code_str in codes.split(";"):
            if not code_str:
                continue
            try:
                code = int(code_str)
            except ValueError:
                continue
            if code == 0:
                fg_color = None
                bold = False
            elif code == 1:
                bold = True
            elif code == 22:
                bold = False
            elif code == 39:
                fg_color = None
            elif code in _ANSI_FG_COLORS:
                fg_color = _ANSI_FG_COLORS[code]
        update_span()
        idx = match.end()
    tail = text[idx:]
    if tail:
        out.append(html.escape(tail))
    if open_span:
        out.append("</span>")
    return "".join(out)


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
    cleaned = [strip_ansi(line).rstrip("\n") for line in lines]
    cleaned = [line for line in cleaned if line.strip()]
    if not cleaned:
        return None
    tail = cleaned[-6:]
    candidate = tail[-1]
    if len(candidate) > 80:
        return None
    occurrences = sum(1 for line in tail if line == candidate)
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
    # Делает обрезку явной: иначе пользователю кажется, что агент "не дописал" ответ.
    suffix = "\n...(обрезано)..."
    if max_chars <= len(suffix) + 20:
        return plain[:max_chars]
    return plain[: max_chars - len(suffix)] + suffix


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
