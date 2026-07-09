# Node: bot.py

Generated: 2026-06-17T10:46:18Z

## Purpose
Instruction node for `bot.py` area.

## Scope
- Source glob: `bot.py`
- Estimated files: 1

## Instructions for agent
- Read only files relevant to the active task.
- Prefer deterministic checks before edits.
- Keep changes minimal and validate with tests/linters where applicable.

## Source of truth
- `bot.py`
- `bot.py`

## Module API
Детальные интерфейсы модулей этой области:

- [bot.py](../api/bot-py.md)

## When to update
- Any commit touching `bot.py`.
- Any commit touching `agent/**` because this node has import/call dependency on it.
- Any commit touching `app/**` because this node has import/call dependency on it.
- Any commit touching `config.py` because this node has import/call dependency on it.
- Any commit touching `config_example.yaml` because this node has import/call dependency on it.
- Any commit touching `desktop/**` because this node has import/call dependency on it.
- Any architecture or behavior change affecting this area.

## Related nodes
- `nodes/agent.md`
- `nodes/app.md`
- `nodes/config-py.md`
- `nodes/config-example-yaml.md`
- `nodes/desktop.md`
- `nodes/i18n.md`
- `nodes/locales.md`
- `nodes/miniapp.md`
- `agent` confidence=0.94 via L0/L1/L2
- `app` confidence=0.95 via L0/L1/L2
- `config.py` confidence=0.90 via L0/L2
- `config_example.yaml` confidence=0.66 via L0
- `desktop` confidence=0.95 via L0
- `i18n` confidence=0.90 via L1/L2
- `locales` confidence=0.94 via L0
- `miniapp` confidence=0.94 via L0/L1/L2

## Owner
- project-maintainers

## Last reviewed
- 2026-07-09T20:44:12Z
