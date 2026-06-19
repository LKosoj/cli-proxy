# API Spec: `desktop/widgets/command_palette.py`

Generated: 2026-06-17T10:46:18Z

## Classes
### `class CommandPaletteItem` (line 23)
*Описывает пункт command palette.*

### `class CommandPaletteDialog(QDialog)` (line 34)
*Компактная палитра команд для быстрой навигации и действий.*
- `def __init__(parent)` (line 39)
- `def retranslate_ui(lang)` (line 78)
- `def set_commands(items)` (line 85)
- `def set_recent_commands(recent_ids)` (line 89)
- `def open_with_query(query)` (line 101)
- `def keyPressEvent(event)` (line 110)
