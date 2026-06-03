# API Spec: `modes/admin/plugin_tools.py`

Generated: 2026-06-03T02:24:29Z

## Classes
### `class AdminToolError(RuntimeError)` (line 27)
*Raised when admin plugin tool cannot execute a request.*

### `class ActionRunResult` (line 32)
- `def to_dict()` (line 47)

## Symbols
- `def resolve_workdir(ctx)` (line 65)
- `def find_allowlisted_action(admin_cfg)` (line 77)
  - *Находит action в `admin.allowlist.<target>`. Возвращает dict с ключами:*
- `async def run_allowlisted_action()` (line 114)
  - *MVP-executor для plugin-tool `admin.execute_action`.*
- `def write_escalation()` (line 253)
- `def build_server_dossier()` (line 304)
