# Node: tests

Generated: 2026-06-03T02:24:28Z

## Purpose
Instruction node for the project test suite under `tests/**`: pytest-based unit, integration, and smoke tests covering all architecture layers (transport, services, modes, agent, core). Single flat package plus `tests/smoke/` for entrypoint checks.

## Scope
- Source glob: `tests/**`
- Current files: 539 under `tests/**` as of last review.
- Subdirectories: only `tests/smoke/` (entrypoint smoke tests); the rest is a flat `tests/test_*.py` namespace.
- Test framework config lives outside this glob: `pytest.ini` (root), `requirements.txt`.
- Two `conftest.py` layers: root `conftest.py` (Qt/offscreen env, RLIMIT_NOFILE bump, `qapp_args` session fixture) and `tests/conftest.py` (adds repo root to `sys.path`, tracks/shuts down `BotApp` async services, resets `ToolRegistry` singleton between tests).
- Naming convention: `tests/test_<area>_<topic>.py`, usually mirroring the source module under test (e.g. `app/services/<name>.py` → `tests/test_<name>.py`).

## Instructions for agent
- Read only the test files relevant to the active task; the suite is large (524 test modules).
- Default to targeted runs for changed modules and nearest integration paths:
  - `.venv/bin/pytest -q tests/test_file.py::test_name`
  - `.venv/bin/pytest -q tests/test_area_*.py`
- Run the full `.venv/bin/pytest -q` only for release/smoke or shared-layer changes (`modes/sdk/runtime/*`, `modes/sdk/services/*`, `app/services/*`, `config.py`, `bot.py`, `session.py`).
- Lint with `.venv/bin/flake8`.
- For bugfixes: add or update a failing test that reproduces the issue before changing code.
- Async tests use `@pytest.mark.asyncio` (loop scope `function` per `pytest.ini`); SSH integration tests are gated by the `ssh_integration` marker.
- Do not substitute smoke tests (`tests/smoke/*`) for targeted unit/integration coverage.
- See `../TESTING.md` for stack details, target-selection map, and quality expectations.

## Source of truth
- `tests/**`
- `pytest.ini` — markers (`asyncio`, `ssh_integration`) and asyncio loop scope.
- `conftest.py` (repo root) — Qt offscreen env, file-descriptor limit, `qapp_args` fixture.
- `tests/conftest.py` — `sys.path` setup, `BotApp` async-service teardown, `ToolRegistry` singleton reset.
- `tests/smoke/_smoke_support.py` and `tests/smoke/test_*_smoke.py` — entrypoint smoke tests.
- `requirements.txt` — test stack (`pytest`, `pytest-asyncio`, `pytest-xdist`, `pytest-qt`, `PySide6`, `aiohttp`).

## Module API
Детальные интерфейсы модулей этой области:

