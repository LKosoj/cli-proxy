# API Spec: `miniapp/services/config_service.py`

Generated: 2026-04-27T22:43:23Z

## Classes
### `class ConfigValidationError(Exception)` (line 20)
*Raised when MiniApp config draft validation fails.*

### `class RevisionConflictError(Exception)` (line 24)
*Raised when config file revision changed during save.*

## Symbols
- `def app_config_to_dict(config)` (line 37)
- `def config_schema()` (line 50)
- `def validate_draft(app_config_path, draft)` (line 751)
- `def draft_diff(current, draft)` (line 790)
- `def save_draft(app_config_path, current_revision, expected_revision, draft)` (line 828)
- `def config_view_with_revision(config)` (line 848)
