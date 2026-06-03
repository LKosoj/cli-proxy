# Node: conftest.py

Generated: 2026-06-03T02:24:29Z

## Purpose
Root-level pytest bootstrap (`conftest.py` at repo root). Auto-loaded by pytest before test collection; configures a headless environment for the whole suite: raises the open-file-descriptor soft limit, forces Qt/PySide6 into offscreen/software mode, prepares `XDG_RUNTIME_DIR`, and exposes the `qapp_args` session fixture consumed by `pytest-qt`.

## Scope
- Source glob: `conftest.py` (non-recursive — matches only the repo-root file).
- Estimated files: 1.
- Out of scope: `tests/conftest.py` (belongs to the `tests/**` node) — it handles `sys.path`, `BotApp` async-service teardown and `ToolRegistry` singleton reset, distinct from this file's responsibilities.
- Side effects run at import time: `_raise_nofile_soft_limit()` and `_configure_qt_test_env()` execute on module load (`conftest.py:36-37`), before any fixture.

## Instructions for agent
- Treat this as global test infrastructure: changes here affect every `pytest` run, not a single test module.
- Keep env setup `os.environ.setdefault(...)` (do not overwrite values the caller/CI already provides) — see `conftest.py:22-25,33`.
- `qapp_args` reads `os.environ["QT_QPA_PLATFORM"]` (`conftest.py:42`); it relies on `_configure_qt_test_env()` having set it. Preserve that ordering if editing.
- After any change run a Qt-dependent test (e.g. `.venv/bin/pytest -q tests/test_config_editor.py`) plus a broad `.venv/bin/pytest -q` smoke to confirm collection still works headless.
- Failure-handling functions deliberately swallow `OSError`/`ValueError` to stay portable across CI hosts; keep that resilience rather than hard-failing on limits/dirs.

## Source of truth
- `conftest.py` (repo root) — the implementation itself.
- `pytest.ini` — markers and `asyncio_default_fixture_loop_scope`; pairs with this file as test config.
- `requirements.txt` — provides `pytest-qt` / `PySide6` that consume `qapp_args` and the Qt env.

## Module API
Детальные интерфейсы модулей этой области:

- [conftest.py](../api/conftest-py.md)

Symbols: `_raise_nofile_soft_limit()` (`conftest.py:8`), `_configure_qt_test_env()` (`conftest.py:21`), `qapp_args()` session fixture (`conftest.py:40`).

## When to update
- Any commit touching `conftest.py` (repo root).
- Changes to the Qt/headless test environment, file-descriptor handling, or the `qapp_args` fixture contract.
- Changes to `pytest.ini` or test-stack pins in `requirements.txt` that alter how this bootstrap is loaded or consumed.

## Related nodes
- `nodes/pytest-ini.md` — companion pytest config (markers, asyncio loop scope).
- `nodes/tests.md` — the suite that loads this file; documents the second `tests/conftest.py` layer.
- `nodes/requirements-txt.md` — `pytest-qt` / `PySide6` dependencies behind `qapp_args`.

## Owner
- project-maintainers

## Last reviewed
- 2026-06-03 (enriched: import-time side effects, qapp_args contract, conftest layering vs `tests/conftest.py`, verified against `conftest.py`, `pytest.ini`, `tests/`)
