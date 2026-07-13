# Node: sessions

Generated: 2026-06-17T10:46:18Z

## Purpose
Instruction node for `sessions` area.

## Scope
- Source glob: `sessions/**`
- Estimated files: 10
- Current files: 10 under `sessions/**` as of last review.

## Instructions for agent
- Read only files relevant to the active task.
- Prefer deterministic checks before edits.
- Keep changes minimal and validate with tests/linters where applicable.

## Source of truth
- `sessions/**`
- `sessions/__init__.py`
- `sessions/conversation_scope.py`
- `sessions/queue_item.py`
- `sessions/scoped_key.py`
- `sessions/session_management.py`
- `sessions/session_output_service.py`
- `sessions/session_run_service.py`
- `sessions/session_state_access.py`
- `sessions/session_status.py`
- `sessions/session_ui.py`

## Module API
Детальные интерфейсы модулей этой области:

- [sessions/conversation_scope.py](../api/sessions/conversation_scope-py.md)
- [sessions/queue_item.py](../api/sessions/queue_item-py.md)
- [sessions/scoped_key.py](../api/sessions/scoped_key-py.md)
- [sessions/session_management.py](../api/sessions/session_management-py.md)
- [sessions/session_output_service.py](../api/sessions/session_output_service-py.md)
- [sessions/session_run_service.py](../api/sessions/session_run_service-py.md)
- [sessions/session_state_access.py](../api/sessions/session_state_access-py.md)
- [sessions/session_status.py](../api/sessions/session_status-py.md)
- [sessions/session_ui.py](../api/sessions/session_ui-py.md)

## When to update
- Any commit touching `sessions/**`.
- Any commit touching `agent/**` because this node has import/call dependency on it.
- Any commit touching `app/**` because this node has import/call dependency on it.
- Any commit touching `bot.py` because this node has import/call dependency on it.
- Any commit touching `desktop/**` because this node has import/call dependency on it.
- Any commit touching `i18n/**` because this node has import/call dependency on it.
- Any architecture or behavior change affecting this area.

## Related nodes
- `nodes/agent.md`
- `nodes/app.md`
- `nodes/bot-py.md`
- `nodes/desktop.md`
- `nodes/i18n.md`
- `nodes/locales.md`
- `nodes/miniapp.md`
- `nodes/modes.md`
- `agent` confidence=0.88 via L0
- `app` confidence=0.90 via L0/L1/L2
- `bot.py` confidence=0.77 via L0
- `desktop` confidence=0.88 via L0
- `i18n` confidence=0.90 via L1/L2
- `locales` confidence=0.66 via L0
- `miniapp` confidence=0.88 via L0
- `modes` confidence=0.90 via L0/L1/L2

## Owner
- project-maintainers

## Last reviewed
- 2026-07-13T10:46:52Z
