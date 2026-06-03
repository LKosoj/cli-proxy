# API Spec: `modes/admin/baseline.py`

Generated: 2026-06-03T02:24:28Z

## Classes
### `class BaselineError(RuntimeError)` (line 38)
*Raised when baseline scan/load fails.*

### `class BaselineCheck` (line 43)

### `class ServerSpec` (line 52)

### `class AdminBaselineScanner` (line 245)
- `def __init__()` (line 246)
- `def checks()` (line 260)
- `async def scan(server)` (line 263)

## Symbols
- `def default_checks()` (line 175)
- `def baseline_path(workdir, server_id)` (line 339)
- `def proposed_baseline_path(workdir, server_id)` (line 343)
- `def prev_baseline_path(workdir, server_id)` (line 347)
- `def load_baseline(workdir, server_id)` (line 351)
- `def load_proposed_baseline(workdir, server_id)` (line 364)
- `def write_baseline(workdir, server_id, profile)` (line 377)
- `def write_proposed_baseline(workdir, server_id, profile)` (line 384)
- `def apply_scan_result(workdir, server_id, profile)` (line 391)
  - *Политика обновления baseline:*
- `def accept_proposed_baseline(workdir, server_id)` (line 410)
  - *Переносит proposed → baseline, старый baseline → baseline.prev.*
- `def discard_proposed_baseline(workdir, server_id)` (line 422)
