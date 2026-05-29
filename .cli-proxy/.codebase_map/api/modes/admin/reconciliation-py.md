# API Spec: `modes/admin/reconciliation.py`

Generated: 2026-04-27T22:43:22Z

## Classes
### `class ServerReconcileReport` (line 36)

### `class TickReport` (line 50)
- `def summary()` (line 54)

### `class MaintenanceReport` (line 66)

### `class AdminReconciler` (line 71)
*Координирует: scan → compare to baseline → store snapshot → store drifts.*
- `def __init__(workdir)` (line 77)
- `async def reconcile_server(spec)` (line 96)
- `async def tick(server_specs)` (line 176)
- `def daily_maintenance(server_ids)` (line 193)
- `async def run_forever()` (line 224)
  - *Опциональный встроенный scheduler. Внешний runner_service может*
