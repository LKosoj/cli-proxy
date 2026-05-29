# API Spec: `desktop/widgets/git_panel.py`

Generated: 2026-04-27T22:43:23Z

## Classes
### `class GitPanelWidget(QWidget)` (line 31)
*Улучшенный виджет для операций Git: статус, история, коммит, получение и отправка изменений.*
- `def __init__(facade, parent)` (line 37)
- `def set_session(session)` (line 206)
  - *Устанавливает активную сессию и обновляет статус Git.*
- `def refresh_status()` (line 284)
  - *Асинхронно запрашивает и отображает статус Git.*
- `def refresh_history()` (line 290)
  - *Асинхронно запрашивает и отображает историю Git.*
- `def show_commit_diff()` (line 327)
  - *Показывает diff выбранного коммита.*
