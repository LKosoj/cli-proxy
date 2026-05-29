# Node: utils

Generated: 2026-04-27T22:43:23Z

## Purpose
`utils/**` is the shared helper package for CLI command template expansion, resume/prompt detection, text/ANSI cleanup, Markdown/ANSI-to-HTML rendering, sandbox and `.cli-proxy` path construction, source artifact ZIP build/validation, and UI/session label plus async task helpers. `utils/__init__.py` re-exports the public helper surface consumed by `bot.py`, `session.py`, `app/**`, `agent/**`, `desktop/**`, `miniapp/**`, `modes/**`, `sessions/**`, `tg/**`, and tests.

## Scope
- Source glob: `utils/**`
- Current files: 7 under `utils/**` as of last review.
- Public helper exports: `utils/__init__.py`.
- CLI/process helpers: `utils/cli.py`.
- Rendering helpers: `utils/html_renderer.py`.
- Path and sandbox helpers: `utils/paths.py`.
- Source artifact CLI: `utils/source_artifact.py`.
- Text normalization helpers: `utils/text.py`.
- UI/session label and async scheduling helpers: `utils/ui.py`.

## Instructions for agent
- Start with `.cli-proxy/.codebase_map/INDEX.md`, then this node, then the task-specific files under `utils/**`.
- Before claiming behavior, inspect the exact helper in `utils/*.py` and cite concrete `path:line`.
- Keep helpers small and dependency-light; check current import direction before adding imports because `utils/**` is used by bot, desktop, MiniApp, sessions, modes, app services, and Telegram code.
- For Telegram formatting/rendering changes, inspect `utils/html_renderer.py`, `tg/markdown.py`, `bot.py`, and `sessions/session_output_service.py`.
- For path/sandbox changes, inspect `utils/paths.py`, `sessions/scoped_key.py`, `app/services/sandbox_service.py`, `session.py`, and callers under `miniapp/**`, `modes/**`, and `desktop/**`.
- For source artifact changes, inspect `utils/source_artifact.py`, `.github/workflows/ci.yml`, and `tests/smoke/test_source_artifact_smoke.py`.
- Validate Python edits with targeted `.venv/bin/pytest -q` tests near the changed helper and `.venv/bin/flake8`.

## Source of truth
- `utils/__init__.py` - public re-export list for helpers used as `from utils import ...`.
- `utils/cli.py` - `build_command()`, prompt/resume regex detection, and environment-variable expansion helpers used by `session.py`.
- `utils/html_renderer.py` - Markdown/ANSI-to-HTML rendering, Telegram Markdown conversion wrapper, temporary HTML file creation, and Mermaid SVG rendering through `mermaid.ink`.
- `utils/paths.py` - sandbox root/shared/session directory builders, `.cli-proxy` artifact path builder, and root containment check.
- `utils/source_artifact.py` - source artifact include/required/forbidden member lists, ZIP build/inspect/validate logic, and `python -m utils.source_artifact` CLI.
- `utils/text.py` - ANSI stripping, tick/time token extraction, MCP startup line cleanup, repeated-block dedupe, and preview truncation.
- `utils/ui.py` - session title/selector label formatting, status dots, and `ensure_async()` background-task scheduling/cleanup.
- API mirrors: `.cli-proxy/.codebase_map/api/utils/*.md`.
- Targeted tests: `tests/test_build_command_resume_image.py`, `tests/test_html_renderer.py`, `tests/test_html_render_headings.py`, `tests/test_html_render_quotes.py`, `tests/test_utils_ui.py`, `tests/test_sandbox_service.py`, `tests/test_multi_cli_per_session.py`, `tests/smoke/test_source_artifact_smoke.py`.

## When to update
- Any commit touching `utils/**`.
- Any change in `session.py` that changes command building, prompt/resume detection, tick/time filtering, or sandbox session path usage.
- Any change in `sessions/scoped_key.py`, `app/services/sandbox_service.py`, `modes/sdk/runtime/agent_core.py`, or `modes/sdk/runtime/memory_store.py` that changes sandbox path token semantics.
- Any change in `bot.py`, `sessions/session_output_service.py`, `sessions/session_management.py`, `desktop/widgets/report_viewer.py`, `desktop/widgets/chat_view.py`, or `tg/markdown.py` that changes HTML/Markdown rendering behavior.
- Any change in `.github/workflows/ci.yml`, release/smoke packaging, or source artifact membership expectations that affects `utils/source_artifact.py`.
- Any change in `desktop/**`, `miniapp/**`, `sessions/**`, or `tg/**` that changes session title/status label formatting or `ensure_async()` usage.
- Any targeted test change that adds, removes, or materially changes coverage for `utils/**`.

## Related nodes
- `.cli-proxy/.codebase_map/nodes/session-py.md` - `session.py` imports `utils.cli`, `utils.paths`, and `utils.text` for command execution, resume detection, sandbox paths, and output filtering.
- `.cli-proxy/.codebase_map/nodes/sessions.md` - `sessions/**` imports `utils.html_renderer`, `utils.text`, and `utils.ui`; `utils/paths.py` imports `sessions/scoped_key.py`.
- `.cli-proxy/.codebase_map/nodes/tg.md` - `utils/html_renderer.py` imports `tg/markdown.py`; Telegram callbacks/handlers use path and status helpers.
- `.cli-proxy/.codebase_map/nodes/app.md` - app services use `utils.paths`, `utils.text`, `utils.html_renderer`, and `utils.ui`.
- `.cli-proxy/.codebase_map/nodes/desktop.md` - desktop widgets/services use `utils.ui`, `utils.html_renderer`, and `utils.paths`.
- `.cli-proxy/.codebase_map/nodes/miniapp.md` - MiniApp routes/services use `utils.paths` and session label helpers from `utils.ui`.
- `.cli-proxy/.codebase_map/nodes/modes.md` - mode SDK/runtime and mode plugins use `utils.paths` and `utils.text`.
- `.cli-proxy/.codebase_map/nodes/agent.md` - agent manager/tooling helpers use `utils.paths` and `utils.text`.
- `.cli-proxy/.codebase_map/nodes/tests.md` - contains targeted coverage for command building, HTML rendering, sandbox paths, UI async helpers, source artifacts, and downstream utils consumers.

## Last reviewed
- 2026-05-01
