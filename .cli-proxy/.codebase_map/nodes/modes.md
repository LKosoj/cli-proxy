# Node: modes

Generated: 2026-06-17T10:46:18Z

## Purpose
Instruction node for `modes` area.

## Scope
- Source glob: `modes/**`
- Estimated files: 220
- Current files: 89 under `modes/**` as of last review.

## Instructions for agent
- Read only files relevant to the active task.
- Prefer deterministic checks before edits.
- Keep changes minimal and validate with tests/linters where applicable.

## Source of truth
- `modes/**`
- `modes/__init__.py`
- `modes/admin/__init__.py`
- `modes/agent/__init__.py`
- `modes/analyst/__init__.py`
- `modes/codebase_mapper/__init__.py`
- `modes/manager/__init__.py`
- `modes/sdd/__init__.py`
- `modes/sdk/__init__.py`
- `modes/webmaster/__init__.py`
- `modes/admin/prompts.yaml`

## Module API
Детальные интерфейсы модулей этой области:

- [modes/analyst/__init__.py](../api/modes/analyst/__init__-py.md)
- [modes/manager/__init__.py](../api/modes/manager/__init__-py.md)
- [modes/sdd/__init__.py](../api/modes/sdd/__init__-py.md)
- [modes/admin/action_specs.py](../api/modes/admin/action_specs-py.md)
- [modes/admin/allowlist.py](../api/modes/admin/allowlist-py.md)
- [modes/admin/analyzer.py](../api/modes/admin/analyzer-py.md)
- [modes/admin/autonomy_loop.py](../api/modes/admin/autonomy_loop-py.md)
- [modes/admin/autonomy_policy.py](../api/modes/admin/autonomy_policy-py.md)
- [modes/admin/baseline.py](../api/modes/admin/baseline-py.md)
- [modes/admin/chat_gateway.py](../api/modes/admin/chat_gateway-py.md)
- [modes/admin/chat_memory.py](../api/modes/admin/chat_memory-py.md)
- [modes/admin/chat_schemas.py](../api/modes/admin/chat_schemas-py.md)
- [modes/admin/chat_service.py](../api/modes/admin/chat_service-py.md)
- [modes/admin/config_store.py](../api/modes/admin/config_store-py.md)
- [modes/admin/drift.py](../api/modes/admin/drift-py.md)

## When to update
- Any commit touching `modes/**`.
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
- `bot.py` confidence=0.94 via L0/L2
- `config.py` confidence=0.90 via L0/L2
- `config_example.yaml` confidence=0.77 via L0
- `desktop` confidence=0.95 via L0
- `i18n` confidence=0.90 via L0/L1/L2
- `locales` confidence=0.88 via L0

## Owner
- project-maintainers

## Last reviewed
- 2026-08-25T00:00:00Z
