# API Spec: `desktop/widgets/log_viewer.py`

Generated: 2026-04-27T22:43:23Z

## Classes
### `class LogSignalEmitter(QObject)` (line 32)
*Эмиттер сигналов для передачи логов в поток UI.*

### `class LogViewerWidget(QWidget)` (line 37)
*Улучшенный виджет для просмотра логов в реальном времени.*
- `def __init__(task_service, parent)` (line 40)
- `def set_theme_colors(colors)` (line 296)
  - *Обновляет цвета темы.*
- `def closeEvent(event)` (line 342)
