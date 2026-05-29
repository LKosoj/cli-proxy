# API Spec: `desktop/widgets/command_palette.py`

Generated: 2026-04-27T22:43:22Z

## Classes
### `class CommandPaletteItem` (line 21)
*Описывает пункт command palette.*

### `class CommandPaletteDialog(QDialog)` (line 32)
*Компактная палитра команд для быстрой навигации и действий.*
- `def __init__(parent)` (line 37)
- `def set_commands(items)` (line 75)
- `def set_recent_commands(recent_ids)` (line 79)
- `def open_with_query(query)` (line 91)
- `def keyPressEvent(event)` (line 100)
