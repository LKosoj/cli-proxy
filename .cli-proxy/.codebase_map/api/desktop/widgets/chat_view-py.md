# API Spec: `desktop/widgets/chat_view.py`

Generated: 2026-06-03T02:24:29Z

## Classes
### `class ChatViewWidget(QWidget)` (line 25)
*Улучшенный виджет отображения истории чата и ввода сообщений.*
- `def __init__(logger)` (line 35)
- `def set_theme_colors(colors)` (line 71)
  - *Обновляет цвета темы для отрисовки HTML-сообщений.*
- `def eventFilter(obj, event)` (line 237)
  - *Перехват клавиш для отправки и навигации по истории.*
- `def append_progress_message(role, text)` (line 406)
  - *Добавление progress-сообщения; границы блока сохраняются в self._progress_cursor.*
- `def update_progress_message(role, text)` (line 428)
  - *Обновление текущего progress-блока. Возвращает True если обновлено.*
- `def clear_progress_message()` (line 443)
  - *Удаление progress-блока из документа.*
- `def append_message(role, text, attachments)` (line 463)
  - *Добавление сообщения в историю. Сбрасывает progress_message_id.*
- `def update_last_message(role, text)` (line 472)
  - *Обновление содержимого последнего сообщения (для стриминга).*
- `def set_assistant_preview(text)` (line 485)
- `def clear_assistant_preview()` (line 491)
- `def show_ask_options(options)` (line 552)
  - *Показать кнопки вариантов ответа для ask_user.*
- `def hide_ask_options()` (line 596)
  - *Скрыть и очистить панель кнопок ask_user.*
- `def clear_history()` (line 616)
  - *Очистка истории.*
- `def set_loading(loading)` (line 621)
  - *Индикация процесса загрузки.*
