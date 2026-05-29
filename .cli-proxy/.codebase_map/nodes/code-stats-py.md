# Node: code_stats.py

Generated: 2026-04-27T22:43:23Z

## Purpose
`code_stats.py` is a standalone repository utility that reports codebase size by file category: file count, total lines, code lines, byte size, and optional JSON output.

## Scope
- Source glob: `code_stats.py`.
- Covers the CLI entrypoint, file collection, exclusion rules, line counting, aggregation, size formatting, text output, verbose output, and `--json` output in `code_stats.py`.
- Does not cover runtime metrics, MiniApp/Desktop UI, lint-evolution stats, or mode quality metrics under `app/**`, `desktop/**`, `miniapp/**`, `modes/**`, or `tests/**`.

## Instructions for agent
- Inspect `code_stats.py` before changing this node; use `.cli-proxy/.codebase_map/api/code_stats-py.md` only as the generated symbol reference.
- Keep the script self-contained and stdlib-only unless the active task explicitly changes that contract.
- Preserve the existing CLI behavior unless the task asks otherwise: optional positional `path`, `-v/--verbose`, and `--json`.
- When editing scan behavior, verify category extensions, excluded paths, test-file separation, and output fields directly in `code_stats.py`.
- For behavior changes, run a targeted smoke check such as `.venv/bin/python code_stats.py . --json`; add targeted pytest coverage only if tests for this script are introduced.

## Source of truth
- `code_stats.py`
- `.cli-proxy/.codebase_map/api/code_stats-py.md`

## Module API
Детальные интерфейсы модулей этой области:

- [code_stats.py](../api/code_stats-py.md)

## When to update
- Any commit touching `code_stats.py`.
- Changes to CLI arguments, output schema/format, file categories, extension mapping, exclusion patterns, line-counting rules, or test/main-file separation.
- Regeneration of `.cli-proxy/.codebase_map/api/code_stats-py.md` that changes documented symbols for `code_stats.py`.

## Related nodes
- (none)

## Owner
- project-maintainers

## Last reviewed
- 2026-04-28
