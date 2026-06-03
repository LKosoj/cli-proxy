# API Spec: `modes/admin/runbooks.py`

Generated: 2026-06-03T02:24:29Z

## Classes
### `class RunbookError(RuntimeError)` (line 19)
*Raised when a runbook file is malformed.*

### `class Runbook` (line 24)
- `def as_dict()` (line 36)
- `def auto_action()` (line 50)

## Symbols
- `def global_runbooks_dir(workdir)` (line 57)
- `def server_runbooks_dir(workdir, server_id)` (line 61)
- `def load_runbooks(workdir)` (line 65)
- `def match_runbooks(runbooks)` (line 131)
  - *Возвращает отсортированный по релевантности список runbook'ов.*
- `def summarize_runbooks(runbooks)` (line 195)
