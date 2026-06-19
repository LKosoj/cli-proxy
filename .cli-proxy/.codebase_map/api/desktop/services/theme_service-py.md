# API Spec: `desktop/services/theme_service.py`

Generated: 2026-06-17T10:46:18Z

## Classes
### `class ThemeService(BaseThemeService)` (line 14)
*Улучшенный сервис для управления темами оформления приложения.*
- `def __init__(config_path, logger)` (line 95)
- `def set_theme(theme_name)` (line 128)
  - *Устанавливает тему по названию.*
- `def add_custom_theme(name, theme)` (line 137)
  - *Добавляет пользовательскую тему.*
- `def remove_custom_theme(name)` (line 146)
  - *Удаляет пользовательскую тему.*
- `def list_themes()` (line 157)
  - *Возвращает список доступных тем.*
- `def get_theme_colors()` (line 161)
  - *Возвращает цвета текущей темы.*
- `def get_current_theme_name()` (line 168)
  - *Возвращает название текущей темы.*
- `def get_main_stylesheet()` (line 186)
  - *Генерирует основной stylesheet для приложения.*
