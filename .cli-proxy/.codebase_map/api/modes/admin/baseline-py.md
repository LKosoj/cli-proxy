# API Spec: `modes/admin/baseline.py`

Generated: 2026-04-27T22:43:22Z

## Classes
### `class BaselineError(RuntimeError)` (line 38)
*Raised when baseline scan/load fails.*

### `class BaselineCheck` (line 43)

### `class ServerSpec` (line 52)

### `class AdminBaselineScanner` (line 244)
- `def __init__()` (line 245)
- `def checks()` (line 257)
- `async def scan(server)` (line 260)

## Symbols
- `def default_checks()` (line 174)
- `def baseline_path(workdir, server_id)` (line 329)
- `def proposed_baseline_path(workdir, server_id)` (line 333)
- `def prev_baseline_path(workdir, server_id)` (line 337)
- `def load_baseline(workdir, server_id)` (line 341)
- `def load_proposed_baseline(workdir, server_id)` (line 354)
- `def write_baseline(workdir, server_id, profile)` (line 367)
- `def write_proposed_baseline(workdir, server_id, profile)` (line 374)
- `def apply_scan_result(workdir, server_id, profile)` (line 381)
  - *Политика обновления baseline:*
- `def accept_proposed_baseline(workdir, server_id)` (line 400)
  - *Переносит proposed → baseline, старый baseline → baseline.prev.*
- `def discard_proposed_baseline(workdir, server_id)` (line 412)
