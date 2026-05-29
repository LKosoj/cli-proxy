# API Spec: `app/mode_dependencies.py`

Generated: 2026-04-27T22:43:22Z

## Classes
### `class RunArtifactsService` (line 37)
- `def is_enabled()` (line 42)
- `def retention_window_days()` (line 45)

### `class SkillRuntimeService` (line 53)
- `def is_selection_enabled()` (line 60)
- `def allows_auto_discovery()` (line 63)
- `def allows_source(source)` (line 66)
- `def registry_path_list()` (line 69)
- `def registry_service()` (line 73)
- `def policy_service()` (line 77)
- `def promote_to_global()` (line 80)
- `def promote_run_skills()` (line 83)
- `def clear_cache()` (line 86)

### `class ModeFoundationServices` (line 94)

### `class ModeDependencies` (line 169)
*Typed mode-level dependencies shared across plugins.*
- `def with_overrides()` (line 192)

## Symbols
- `def build_mode_foundation_services(config)` (line 102)
