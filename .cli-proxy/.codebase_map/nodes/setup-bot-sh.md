# Node: setup_bot.sh

Generated: 2026-06-17T10:46:19Z

## Purpose
Instruction node for `setup_bot.sh` area.

## Scope
- Source glob: `setup_bot.sh`
- Estimated files: 1

## Instructions for agent
- Read only files relevant to the active task.
- Prefer deterministic checks before edits.
- Keep changes minimal and validate with tests/linters where applicable.

## Source of truth
- `setup_bot.sh`
- `setup_bot.sh`

## When to update
- Any commit touching `setup_bot.sh`.
- Any commit touching `agent/**` because this node has import/call dependency on it.
- Any commit touching `app/**` because this node has import/call dependency on it.
- Any commit touching `config_example.yaml` because this node has import/call dependency on it.
- Any commit touching `miniapp/**` because this node has import/call dependency on it.
- Any commit touching `session.py` because this node has import/call dependency on it.
- Any architecture or behavior change affecting this area.

## Related nodes
- `nodes/agent.md`
- `nodes/app.md`
- `nodes/config-example-yaml.md`
- `nodes/miniapp.md`
- `nodes/session-py.md`
- `nodes/tests.md`
- `agent` confidence=0.66 via L0
- `app` confidence=0.66 via L0
- `config_example.yaml` confidence=0.66 via L0
- `miniapp` confidence=0.66 via L0
- `session.py` confidence=0.66 via L0
- `tests` confidence=0.66 via L0

## Owner
- project-maintainers

## Last reviewed
- 2026-06-17T10:46:19Z
