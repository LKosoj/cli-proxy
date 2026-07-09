# Node: tests

Generated: 2026-06-17T10:46:18Z

## Purpose
Instruction node for `tests` area.

## Scope
- Source glob: `tests/**`
- Estimated files: 552
- Current files: 558 under `tests/**` as of last review.

## Instructions for agent
- Read only files relevant to the active task.
- Prefer deterministic checks before edits.
- Keep changes minimal and validate with tests/linters where applicable.

## Source of truth
- `tests/**`
- `tests/conftest.py`
- `tests/smoke/_smoke_support.py`
- `tests/smoke/test_bot_entrypoint_smoke.py`
- `tests/smoke/test_desktop_entrypoint_smoke.py`
- `tests/smoke/test_miniapp_server_smoke.py`
- `tests/smoke/test_setup_bot_script.py`
- `tests/smoke/test_setup_bot_smoke.py`
- `tests/smoke/test_source_artifact_smoke.py`
- `tests/smoke/test_startup_smoke.py`
- `tests/test_admin_allowlist.py`

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
- `nodes/locales.md`
- `agent` confidence=0.95 via L0/L1/L2
- `app` confidence=0.95 via L0/L1/L2
- `bot.py` confidence=0.95 via L0/L2
- `config.py` confidence=0.90 via L0/L2
- `config_example.yaml` confidence=0.94 via L0
- `desktop` confidence=0.95 via L0/L1/L2
- `i18n` confidence=0.90 via L0/L1/L2
- `locales` confidence=0.95 via L0

## Owner
- project-maintainers

## Last reviewed
- 2026-07-09T20:44:12Z
