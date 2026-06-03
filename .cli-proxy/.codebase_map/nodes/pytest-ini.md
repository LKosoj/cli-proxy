# Node: pytest.ini

Generated: 2026-06-03T02:24:29Z

## Purpose
Root pytest configuration (`pytest.ini` at repo root). Holds the suite-wide `[pytest]` section: the pytest-asyncio default fixture loop scope and the custom marker registry. Loaded automatically by pytest for every run; pairs with the two `conftest.py` layers (root `conftest.py`, `tests/conftest.py`) that supply the actual fixtures/env.

## Scope
- Source glob: `pytest.ini` (matches only the repo-root file).
- Estimated files: 1.
- Current contents (`pytest.ini:1-6`):
  - `asyncio_default_fixture_loop_scope = function` — pytest-asyncio fixture loop scope.
  - `markers` — registers `asyncio` (declared for environments without pytest-asyncio) and `ssh_integration` (integration tests that start a local SSH server).
- Out of scope: fixtures and headless/env setup (live in `conftest.py` and `tests/conftest.py`); dependency pins (live in `requirements.txt`).

## Instructions for agent
- Treat this as global test config: changes affect every `pytest` invocation, not one module.
- Register any new `@pytest.mark.<name>` here under `markers` before using it, or pytest emits `PytestUnknownMarkWarning` (which CI may treat as an error).
- `ssh_integration` currently gates `tests/test_ssh_service_live.py` (`@pytest.mark.ssh_integration` + `@pytest.mark.asyncio`); deselect it with `.venv/bin/pytest -q -m "not ssh_integration"` when no local SSH server is available.
- Keep the `asyncio` marker registered: tests apply `@pytest.mark.asyncio` directly, so it must exist independently of pytest-asyncio auto-mode.
- Do not change `asyncio_default_fixture_loop_scope` (`function`) without auditing async fixture lifetimes across the suite — a broader scope can leak event loops between tests.
- After edits, validate with a targeted async run plus a collection smoke: `.venv/bin/pytest -q tests/test_ssh_service_live.py` and `.venv/bin/pytest -q --collect-only`.

## Source of truth
- `pytest.ini` (repo root) — the configuration itself.
- `conftest.py` (repo root) — Qt/offscreen env, RLIMIT_NOFILE bump, `qapp_args` fixture loaded alongside this config.
- `tests/conftest.py` — `sys.path` setup, `BotApp` async-service teardown, `ToolRegistry` singleton reset.
- `tests/test_ssh_service_live.py` — sole consumer of the `ssh_integration` marker.
- `requirements.txt` — test-stack pins behind this config (`pytest==8.3.4`, `pytest-asyncio==0.25.3`, `pytest-xdist==3.8.0`, `pytest-qt==4.5.0`).

## When to update
- Any commit touching `pytest.ini`.
- Adding/removing custom markers, or changing the asyncio loop scope / pytest-asyncio configuration.
- Test-stack pin changes in `requirements.txt` (esp. `pytest`, `pytest-asyncio`) that alter how this config is interpreted.

## Related nodes
- `nodes/conftest-py.md` — companion root pytest bootstrap (fixtures, headless env).
- `nodes/tests.md` — the suite governed by this config; documents both `conftest.py` layers and marker usage.
- `nodes/requirements-txt.md` — `pytest` / `pytest-asyncio` / `pytest-qt` pins behind this config.

## Owner
- project-maintainers

## Last reviewed
- 2026-06-03 (enriched: contents breakdown, marker registry + `ssh_integration` consumer, asyncio loop scope, run/deselect commands; verified against `pytest.ini`, `tests/test_ssh_service_live.py`, `requirements.txt`, `conftest.py`)
