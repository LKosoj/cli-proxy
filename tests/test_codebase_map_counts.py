from __future__ import annotations

import re
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MAP_DIR = ROOT / ".cli-proxy" / ".codebase_map"

COUNTED_SOURCE_GLOBS = (
    "tests/**",
    "modes/**",
    "app/**",
    "agent/**",
    "desktop/**",
    "tg/**",
    "miniapp/**",
    "sessions/**",
    "utils/**",
)


def _rg_file_count(source_glob: str) -> int:
    assert source_glob.endswith("/**")
    base_path = source_glob[:-3].rstrip("/")
    result = subprocess.run(
        ["rg", "--files", base_path],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode in (0, 1), result.stderr
    return len([line for line in result.stdout.splitlines() if line.strip()])


def _expected_counts() -> dict[str, int]:
    return {source_glob: _rg_file_count(source_glob) for source_glob in COUNTED_SOURCE_GLOBS}


def _read_map_file(relative_path: str) -> str:
    return (MAP_DIR / relative_path).read_text(encoding="utf-8")


def _index_counts() -> dict[str, int]:
    text = _read_map_file("INDEX.md")
    pattern = re.compile(
        r"^- \[[^\]]+\]\([^)]+\) - files: (?P<count>\d+), "
        r"source_glob: `(?P<source_glob>[^`]+)`$",
        re.MULTILINE,
    )
    return {
        match.group("source_glob"): int(match.group("count"))
        for match in pattern.finditer(text)
        if match.group("source_glob") in COUNTED_SOURCE_GLOBS
    }


def _structure_counts() -> dict[str, int]:
    text = _read_map_file("STRUCTURE.md")
    pattern = re.compile(r"^\| `(?P<area>[^`]+/)` \| (?P<count>\d+) \|", re.MULTILINE)
    return {
        f"{match.group('area')}**": int(match.group("count"))
        for match in pattern.finditer(text)
        if f"{match.group('area')}**" in COUNTED_SOURCE_GLOBS
    }


def _node_counts() -> dict[str, int]:
    counts: dict[str, int] = {}
    pattern = re.compile(
        r"^- Current files: (?P<count>\d+) under `(?P<source_glob>[^`]+)` as of last review\.$",
        re.MULTILINE,
    )
    for source_glob in COUNTED_SOURCE_GLOBS:
        node_name = source_glob[:-3].rstrip("/")
        text = _read_map_file(f"nodes/{node_name}.md")
        match = pattern.search(text)
        assert match is not None, f"nodes/{node_name}.md must declare Current files"
        assert match.group("source_glob") == source_glob
        counts[source_glob] = int(match.group("count"))
    return counts


def test_codebase_map_counts_match_current_source_tree() -> None:
    expected = _expected_counts()

    assert _index_counts() == expected
    assert _structure_counts() == expected
    assert _node_counts() == expected
