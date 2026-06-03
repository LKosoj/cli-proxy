# API Spec: `modes/admin/runbook_builder.py`

Generated: 2026-06-03T02:24:29Z

## Classes
### `class RunbookBuilderError(RuntimeError)` (line 46)
*Raised when a runbook cannot be built (bad input or I/O failure).*

### `class ScriptInput` (line 51)
*Представление одного скрипта в спецификации билдера.*
- `def validated()` (line 57)

### `class BuildSpec` (line 73)

## Symbols
- `def scripts_dir(workdir, rb_id)` (line 99)
- `def build_runbook_from_scripts(workdir, spec)` (line 120)
  - *Материализует BuildSpec:*
