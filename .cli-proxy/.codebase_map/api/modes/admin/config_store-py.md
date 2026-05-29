# API Spec: `modes/admin/config_store.py`

Generated: 2026-04-27T22:43:22Z

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
- `def merge_generated_config(generated_payload)` (line 229)
- `def replace_generated_config(generated_payload)` (line 254)
- `def apply_scan_result()` (line 281)
- `def set_pinned_cli(cli_name)` (line 323)
- `def update_runtime()` (line 339)
- `def set_last_scan_at(scan_ts)` (line 383)
- `def validate_config(payload)` (line 392)

## Symbols
- `def default_admin_template_path()` (line 40)
- `def admin_config_path(session_workdir)` (line 44)
