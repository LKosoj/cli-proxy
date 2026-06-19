# API Spec: `app/mode_dependencies.py`

Generated: 2026-06-17T10:46:18Z

## Classes
### `class RunArtifactsService` (line 39)
- `def is_enabled()` (line 44)
- `def retention_window_days()` (line 47)

### `class SkillRuntimeService` (line 55)
- `def is_selection_enabled()` (line 62)
- `def allows_auto_discovery()` (line 65)
- `def allows_source(source)` (line 68)
- `def registry_path_list()` (line 71)
- `def registry_service()` (line 75)
- `def policy_service()` (line 79)
- `def promote_to_global()` (line 82)
- `def promote_run_skills()` (line 85)
- `def clear_cache()` (line 88)

### `class ModeFoundationServices` (line 96)

### `class ModeDependencies` (line 177)
*Typed mode-level dependencies shared across plugins.*
- `def with_overrides()` (line 202)

## Symbols
- `def build_mode_foundation_services(config)` (line 105)
