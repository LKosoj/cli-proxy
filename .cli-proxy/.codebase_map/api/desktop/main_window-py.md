# API Spec: `desktop/main_window.py`

Generated: 2026-06-17T10:46:18Z

## Classes
### `class StateJsonDialog(QDialog)` (line 61)
*Диалог просмотра state.json активной Desktop-сессии (read-only).*
- `def __init__(payload)` (line 64)

### `class MainWindow(QMainWindow)` (line 90)
*Главное окно приложения с навигацией.*
- `def __init__(facade, ui_state_service, logger)` (line 95)
- `def closeEvent(event)` (line 1659)
  - *Сохранение состояния при закрытии.*
