# Node: session.py

Generated: 2026-04-27T22:43:23Z

## Purpose
Runtime session orchestration for CLI-backed conversations. `session.py` defines session state, CLI selection/resume handling, headless and interactive execution, activity ticks, interruption/cleanup, and `SessionManager` persistence/lookup.

## Scope
- `/srv/git_projects/cli-proxy/session.py`
- API mirror: `/srv/git_projects/cli-proxy/.cli-proxy/.codebase_map/api/session-py.md`

## Instructions for agent
- Before making runtime claims, inspect `/srv/git_projects/cli-proxy/session.py`; use the API mirror only as a navigation aid.
- Keep changes surgical: this file is shared by bot, desktop, modes, session storage, and CLI stream handling.
- Preserve compatibility between legacy session fields and nested state dataclasses: `CliState`, `GitState`, `ModeState`, `OrchestratorState`.
- For behavior changes, verify the touched execution path with targeted tests under `/srv/git_projects/cli-proxy/tests/**`; do not run the full suite unless the change crosses shared runtime boundaries.
- When session payloads, resume tokens, scoped keys, or config-dependent CLI selection change, check callers in related nodes before editing.

## Source of truth
- `/srv/git_projects/cli-proxy/session.py`
- `/srv/git_projects/cli-proxy/.cli-proxy/.codebase_map/api/session-py.md`

## When to update
- Any commit touching `/srv/git_projects/cli-proxy/session.py`.
- Changes to CLI execution, resume-token recovery, stream adapters, activity ticks, interruption/cleanup, scoped session keys, or persisted session payloads.
- Changes in `/srv/git_projects/cli-proxy/app/services/**`, `/srv/git_projects/cli-proxy/config.py`, `/srv/git_projects/cli-proxy/sessions/**`, `/srv/git_projects/cli-proxy/modes/**`, `/srv/git_projects/cli-proxy/desktop/**`, or `/srv/git_projects/cli-proxy/bot.py` that call or depend on session behavior.
- Any generated map/API refresh that changes `/srv/git_projects/cli-proxy/.cli-proxy/.codebase_map/api/session-py.md`.

## Related nodes
- `/srv/git_projects/cli-proxy/.cli-proxy/.codebase_map/nodes/app.md`
- `/srv/git_projects/cli-proxy/.cli-proxy/.codebase_map/nodes/bot-py.md`
- `/srv/git_projects/cli-proxy/.cli-proxy/.codebase_map/nodes/config-py.md`
- `/srv/git_projects/cli-proxy/.cli-proxy/.codebase_map/nodes/desktop.md`
- `/srv/git_projects/cli-proxy/.cli-proxy/.codebase_map/nodes/modes.md`
- `/srv/git_projects/cli-proxy/.cli-proxy/.codebase_map/nodes/sessions.md`
- `/srv/git_projects/cli-proxy/.cli-proxy/.codebase_map/nodes/tests.md`

## Last reviewed
- 2026-05-31
