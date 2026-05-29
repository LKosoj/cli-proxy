from __future__ import annotations

import html
import logging
import os
import re
import tempfile
from base64 import urlsafe_b64encode
from typing import Dict, List, Optional

import requests
from tg.markdown import to_markdown_v2
from utils.text import normalize_text


logger = logging.getLogger(__name__)


_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[mK]")
_MERMAID_BLOCK_RE = re.compile(r"```mermaid\s*\n(.*?)\n```", re.DOTALL | re.IGNORECASE)
_HTML_TAG_RE = re.compile(r"(<[^>]+>)")

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


def render_html(text: str, theme_colors: Optional[Dict[str, str]] = None, fragment: bool = False) -> str:
    return ansi_to_html(str(text or ""), theme_colors=theme_colors, fragment=fragment)


def render_markdown(text: str) -> str:
    return to_markdown_v2(str(text or ""))


def make_html_file(html_text: str, prefix: str) -> str:
    fd, path = tempfile.mkstemp(prefix=f"{str(prefix or 'html')}-", suffix=".html")
    with os.fdopen(fd, "w", encoding="utf-8") as file_obj:
        file_obj.write(str(html_text or ""))
    return path


def ansi_to_html(text: str, theme_colors: Optional[Dict[str, str]] = None, fragment: bool = False) -> str:
    cleaned = normalize_text(text, strip_ansi=False)
    rendered = _render_mermaid_blocks(cleaned)
    html_body = _markdown_to_html(rendered)
    html_body = _apply_ansi_to_html(html_body)
    if fragment:
        return _inline_styles(html_body, theme_colors=theme_colors)
    return _wrap_html(html_body, theme_colors=theme_colors)


