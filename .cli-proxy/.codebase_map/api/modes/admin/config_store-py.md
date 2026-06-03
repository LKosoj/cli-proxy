# API Spec: `modes/admin/config_store.py`

Generated: 2026-06-03T02:24:29Z

## Classes
### `class AdminConfigStoreError(RuntimeError)` (line 36)
*Raised when admin config bootstrap/read fails.*

### `class AdminConfigStore` (line 54)
- `def __init__(session_workdir)` (line 55)
- `def config_path()` (line 60)
- `def secrets_path()` (line 64)
- `def ensure_config()` (line 70)
- `def load_config()` (line 102)
- `def load_effective_config()` (line 124)
- `def build_effective_config(cls, payload)` (line 129)
- `def merge_generated_config(generated_payload)` (line 273)
- `def replace_generated_config(generated_payload)` (line 298)
- `def apply_scan_result()` (line 325)
- `def set_pinned_cli(cli_name)` (line 367)
- `def update_runtime()` (line 383)
- `def set_last_scan_at(scan_ts)` (line 427)
- `def validate_config(payload)` (line 436)

## Symbols
- `def default_admin_template_path()` (line 40)
- `def admin_config_path(session_workdir)` (line 44)
