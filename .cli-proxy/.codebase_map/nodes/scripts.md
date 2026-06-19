# Node: scripts

Generated: 2026-06-17T10:46:18Z

## Purpose
Instruction node for `scripts` area.

## Scope
- Source glob: `scripts/**`
- Estimated files: 2

## Instructions for agent
- Read only files relevant to the active task.
- Prefer deterministic checks before edits.
- Keep changes minimal and validate with tests/linters where applicable.

## Source of truth
- `scripts/**`
- `scripts/check_i18n_parity.py`
- `scripts/setup-claude-bot.sh`

## Module API
Детальные интерфейсы модулей этой области:

- [scripts/check_i18n_parity.py](../api/scripts/check_i18n_parity-py.md)

## When to update
- Any commit touching `scripts/**`.
- Any commit touching `agent/**` because this node has import/call dependency on it.
- Any commit touching `app/**` because this node has import/call dependency on it.
- Any commit touching `bot.py` because this node has import/call dependency on it.
- Any commit touching `desktop/**` because this node has import/call dependency on it.
- Any commit touching `miniapp/**` because this node has import/call dependency on it.
- Any architecture or behavior change affecting this area.

## Related nodes
- `nodes/agent.md`
- `nodes/app.md`
- `nodes/bot-py.md`
- `nodes/desktop.md`
- `nodes/miniapp.md`
- `nodes/modes.md`
- `nodes/sessions.md`
- `nodes/summary-py.md`
- `agent` confidence=0.66 via L0
- `app` confidence=0.66 via L0
- `bot.py` confidence=0.66 via L0
- `desktop` confidence=0.66 via L0
- `miniapp` confidence=0.66 via L0
- `modes` confidence=0.66 via L0
- `sessions` confidence=0.66 via L0
- `summary.py` confidence=0.66 via L0

## Owner
- project-maintainers

## Last reviewed
- 2026-06-17T10:46:18Z
