# API Spec: `desktop/widgets/config_editor.py`

Generated: 2026-04-27T22:43:23Z

## Classes
### `class DiffDialog(QDialog)` (line 39)
*Dialog to display unified diff before saving.*
- `def __init__(diff_text, parent)` (line 42)

### `class ConfigEditorWidget(QWidget)` (line 69)
*Widget for editing application configuration.*
- `def __init__(config_service, parent)` (line 76)
- `def load_config()` (line 111)
  - *Load configuration from service and update UI.*
