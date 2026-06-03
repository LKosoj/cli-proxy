# API Spec: `desktop/widgets/config_editor.py`

Generated: 2026-06-03T02:24:29Z

## Classes
### `class DiffDialog(QDialog)` (line 110)
*Dialog to display unified diff before saving.*
- `def __init__(diff_text, parent)` (line 113)

### `class ConfigEditorWidget(QWidget)` (line 140)
*Widget for editing application configuration.*
- `def __init__(config_service, parent)` (line 147)
- `def load_config()` (line 183)
  - *Load configuration from service and update UI.*
