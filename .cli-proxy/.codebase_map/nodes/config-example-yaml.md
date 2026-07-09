# Node: config_example.yaml

Generated: 2026-06-17T10:46:18Z

## Purpose
Instruction node for `config_example.yaml` area.

## Scope
- Source glob: `config_example.yaml`
- Estimated files: 1

## Instructions for agent
- Read only files relevant to the active task.
- Prefer deterministic checks before edits.
- Keep changes minimal and validate with tests/linters where applicable.

## Source of truth
- `config_example.yaml`
- `config_example.yaml`

## When to update
- Any commit touching `config_example.yaml`.
- Any commit touching `agent/**` because this node has import/call dependency on it.
- Any commit touching `app/**` because this node has import/call dependency on it.
- Any commit touching `bot.py` because this node has import/call dependency on it.
- Any commit touching `config.py` because this node has import/call dependency on it.
- Any commit touching `desktop/**` because this node has import/call dependency on it.
- Any architecture or behavior change affecting this area.

## Related nodes
- `nodes/agent.md`
- `nodes/app.md`
- `nodes/bot-py.md`
- `nodes/config-py.md`
- `nodes/desktop.md`
- `nodes/i18n.md`
- `nodes/locales.md`
- `nodes/miniapp.md`
- `agent` confidence=0.88 via L0
- `app` confidence=0.94 via L0
- `bot.py` confidence=0.66 via L0
- `config.py` confidence=0.77 via L0
- `desktop` confidence=0.66 via L0
- `i18n` confidence=0.66 via L0
- `locales` confidence=0.66 via L0
- `miniapp` confidence=0.77 via L0

## Owner
- project-maintainers

## Last reviewed
- 2026-07-07T00:00:00Z
