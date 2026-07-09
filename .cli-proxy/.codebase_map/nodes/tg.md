# Node: tg

Generated: 2026-06-17T10:46:18Z

## Purpose
Instruction node for `tg` area.

## Scope
- Source glob: `tg/**`
- Estimated files: 18
- Current files: 18 under `tg/**` as of last review.

## Instructions for agent
- Read only files relevant to the active task.
- Prefer deterministic checks before edits.
- Keep changes minimal and validate with tests/linters where applicable.

## Source of truth
- `tg/**`
- `tg/__init__.py`
- `tg/callback_actions/__init__.py`
- `tg/callback_actions/dirs.py`
- `tg/callback_actions/files.py`
- `tg/callback_actions/preset.py`
- `tg/callback_actions/protocol.py`
- `tg/callback_actions/session.py`
- `tg/callbacks.py`
- `tg/command_policy.py`
- `tg/command_registry.py`

## Module API
Детальные интерфейсы модулей этой области:

- [tg/callback_actions/__init__.py](../api/tg/callback_actions/__init__-py.md)
- [tg/callback_actions/dirs.py](../api/tg/callback_actions/dirs-py.md)
- [tg/callback_actions/files.py](../api/tg/callback_actions/files-py.md)
- [tg/callback_actions/preset.py](../api/tg/callback_actions/preset-py.md)
- [tg/callback_actions/protocol.py](../api/tg/callback_actions/protocol-py.md)
- [tg/callback_actions/session.py](../api/tg/callback_actions/session-py.md)
- [tg/callbacks.py](../api/tg/callbacks-py.md)
- [tg/command_registry.py](../api/tg/command_registry-py.md)
- [tg/file_upload_handler.py](../api/tg/file_upload_handler-py.md)
- [tg/files_service_adapter.py](../api/tg/files_service_adapter-py.md)

## When to update
- Any commit touching `tg/**`.
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
- `config.py` confidence=0.66 via L0
- `config_example.yaml` confidence=0.66 via L0
- `desktop` confidence=0.95 via L0
- `i18n` confidence=0.90 via L0/L1/L2
- `locales` confidence=0.95 via L0

## Owner
- project-maintainers

## Last reviewed
- 2026-07-09T21:32:00Z
