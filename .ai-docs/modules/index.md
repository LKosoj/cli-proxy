# Модули

```markdown
# Модули

## SessionUI — Управление сессиями через Telegram

Класс `SessionUI` предоставляет интерактивный интерфейс для управления сессиями через Telegram-бота с использованием inline-кнопок. Позволяет просматривать, выбирать, переименовывать сессии, обновлять токены возобновления и управлять активной сессией.

### Поля

- `config` — объект конфигурации приложения.
- `manager` — экземпляр `SessionManager` для управления сессиями.
- `_send_message` — асинхронная функция отправки сообщений в Telegram.
- `_format_ts` — функция форматирования временных меток.
- `_short_label` — функция усечения текста (например, для отображения в кнопках).
- `pending_session_rename` — словарь `chat_id → session_id`, хранящий сессии, ожидающие переименования.
- `pending_session_resume` — словарь `chat_id → session_id`, хранящий сессии, ожидающие обновления токена возобновления.

### Методы

#### `build_sessions_menu() → InlineKeyboardMarkup`

Создаёт inline-клавиатуру со списком всех доступных сессий. Каждая кнопка содержит имя сессии и её ID. Доступны действия: выбор активной, переименование, обновление токена, просмотр статуса.

**Возвращает:**  
`InlineKeyboardMarkup` — готовая разметка для отправки в Telegram.

---

#### `handle_pending_message(chat_id: int, text: str, context: ContextTypes.DEFAULT_TYPE) → bool`

Обрабатывает текстовые сообщения от пользователя в режиме ожидания ввода (например, при переименовании сессии или вводе нового токена возобновления).

**Аргументы:**
- `chat_id` — идентификатор чата.
- `text` — введённый пользователем текст.
- `context` — контекст выполнения бота.

**Возвращает:**  
`True`, если сообщение было обработано как ожидающий ввод; `False`, если не найдено соответствующее ожидание.

---

#### `handle_callback(query, chat_id: int, context: ContextTypes.DEFAULT_TYPE) → bool`

Обрабатывает нажатия на inline-кнопки в меню сессий. Поддерживает действия:
- Выбор сессии как активной.
- Переименование.
- Обновление токена возобновления.
- Просмотр статуса.
- Управление очередью.

**Аргументы:**
- `query` — объект callback-запроса от Telegram.
- `chat_id` — идентификатор чата.
- `context` — контекст выполнения.

**Возвращает:**  
`True`, если callback был распознан и обработан; `False` — в противном случае.

---

#### `get_state(state_path: str, tool_name: str, workdir: str) → Optional[SessionState]`

Возвращает состояние сессии по пути к файлу, имени инструмента и рабочей директории.

**Аргументы:**
- `state_path` — путь к файлу состояния.
- `tool_name` — имя инструмента.
- `workdir` — рабочая директория сессии.

**Возвращает:**  
`SessionState` при успехе, `None` — если состояние не найдено.

---

#### `_send_message(context: Context, chat_id: int, text: str) → None`

Отправляет текстовое сообщение пользователю через Telegram-бота.

**Аргументы:**
- `context` — контекст выполнения.
- `chat_id` — идентификатор получателя.
- `text` — текст сообщения.

---

#### `_format_ts(timestamp: float) → str`

Форматирует Unix-временную метку в человекочитаемую строку (например, `2025-04-05 14:30:22`).

**Аргументы:**
- `timestamp` — временная метка в формате Unix time.

**Возвращает:**  
Строка с датой и временем.
```

## Список модулей

- [modules/bot__py](bot__py.md)
- [modules/command_registry__py](command_registry__py.md)
- [modules/config__py](config__py.md)
- [modules/dirs_ui__py](dirs_ui__py.md)
- [modules/git_ops__py](git_ops__py.md)
- [modules/mcp_bridge__py](mcp_bridge__py.md)
- [modules/metrics__py](metrics__py.md)
- [modules/mtproto_ui__py](mtproto_ui__py.md)
- [modules/session__py](session__py.md)
- [modules/session_ui__py](session_ui__py.md)
- [modules/state__py](state__py.md)
- [modules/summary__py](summary__py.md)
- [modules/toolhelp__py](toolhelp__py.md)
- [modules/utils__py](utils__py.md)
