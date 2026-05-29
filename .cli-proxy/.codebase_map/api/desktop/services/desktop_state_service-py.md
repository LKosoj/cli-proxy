# API Spec: `desktop/services/desktop_state_service.py`

Generated: 2026-04-27T22:43:22Z

## Classes
### `class DesktopUiState` (line 15)
*Модель состояния UI для сохранения между сессиями.*

### `class DesktopUiStateService` (line 35)
*Сервис управления состоянием UI (геометрия, вкладки, настройки).*
- `def __init__(facade, logger)` (line 38)
- `def is_ready()` (line 47)
- `async def wait_ready()` (line 50)
- `async def load()` (line 58)
  - *Загрузка состояния из файла (не блокирует event loop).*
- `async def save()` (line 88)
  - *Сохранение состояния (атомарно, не блокирует event loop).*
- `def shutdown()` (line 127)
