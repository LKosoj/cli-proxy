# Node: conftest.py

Generated: 2026-04-27T22:43:23Z

## Purpose
`/srv/git_projects/cli-proxy/conftest.py` defines repository-wide pytest startup setup. It raises the process file-descriptor soft limit when possible, configures headless/software Qt defaults for tests, and provides the `qapp_args` session fixture used by pytest-qt/PySide6 tests.

## Scope
- Source glob: `conftest.py`
- File: `/srv/git_projects/cli-proxy/conftest.py`
- Includes import-time helpers `_raise_nofile_soft_limit()` and `_configure_qt_test_env()`.
- Includes Qt-related environment defaults: `QT_QPA_PLATFORM`, `QT_OPENGL`, `QT_QUICK_BACKEND`, `PYTEST_QT_API`, and `XDG_RUNTIME_DIR`.
- Includes the `qapp_args` pytest fixture returning the Qt application argv for tests.
- Excludes test-local cleanup and import-path setup in `/srv/git_projects/cli-proxy/tests/conftest.py`.
- Excludes pytest marker and asyncio fixture-scope config in `/srv/git_projects/cli-proxy/pytest.ini`.

## Instructions for agent
- Start with `/srv/git_projects/cli-proxy/.cli-proxy/.codebase_map/INDEX.md`, then this node, then `/srv/git_projects/cli-proxy/conftest.py`.
- Treat this file as process-wide pytest setup: import-time changes affect every `.venv/bin/pytest` invocation.
- Preserve `os.environ.setdefault(...)` semantics unless the task explicitly changes how caller-provided Qt environment variables are honored.
- Keep responsibilities separate from `/srv/git_projects/cli-proxy/tests/conftest.py`, which owns repo import-path setup, BotApp async-service cleanup, and ToolRegistry singleton reset.
- For Qt/pytest-qt changes, verify with targeted tests that use `qtbot`, for example `/srv/git_projects/cli-proxy/tests/test_chat_view.py` or the affected `/srv/git_projects/cli-proxy/tests/test_desktop_*.py` file.
- For file-descriptor limit changes, verify with the nearest affected targeted pytest path; do not run the full suite unless the change is shared enough to require it.

## Source of truth
- `/srv/git_projects/cli-proxy/conftest.py` - root pytest startup hooks and `qapp_args` fixture.
- `/srv/git_projects/cli-proxy/.cli-proxy/.codebase_map/api/conftest-py.md` - generated symbol inventory only.
- `/srv/git_projects/cli-proxy/.cli-proxy/.codebase_map/TESTING.md` - repository pytest workflow and root fixture responsibilities.
- `/srv/git_projects/cli-proxy/pytest.ini` - pytest marker and asyncio fixture-scope config.
- `/srv/git_projects/cli-proxy/requirements.txt` - pytest, pytest-qt, PySide6, and qasync dependency pins.
- `/srv/git_projects/cli-proxy/tests/conftest.py` - separate test-local fixtures and cleanup responsibilities.

## Module API
Детальные интерфейсы модулей этой области:

- [conftest.py](../api/conftest-py.md)

## When to update
- Any change to `/srv/git_projects/cli-proxy/conftest.py`, including import-time setup, environment defaults, runtime-dir handling, file-descriptor limits, or `qapp_args`.
- Any change to `/srv/git_projects/cli-proxy/pytest.ini` that changes pytest process behavior consumed with this root setup.
- Any dependency change in `/srv/git_projects/cli-proxy/requirements.txt` affecting `pytest`, `pytest-qt`, `PySide6`, or `qasync`.
- Any change in `/srv/git_projects/cli-proxy/tests/conftest.py` that moves responsibilities between root and test-local fixtures.
- Any update to `/srv/git_projects/cli-proxy/.cli-proxy/.codebase_map/TESTING.md` that changes root fixture guidance.

## Related nodes
- `/srv/git_projects/cli-proxy/.cli-proxy/.codebase_map/nodes/tests.md` - owns test suite workflow and fixture responsibility boundaries.
- `/srv/git_projects/cli-proxy/.cli-proxy/.codebase_map/nodes/desktop.md` - owns PySide6/qasync Desktop code covered by Qt tests.
- `/srv/git_projects/cli-proxy/.cli-proxy/.codebase_map/nodes/pytest-ini.md` - pytest process configuration used alongside root `conftest.py`.
- `/srv/git_projects/cli-proxy/.cli-proxy/.codebase_map/nodes/requirements-txt.md` - dependency pins for pytest, pytest-qt, PySide6, and qasync.

## Owner
- project-maintainers

## Last reviewed
- 2026-04-28
