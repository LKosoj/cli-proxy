# Node: session.py

Generated: 2026-06-17T10:46:19Z

## Purpose
Instruction node for `session.py` area.

## Scope
- Source glob: `session.py`
- Estimated files: 1

## Instructions for agent
- Read only files relevant to the active task.
- Prefer deterministic checks before edits.
- Keep changes minimal and validate with tests/linters where applicable.

## Source of truth
- `session.py`
- `session.py`

## Module API
Детальные интерфейсы модулей этой области:

- [session.py](../api/session-py.md)

## When to update
- Any commit touching `session.py`.
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
- `agent` confidence=0.94 via L0
- `app` confidence=0.95 via L0/L1/L2
- `bot.py` confidence=0.66 via L0
- `config.py` confidence=0.90 via L2
- `config_example.yaml` confidence=0.77 via L0
- `desktop` confidence=0.77 via L0
- `i18n` confidence=0.90 via L1/L2
- `miniapp` confidence=0.94 via L0

## Owner
- project-maintainers

## Last reviewed
- 2026-08-25T00:00:00Z
