# API Spec: `modes/admin/autonomy_policy.py`

Generated: 2026-06-03T02:24:28Z

## Classes
### `class AutonomyPolicy` (line 19)
*Политика автономии: что агенту разрешено делать самостоятельно.*
- `def permits_severity(severity)` (line 38)
- `def permits_action(action_id)` (line 44)
- `def permits_adhoc_argv(argv)` (line 50)
- `def for_server(server_id)` (line 59)

## Symbols
- `def load_autonomy_policy(admin_cfg)` (line 149)
  - *Парсит `admin.autonomy` из конфига:*
