# API Spec: `modes/admin/chat_schemas.py`

Generated: 2026-04-27T22:43:22Z

## Classes
### `class PlanStep` (line 85)
- `def as_dict()` (line 93)

### `class Intent` (line 109)
- `def as_dict()` (line 127)

### `class IntentValidationError(ValueError)` (line 159)
- `def __init__(message)` (line 160)

### `class ValidationContext` (line 166)
- `def action_definition()` (line 172)

## Symbols
- `def compile_denylist(extra_patterns)` (line 49)
- `def matches_denylist(command, compiled)` (line 64)
- `def argv_denylist_hit(argv)` (line 76)
- `def build_validation_context()` (line 177)
- `def parse_intent(payload)` (line 292)
- `def revalidate_intent(intent)` (line 444)
