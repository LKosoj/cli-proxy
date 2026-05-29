from __future__ import annotations

import html
import re
from typing import Dict, List

_UNSAFE_HTML_RE = re.compile(
    r"(?is)<\s*(script|iframe|object|embed|form|style)\b|on[a-z]+\s*=|javascript:"
)
_TABLE_SEPARATOR_RE = re.compile(r"^\s*\|?(?:\s*:?-{3,}:?\s*\|)+\s*:?-{3,}:?\s*\|?\s*$")


def _collect_pipe_blocks(lines: List[str]) -> List[tuple[int, int]]:
    blocks: List[tuple[int, int]] = []
    start = None
    in_fence = False
    for idx, line in enumerate(lines):
        if line.strip().startswith("```"):
            in_fence = not in_fence
            if start is not None:
                if idx - start >= 2:
                    blocks.append((start, idx))
                start = None
            continue
        if in_fence:
            if start is not None:
                start = None
            continue
        if "|" in line:
            if start is None:
                start = idx
            continue
        if start is not None:
            if idx - start >= 2:
                blocks.append((start, idx))
            start = None
    if start is not None and len(lines) - start >= 2:
        blocks.append((start, len(lines)))
    return blocks


def _table_column_count(line: str) -> int:
    stripped = line.strip().strip("|")
    if not stripped:
        return 0
    return len([cell for cell in stripped.split("|")])


def lint_markdown_document(text: str) -> Dict[str, List[str]]:
    issues: List[str] = []
    raw = str(text or "")
    lines = raw.splitlines()
    fence_lines = [line for line in lines if line.strip().startswith("```")]
    if len(fence_lines) % 2 != 0:
        issues.append("unbalanced_fenced_code_blocks")
    if _UNSAFE_HTML_RE.search(raw):
        issues.append("unsafe_raw_html")
    for start, end in _collect_pipe_blocks(lines):
        block = lines[start:end]
        if len(block) < 2:
            continue
        if not _TABLE_SEPARATOR_RE.match(block[1]):
            issues.append("malformed_markdown_table")
            continue
        header_cols = _table_column_count(block[0])
        if header_cols < 2:
            issues.append("malformed_markdown_table")
            continue
        for row in block[2:]:
            if _table_column_count(row) != header_cols:
                issues.append("malformed_markdown_table")
                break
    return {"issues": issues}


def repair_markdown_document(text: str) -> tuple[str, List[str]]:
    raw = str(text or "")
    issues = lint_markdown_document(raw).get("issues") or []
    repaired = raw
    applied: List[str] = []
    if "unbalanced_fenced_code_blocks" in issues:
        repaired = repaired.rstrip() + "\n```\n"
        applied.append("closed_unbalanced_fenced_code_blocks")
    if "unsafe_raw_html" in issues:
        repaired_lines: List[str] = []
        for line in repaired.splitlines():
            if _UNSAFE_HTML_RE.search(line):
                repaired_lines.append(html.escape(line, quote=False))
            else:
                repaired_lines.append(line)
        repaired = "\n".join(repaired_lines)
        applied.append("escaped_unsafe_raw_html")
    if "malformed_markdown_table" in issues:
        source_lines = repaired.splitlines()
        blocks = _collect_pipe_blocks(source_lines)
        if blocks:
            out_lines: List[str] = []
            cursor = 0
            for start, end in blocks:
                out_lines.extend(source_lines[cursor:start])
                block = source_lines[start:end]
                if len(block) >= 2 and _TABLE_SEPARATOR_RE.match(block[1]):
                    header = [cell.strip() for cell in block[0].strip().strip("|").split("|")]
                    row_lines: List[str] = []
                    malformed = False
                    for row in block[2:]:
                        cells = [cell.strip() for cell in row.strip().strip("|").split("|")]
                        if len(cells) != len(header):
                            malformed = True
                            break
                        row_lines.append("- " + "; ".join(f"{header[idx]}: {cell}" for idx, cell in enumerate(cells)))
                    if malformed:
                        out_lines.extend(
                            [
                                "- " + " | ".join(part.strip() for part in line.strip().strip("|").split("|"))
                                for line in block
                                if line.strip()
                            ]
                        )
                    else:
                        out_lines.extend(row_lines or ["- " + " | ".join(header)])
                else:
                    out_lines.extend(
                        [
                            "- " + " | ".join(part.strip() for part in line.strip().strip("|").split("|"))
                            for line in block
                            if line.strip()
                        ]
                    )
                cursor = end
            out_lines.extend(source_lines[cursor:])
            repaired = "\n".join(out_lines)
            applied.append("converted_malformed_markdown_tables_to_lists")
    return repaired, applied


def render_document_lint_report(*, issues: List[str], repairs: List[str]) -> str:
    lines = ["# Document Lint", ""]
    lines.extend(["## Issues", ""])
    if issues:
        for item in issues:
            lines.append(f"- {str(item).strip()}")
    else:
        lines.append("- none")
    lines.extend(["", "## Repairs", ""])
    if repairs:
        for item in repairs:
            lines.append(f"- {str(item).strip()}")
    else:
        lines.append("- none")
    return "\n".join(lines).strip()