- [tests/smoke/_smoke_support.py](../api/tests/smoke/_smoke_support-py.md)
- [tests/smoke/test_bot_entrypoint_smoke.py](../api/tests/smoke/test_bot_entrypoint_smoke-py.md)
- [tests/smoke/test_desktop_entrypoint_smoke.py](../api/tests/smoke/test_desktop_entrypoint_smoke-py.md)
- [tests/smoke/test_miniapp_server_smoke.py](../api/tests/smoke/test_miniapp_server_smoke-py.md)
- [tests/smoke/test_setup_bot_script.py](../api/tests/smoke/test_setup_bot_script-py.md)
- [tests/smoke/test_setup_bot_smoke.py](../api/tests/smoke/test_setup_bot_smoke-py.md)
- [tests/smoke/test_source_artifact_smoke.py](../api/tests/smoke/test_source_artifact_smoke-py.md)
- [tests/smoke/test_startup_smoke.py](../api/tests/smoke/test_startup_smoke-py.md)
- [tests/test_admin_allowlist.py](../api/tests/test_admin_allowlist-py.md)
- [tests/test_admin_analyzer.py](../api/tests/test_admin_analyzer-py.md)
- [tests/test_admin_autonomy_e2e.py](../api/tests/test_admin_autonomy_e2e-py.md)
- [tests/test_admin_autonomy_loop.py](../api/tests/test_admin_autonomy_loop-py.md)
- [tests/test_admin_autonomy_policy.py](../api/tests/test_admin_autonomy_policy-py.md)
- [tests/test_admin_autopilot_telegram_format.py](../api/tests/test_admin_autopilot_telegram_format-py.md)
- [tests/test_admin_baseline.py](../api/tests/test_admin_baseline-py.md)
- [tests/test_admin_chat_autopilot_verdict.py](../api/tests/test_admin_chat_autopilot_verdict-py.md)
- [tests/test_admin_chat_gateway.py](../api/tests/test_admin_chat_gateway-py.md)
- [tests/test_admin_chat_memory.py](../api/tests/test_admin_chat_memory-py.md)
- [tests/test_admin_chat_schemas.py](../api/tests/test_admin_chat_schemas-py.md)
- [tests/test_admin_chat_service.py](../api/tests/test_admin_chat_service-py.md)
- [tests/test_admin_config_store.py](../api/tests/test_admin_config_store-py.md)
- [tests/test_admin_drift.py](../api/tests/test_admin_drift-py.md)
- [tests/test_admin_executor.py](../api/tests/test_admin_executor-py.md)
- [tests/test_admin_facade.py](../api/tests/test_admin_facade-py.md)
- [tests/test_admin_facade_scripts.py](../api/tests/test_admin_facade_scripts-py.md)
- [tests/test_admin_local_transport.py](../api/tests/test_admin_local_transport-py.md)
- [tests/test_admin_memory.py](../api/tests/test_admin_memory-py.md)
- [tests/test_admin_mode_architecture.py](../api/tests/test_admin_mode_architecture-py.md)
- [tests/test_admin_mode_lifecycle.py](../api/tests/test_admin_mode_lifecycle-py.md)
- [tests/test_admin_mode_plugin.py](../api/tests/test_admin_mode_plugin-py.md)
- [tests/test_admin_monitor.py](../api/tests/test_admin_monitor-py.md)
- [tests/test_admin_notifier.py](../api/tests/test_admin_notifier-py.md)
- [tests/test_admin_plugin_tools.py](../api/tests/test_admin_plugin_tools-py.md)
- [tests/test_admin_prereqs.py](../api/tests/test_admin_prereqs-py.md)
- [tests/test_admin_reconciliation.py](../api/tests/test_admin_reconciliation-py.md)
- [tests/test_admin_runbook_builder.py](../api/tests/test_admin_runbook_builder-py.md)
- [tests/test_admin_runbook_promoter.py](../api/tests/test_admin_runbook_promoter-py.md)
- [tests/test_admin_runbook_validator.py](../api/tests/test_admin_runbook_validator-py.md)
- [tests/test_admin_runbooks.py](../api/tests/test_admin_runbooks-py.md)

## When to update
- Any commit touching `tests/**`.
- Any commit touching `agent/**` because this node has import/call dependency on it.
- Any commit touching `app/**` because this node has import/call dependency on it.
- Any commit touching `bot.py` because this node has import/call dependency on it.
- Any commit touching `config.py` because this node has import/call dependency on it.
- Any commit touching `config_example.yaml` because this node has import/call dependency on it.
- Any architecture or behavior change affecting this area.

## Related nodes
- `nodes/agent.md`
- `nodes/app.md`
- `nodes/bot-py.md`
- `nodes/config-py.md`
- `nodes/config-example-yaml.md`
- `nodes/desktop.md`
- `nodes/i18n.md`
- `nodes/miniapp.md`
- `agent` confidence=0.95 via L0/L1/L2
- `app` confidence=0.95 via L0/L1/L2
- `bot.py` confidence=0.90 via L0/L2
- `config.py` confidence=0.90 via L0/L2
- `config_example.yaml` confidence=0.95 via L0
- `desktop` confidence=0.90 via L0/L1/L2
- `i18n` confidence=0.90 via L1/L2
- `miniapp` confidence=0.95 via L0/L1/L2

## Owner
- project-maintainers

## Last reviewed
- 2026-06-03 (enriched: scope counts, conftest layers, run commands verified against `tests/`, `pytest.ini`, `conftest.py`)
