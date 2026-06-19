# API Spec: `desktop/widgets/chat_view.py`

Generated: 2026-06-17T10:46:18Z

## Classes
### `class ChatViewWidget(QWidget)` (line 26)
*Улучшенный виджет отображения истории чата и ввода сообщений.*
- `def __init__(logger)` (line 38)
- `def set_theme_colors(colors)` (line 75)
  - *Обновляет цвета темы для отрисовки HTML-сообщений.*
- `def eventFilter(obj, event)` (line 251)
  - *Перехват клавиш для отправки и навигации по истории.*
- `def append_progress_message(role, text)` (line 420)
  - *Добавление progress-сообщения; границы блока сохраняются в self._progress_cursor.*
- `def update_progress_message(role, text)` (line 442)
  - *Обновление текущего progress-блока. Возвращает True если обновлено.*
- `def clear_progress_message()` (line 457)
  - *Удаление progress-блока из документа.*
- `def append_message(role, text, attachments)` (line 477)
  - *Добавление сообщения в историю. Сбрасывает progress_message_id.*
- `def update_last_message(role, text)` (line 486)
  - *Обновление содержимого последнего сообщения (для стриминга).*
- `def set_assistant_preview(text)` (line 499)
- `def clear_assistant_preview()` (line 505)
- `def show_ask_options(options)` (line 566)
  - *Показать кнопки вариантов ответа для ask_user.*
- `def hide_ask_options()` (line 610)
  - *Скрыть и очистить панель кнопок ask_user.*
- `def clear_history()` (line 630)
  - *Очистка истории.*
- `def set_loading(loading)` (line 635)
  - *Индикация процесса загрузки.*
- `def retranslate_ui(lang)` (line 647)
