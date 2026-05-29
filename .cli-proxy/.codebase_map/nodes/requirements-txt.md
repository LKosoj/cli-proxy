# Node: requirements.txt

Generated: 2026-04-27T22:43:23Z

## Purpose
`/srv/git_projects/cli-proxy/requirements.txt` is the repository pip dependency manifest for bot runtime, MiniApp/web services, Desktop UI, mode/agent tooling, integrations, tests, and linting.

## Scope
- Source glob: `requirements.txt`
- File: `/srv/git_projects/cli-proxy/requirements.txt`
- Covers direct Python requirements installed by `python -m pip install -r requirements.txt`, CI, and `setup_bot.sh`.
- Includes runtime libraries, integration clients, Markdown/content extraction packages, Desktop Qt packages, test packages, and lint tooling.
- Does not cover transitive dependency resolution, npm/browser assets, or `pytest-cov`, which is installed separately in `/srv/git_projects/cli-proxy/.github/workflows/ci.yml`.

## Instructions for agent
- Start with `/srv/git_projects/cli-proxy/.cli-proxy/.codebase_map/INDEX.md`, then this node, then `/srv/git_projects/cli-proxy/requirements.txt`.
- Before adding, removing, or changing a package spec, identify the consuming code or command with targeted `rg` searches and read the owning node for that area.
- Keep dependency edits narrow; preserve the existing mix of exact pins and bounded ranges unless the task explicitly changes version policy.
- Do not reorder or reformat the full file for an isolated dependency change.
- For test-stack changes (`pytest`, `pytest-asyncio`, `pytest-xdist`, `pytest-qt`, `PySide6`, `qasync`), also read `/srv/git_projects/cli-proxy/.cli-proxy/.codebase_map/TESTING.md`, `nodes/tests.md`, `nodes/conftest-py.md`, and `nodes/pytest-ini.md`.
- Verify with targeted `.venv/bin/pytest -q ...` paths for the affected area; run `.venv/bin/flake8` when Python edits or lint-tool dependency changes are part of the task.

## Source of truth
- `/srv/git_projects/cli-proxy/requirements.txt` - direct pip requirements.
- `/srv/git_projects/cli-proxy/.github/workflows/ci.yml` - CI install, pip cache key, lint, pytest, coverage, and smoke commands.
- `/srv/git_projects/cli-proxy/setup_bot.sh` - setup-script install path for `requirements.txt`.
- `/srv/git_projects/cli-proxy/README.md` and `/srv/git_projects/cli-proxy/README_EN.MD` - user-facing install command.
- `/srv/git_projects/cli-proxy/.cli-proxy/.codebase_map/STACK.md` - dependency-to-runtime stack notes.
- `/srv/git_projects/cli-proxy/.cli-proxy/.codebase_map/TESTING.md` - pytest/lint dependency expectations.
- `/srv/git_projects/cli-proxy/.cli-proxy/.codebase_map/INTEGRATIONS.md` - integration/content-extraction dependency notes.

## When to update
- Any change to `/srv/git_projects/cli-proxy/requirements.txt`: package add/remove, version pin/range change, or tooling dependency change.
- Any change to `/srv/git_projects/cli-proxy/.github/workflows/ci.yml`, `/srv/git_projects/cli-proxy/setup_bot.sh`, `/srv/git_projects/cli-proxy/README.md`, or `/srv/git_projects/cli-proxy/README_EN.MD` that changes how `requirements.txt` is installed or documented.
- Any change to `/srv/git_projects/cli-proxy/.cli-proxy/.codebase_map/STACK.md`, `TESTING.md`, or `INTEGRATIONS.md` that changes documented dependency ownership.
- Any runtime, Desktop, MiniApp, mode, agent plugin, test, or lint behavior change that introduces or removes a direct pip dependency.

## Related nodes
- `/srv/git_projects/cli-proxy/.cli-proxy/.codebase_map/nodes/tests.md` - pytest/lint workflow and test dependency guidance.
- `/srv/git_projects/cli-proxy/.cli-proxy/.codebase_map/nodes/conftest-py.md` - root pytest setup that consumes pytest-qt, PySide6, and qasync.
- `/srv/git_projects/cli-proxy/.cli-proxy/.codebase_map/nodes/pytest-ini.md` - pytest configuration used with the dependency stack.
- `/srv/git_projects/cli-proxy/.cli-proxy/.codebase_map/nodes/desktop.md` - PySide6/qasync Desktop runtime.
- `/srv/git_projects/cli-proxy/.cli-proxy/.codebase_map/nodes/miniapp.md` - aiohttp MiniApp/web runtime.
- `/srv/git_projects/cli-proxy/.cli-proxy/.codebase_map/nodes/tg.md` - python-telegram-bot and Telegram Markdown transport.
- `/srv/git_projects/cli-proxy/.cli-proxy/.codebase_map/nodes/agent.md` - agent plugin dependencies including web/content tooling.

## Last reviewed
- 2026-05-08
