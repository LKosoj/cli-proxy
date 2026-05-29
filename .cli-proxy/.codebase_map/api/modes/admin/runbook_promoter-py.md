# API Spec: `modes/admin/runbook_promoter.py`

Generated: 2026-04-27T22:43:22Z

## Classes
### `class RunbookPromoteError(RuntimeError)` (line 38)
*Raised when runbook promotion cannot proceed.*

### `class PromoteResult` (line 43)
- `def to_dict()` (line 51)

## Symbols
- `async def promote_runbook(workdir, rb_id)` (line 124)
  - *Добавляет серверы в `servers` списка runbook'а и (опционально) поднимает `auto_action.confidence`.*
