# API Spec: `desktop/widgets/config_editor.py`

Generated: 2026-06-17T10:46:18Z

## Classes
### `class _JsonEditDialog(QDialog)` (line 229)
*Generic dialog to create/edit a JSON object.*
- `def __init__(title, initial, lang, parent)` (line 232)
- `def get_result()` (line 276)

### `class PresetsDialog(QDialog)` (line 280)
*CRUD dialog for config.presets (array of {name, prompt}).*
- `def __init__(presets, lang, parent)` (line 283)
- `def get_presets()` (line 392)

### `class MCPClientsDialog(QDialog)` (line 397)
*Dialog for viewing/editing MCP clients list as JSON.*
- `def __init__(clients, lang, parent)` (line 400)
- `def get_clients()` (line 466)

### `class DiffDialog(QDialog)` (line 470)
*Dialog to display unified diff before saving.*
- `def __init__(diff_text, lang, parent)` (line 473)

### `class ConfigEditorWidget(QWidget)` (line 500)
*Widget for editing application configuration.*
- `def __init__(config_service, facade, parent)` (line 507)
- `def load_config()` (line 552)
  - *Load configuration from service and update UI.*
- `def retranslate_ui(lang)` (line 1868)
