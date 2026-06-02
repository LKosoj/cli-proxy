# Node: tests

Generated: 2026-04-27T22:43:22Z

## Purpose
Guide agents that maintain the repository test suite: pytest unit/integration tests, smoke entrypoint checks, MiniApp/web browser checks, and test fixtures used across bot, app services, modes, desktop, and runtime code.

## Scope
- Source glob: `tests/**`
- Current files: 525 under `tests/**` as of last review.
- Includes root pytest environment setup in `conftest.py`, test-local cleanup in `tests/conftest.py`, and pytest config in `pytest.ini`.
- Covers focused suites such as `tests/test_admin_*`, `tests/test_analyst_*`, `tests/test_manager_*`, `tests/test_desktop_*`, `tests/test_miniapp_*`, `tests/test_ssh_*`, `tests/test_security_*`, and `tests/test_lint_evolution_*`.
- Smoke checks live in `tests/smoke/*`.

## Instructions for agent
- Before changing tests, read the production path under test and the nearest matching `tests/test_*.py` file.
- Keep test edits focused on the changed behavior; do not reorganize unrelated tests or fixtures.
- Default verification is targeted pytest through `.venv/bin/pytest -q tests/test_file.py` or `.venv/bin/pytest -q tests/test_file.py::test_name`.
- Use `.venv/bin/pytest -q tests/smoke/...` for entrypoint smoke coverage; do not use smoke tests as a replacement for targeted unit/integration tests.
- Run full `.venv/bin/pytest -q` only for release/smoke-check work or shared runtime changes where targeted coverage is insufficient.
- Run `.venv/bin/flake8` when Python edits are part of the task.
- For MiniApp/web behavior, include the relevant `tests/test_miniapp_*.py` path and `tests/test_miniapp_playwright.py` or `playwright-cli` verification when browser behavior is affected.
- Preserve fixture responsibilities: `conftest.py` owns Qt/offscreen and file descriptor setup; `tests/conftest.py` owns repo import path, BotApp async service cleanup, and ToolRegistry singleton reset.

## Source of truth
- `.cli-proxy/.codebase_map/TESTING.md`
- `pytest.ini`
- `requirements.txt`
- `conftest.py`
- `tests/**`
- `tests/conftest.py`
- `tests/test_miniapp_playwright.py`
- `tests/smoke/_smoke_support.py`
- `tests/smoke/test_bot_entrypoint_smoke.py`
- `tests/smoke/test_desktop_entrypoint_smoke.py`
- `tests/smoke/test_miniapp_server_smoke.py`
- `tests/smoke/test_setup_bot_script.py`
- `tests/smoke/test_setup_bot_smoke.py`
- `tests/smoke/test_source_artifact_smoke.py`
- `tests/smoke/test_startup_smoke.py`
- `tests/test_readme_feature_flags_sync.py` - README/README_EN config and feature documentation sync checks, including ConfigApplyPolicy reload/restart semantics.
- `tests/test_mode_run_lifecycle_service.py` - ModeRunLifecycleService facade coverage, including boundary validator negative-path logging/result conversion.
- `tests/test_miniapp_config_tab_js.py` - MiniApp config tab client behavior, including redacted secret sentinel preservation, new secret submission, explicit secret clear, and secret-change save warnings.
- `tests/test_miniapp_playwright.py` - Browser-backed MiniApp flows, including secret-safe config DOM evidence and save side-effect checks through real route auth.
- `tests/test_task_bearing_cli_skill_hook.py` - Task-bearing direct/routed CLI skill-selection tests and prompt wrapping evidence.

## When to update
- Any change adding, removing, renaming, or moving files under `tests/**`.
- Any change to shared pytest setup in `conftest.py`, `tests/conftest.py`, or `pytest.ini`.
- Any change to test dependencies or commands in `requirements.txt` or `.cli-proxy/.codebase_map/TESTING.md`.
- Any behavior change that creates, removes, or materially changes the targeted test path agents should run.
- Any MiniApp/web test workflow change involving `tests/test_miniapp_playwright.py` or `playwright-cli`.

## Related nodes
- `nodes/agent.md`
- `nodes/app.md`
- `nodes/bot-py.md`
- `nodes/config-py.md`
- `nodes/config-example-yaml.md`
- `nodes/desktop.md`
- `nodes/miniapp.md`
- `nodes/modes.md`
- `nodes/session-py.md`
- `nodes/sessions.md`
- `nodes/tg.md`

## Last reviewed
- 2026-06-02