def _resolve_theme(theme_colors: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    defaults = {
        "text_primary": "#111",
        "bg_primary": "#fff",
        "bg_secondary": "#f6f8fa",
        "bg_tertiary": "#f6f8fa",
        "border": "#ddd",
        "text_secondary": "#555",
    }
    if theme_colors:
        for key in defaults:
            if key in theme_colors:
                defaults[key] = theme_colors[key]
    return defaults


def _inline_styles(body: str, theme_colors: Optional[Dict[str, str]] = None) -> str:
    import re as regex

    colors = _resolve_theme(theme_colors)
    text_color = colors["text_primary"]
    mono = "ui-monospace,SFMono-Regular,Consolas,Monaco,Menlo,monospace"

    body = regex.sub(
        r"<(h[1-6])([^>]*)>",
        lambda match: (
            f"<{match.group(1)}{match.group(2)} style='background:transparent;color:{text_color};'>"
        ),
        body,
    )
    body = regex.sub(
        r"<p([^>]*)>",
        lambda match: f"<p{match.group(1)} style='background:transparent;color:{text_color};'>",
        body,
    )
    body = regex.sub(
        r"<(ul|ol)([^>]*)>",
        lambda match: (
            f"<{match.group(1)}{match.group(2)} style='background:transparent;color:{text_color};"
            "padding-left:24px;'>"
        ),
        body,
    )
    body = regex.sub(
        r"<li([^>]*)>",
        lambda match: f"<li{match.group(1)} style='background:transparent;color:{text_color};'>",
        body,
    )
    border = colors["border"]
    body = regex.sub(
        r"<hr([^>]*)>",
        lambda match: f"<hr{match.group(1)} style='border:none;border-top:1px solid {border};'>",
        body,
    )
    body = regex.sub(
        r"<table([^>]*)>",
        lambda match: (
            f"<table{match.group(1)} style='border-collapse:collapse;margin:12px 0;"
            f"background:transparent;color:{text_color};'>"
        ),
        body,
    )
    body = regex.sub(
        r"<pre>",
        (
            f"<pre style='background:{colors['bg_tertiary']};padding:12px;border-radius:6px;"
            f"overflow:auto;font-family:{mono};color:{text_color};'>"
        ),
        body,
    )
    body = regex.sub(
        r"<code>",
        (
            f"<code style='background:{colors['bg_tertiary']};padding:2px 4px;border-radius:4px;"
            f"font-family:{mono};color:{text_color};'>"
        ),
        body,
    )
    body = regex.sub(
        r"<th(?P<rest>[^>]*)>",
        lambda match: (
            f"<th{match.group('rest')} style='background:{colors['bg_secondary']};"
            f"border:1px solid {colors['border']};padding:6px 10px;vertical-align:top;color:{text_color};'>"
        ),
        body,
    )
    body = regex.sub(
        r"<td(?P<rest>[^>]*)>",
        lambda match: (
            f"<td{match.group('rest')} style='border:1px solid {colors['border']};"
            f"padding:6px 10px;vertical-align:top;background:transparent;color:{text_color};'>"
        ),
        body,
    )
    body = regex.sub(
        r"<blockquote>",
        (
            f"<blockquote style='border-left:4px solid {colors['border']};"
            f"padding-left:12px;color:{colors['text_secondary']};background:transparent;'>"
        ),
        body,
    )
    return body


def _wrap_html(body: str, theme_colors: Optional[Dict[str, str]] = None) -> str:
    colors = _resolve_theme(theme_colors)
    return (
        "<!doctype html>\n"
        "<html><head><meta charset=\"utf-8\">"
        "<style>"
        "body{font-family:system-ui,-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;"
        f"line-height:1.5;color:{colors['text_primary']};background:{colors['bg_primary']};padding:16px;}}"
        "pre,code{font-family:ui-monospace,SFMono-Regular,Consolas,Monaco,Menlo,monospace;}"
        f"pre{{background:{colors['bg_tertiary']};padding:12px;border-radius:6px;overflow:auto;}}"
        f"code{{background:{colors['bg_tertiary']};padding:2px 4px;border-radius:4px;}}"
        "table{border-collapse:collapse;margin:12px 0;}"
        f"th,td{{border:1px solid {colors['border']};padding:6px 10px;vertical-align:top;}}"
        f"th{{background:{colors['bg_secondary']};}}"
        f"blockquote{{border-left:4px solid {colors['border']};padding-left:12px;color:{colors['text_secondary']};}}"
        "ul,ol{padding-left:24px;}"
        ".mermaid-diagram{margin:12px 0;}"
        ".mermaid-diagram svg{max-width:100%;height:auto;}"
        "</style></head><body>"
        f"{body}"
        "</body></html>"
    )


def _markdown_to_html(text: str) -> str:
    from markdown_it import MarkdownIt

    md = MarkdownIt("commonmark", {"html": True, "linkify": True, "breaks": True})
    md = md.enable("table").enable("strikethrough")
    try:
        from mdit_py_plugins.tasklists import tasklists_plugin

        md = md.use(tasklists_plugin, enabled=True)
    except Exception as exc:
        logger.info("tasklists plugin unavailable for html rendering: %s", exc)
    return md.render(text)


def _render_mermaid_blocks(text: str) -> str:
    def replacer(match: re.Match) -> str:
        source = match.group(1).strip()
        svg = _render_mermaid_svg(source)
        if not svg:
            return match.group(0)
        return f"<div class=\"mermaid-diagram\">{svg}</div>"

    return _MERMAID_BLOCK_RE.sub(replacer, text)


def _render_mermaid_svg(source: str) -> Optional[str]:
    if not source:
        return None
    payload = urlsafe_b64encode(source.encode("utf-8")).decode("ascii").rstrip("=")
    url = f"https://mermaid.ink/svg/{payload}"
    try:
        response = requests.get(url, timeout=10)
    except Exception:
        return None
    if not response.ok:
        return None
    text = response.text.strip()
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
            match = re.match(r"^</?\s*([a-zA-Z0-9]+)", part)
            tag = match.group(1).lower() if match else ""
            allowed = {
                "p",
                "br",
                "pre",
                "code",
                "em",
                "strong",
                "del",
                "blockquote",
                "ul",
                "ol",
                "li",
                "h1",
                "h2",
                "h3",
                "h4",
                "h5",
                "h6",
                "table",
                "thead",
                "tbody",
                "tr",
                "th",
                "td",
                "a",
                "hr",
                "input",
                "div",
                "svg",
                "g",
                "path",
                "circle",
                "rect",
                "text",
                "tspan",
                "defs",
                "style",
                "polygon",
                "polyline",
                "line",
                "marker",
                "clippath",
                "lineargradient",
                "radialgradient",
                "stop",
            }
            if tag and tag in allowed:
                out.append(part)
            else:
                out.append(html.escape(part, quote=False))
        else:
            out.append(_ansi_to_html_fragment(part))
    return "".join(out)


def _ansi_to_html_fragment(text: str) -> str:
    def _esc(value: str) -> str:
        value = value or ""
        for _ in range(2):
            decoded = html.unescape(value)
            if decoded == value:
                break
            value = decoded
        value = value.replace("\xa0", " ")
        return html.escape(value, quote=False)

    if "\x1b[" not in text:
        return _esc(text)
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
        chunk = text[idx:match.start()]
        if chunk:
            out.append(_esc(chunk))
        codes = match.group(0)[2:-1] or "0"
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
        out.append(_esc(tail))
    if open_span:
        out.append("</span>")
    return "".join(out)
