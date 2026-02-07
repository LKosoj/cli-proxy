# Модули

```markdown
### Модули

#### `session_ui`
Модуль предоставляет пользовательский интерфейс для управления сессиями через Telegram-бота. Он позволяет просматривать список сессий, выбирать активную, переименовывать, обновлять resume-токен, проверять статус и управлять очередью. Взаимодействие осуществляется через inline-кнопки и текстовые сообщения. Модуль отвечает за обработку пользовательских действий с сессиями в Telegram-интерфейсе, включая просмотр состояния, управление очередью и закрытие сессий. Он взаимодействует с менеджером сессий и системой хранения состояний, обеспечивая отображение информации и выполнение операций через callback-запросы. Логика обработки разделена по префиксам входящих данных, что позволяет маршрутизировать действия в зависимости от типа запроса.

**Ключевые структуры данных:**
- `SessionUI` — Класс для управления интерфейсом сессий в Telegram, включая построение меню, обработку колбэков и ввода.
- `Session` — объект, управляющий состоянием и очередью инструмента для конкретного пользователя.
- `SessionManager` — централизованное хранилище активных сессий с возможностью сохранения на диск.

**Методы:**
- `build_sessions_menu() -> InlineKeyboardMarkup` — Создаёт клавиатуру с списком всех сессий.
- `handle_pending_message(chat_id: int, text: str, context: ContextTypes.DEFAULT_TYPE) -> bool` — Обрабатывает текстовые сообщения от пользователей, ожидающие ввода (переименование, resume).
- `handle_callback(query, chat_id: int, context: ContextTypes.DEFAULT_TYPE) -> bool` — Обрабатывает нажатия на inline-кнопки меню сессий.
```

## Список модулей

- [modules/agent/__init____py](agent/__init____py.md)
- [modules/agent/agent_core__py](agent/agent_core__py.md)
- [modules/agent/contracts__py](agent/contracts__py.md)
- [modules/agent/dispatcher__py](agent/dispatcher__py.md)
- [modules/agent/executor__py](agent/executor__py.md)
- [modules/agent/heuristics__py](agent/heuristics__py.md)
- [modules/agent/manager__py](agent/manager__py.md)
- [modules/agent/manager_prompts__py](agent/manager_prompts__py.md)
- [modules/agent/manager_store__py](agent/manager_store__py.md)
- [modules/agent/mcp/__init____py](agent/mcp/__init____py.md)
- [modules/agent/mcp/http_client__py](agent/mcp/http_client__py.md)
- [modules/agent/mcp/jsonrpc__py](agent/mcp/jsonrpc__py.md)
- [modules/agent/mcp/manager__py](agent/mcp/manager__py.md)
- [modules/agent/mcp/stdio_client__py](agent/mcp/stdio_client__py.md)
- [modules/agent/memory_policy__py](agent/memory_policy__py.md)
- [modules/agent/memory_store__py](agent/memory_store__py.md)
- [modules/agent/openai_client__py](agent/openai_client__py.md)
- [modules/agent/orchestrator__py](agent/orchestrator__py.md)
- [modules/agent/planner__py](agent/planner__py.md)
- [modules/agent/plugins/__init____py](agent/plugins/__init____py.md)
- [modules/agent/plugins/ask_user__py](agent/plugins/ask_user__py.md)
- [modules/agent/plugins/auto_tts__py](agent/plugins/auto_tts__py.md)
- [modules/agent/plugins/base__py](agent/plugins/base__py.md)
- [modules/agent/plugins/chief__py](agent/plugins/chief__py.md)
- [modules/agent/plugins/codeinterpreter__py](agent/plugins/codeinterpreter__py.md)
- [modules/agent/plugins/ddg_image_search__py](agent/plugins/ddg_image_search__py.md)
- [modules/agent/plugins/delete_file__py](agent/plugins/delete_file__py.md)
- [modules/agent/plugins/edit_file__py](agent/plugins/edit_file__py.md)
- [modules/agent/plugins/fetch_page__py](agent/plugins/fetch_page__py.md)
- [modules/agent/plugins/github_analysis__py](agent/plugins/github_analysis__py.md)
- [modules/agent/plugins/gtts_text_to_speech__py](agent/plugins/gtts_text_to_speech__py.md)
- [modules/agent/plugins/haiper_image_to_video__py](agent/plugins/haiper_image_to_video__py.md)
- [modules/agent/plugins/list_directory__py](agent/plugins/list_directory__py.md)
- [modules/agent/plugins/manage_message__py](agent/plugins/manage_message__py.md)
- [modules/agent/plugins/manage_tasks__py](agent/plugins/manage_tasks__py.md)
- [modules/agent/plugins/memory__py](agent/plugins/memory__py.md)
- [modules/agent/plugins/movie_info__py](agent/plugins/movie_info__py.md)
- [modules/agent/plugins/prompt_perfect__py](agent/plugins/prompt_perfect__py.md)
- [modules/agent/plugins/read_file__py](agent/plugins/read_file__py.md)
- [modules/agent/plugins/reminders__py](agent/plugins/reminders__py.md)
- [modules/agent/plugins/run_command__py](agent/plugins/run_command__py.md)
- [modules/agent/plugins/schedule_task__py](agent/plugins/schedule_task__py.md)
- [modules/agent/plugins/search_files__py](agent/plugins/search_files__py.md)
- [modules/agent/plugins/search_text__py](agent/plugins/search_text__py.md)
- [modules/agent/plugins/search_web__py](agent/plugins/search_web__py.md)
- [modules/agent/plugins/send_file__py](agent/plugins/send_file__py.md)
- [modules/agent/plugins/show_me_diagrams__py](agent/plugins/show_me_diagrams__py.md)
- [modules/agent/plugins/stable_diffusion__py](agent/plugins/stable_diffusion__py.md)
- [modules/agent/plugins/task_management__py](agent/plugins/task_management__py.md)
- [modules/agent/plugins/text_document_qa__py](agent/plugins/text_document_qa__py.md)
- [modules/agent/plugins/use_cli__py](agent/plugins/use_cli__py.md)
- [modules/agent/plugins/web_research__py](agent/plugins/web_research__py.md)
- [modules/agent/plugins/website_content__py](agent/plugins/website_content__py.md)
- [modules/agent/plugins/wolfram_alpha__py](agent/plugins/wolfram_alpha__py.md)
- [modules/agent/plugins/write_file__py](agent/plugins/write_file__py.md)
- [modules/agent/plugins/youtube_transcript__py](agent/plugins/youtube_transcript__py.md)
- [modules/agent/profiles__py](agent/profiles__py.md)
- [modules/agent/session_store__py](agent/session_store__py.md)
- [modules/agent/tooling/__init____py](agent/tooling/__init____py.md)
- [modules/agent/tooling/constants__py](agent/tooling/constants__py.md)
- [modules/agent/tooling/helpers__py](agent/tooling/helpers__py.md)
- [modules/agent/tooling/loader__py](agent/tooling/loader__py.md)
- [modules/agent/tooling/mcp_plugin__py](agent/tooling/mcp_plugin__py.md)
- [modules/agent/tooling/registry__py](agent/tooling/registry__py.md)
- [modules/agent/tooling/spec__py](agent/tooling/spec__py.md)
- [modules/bot__py](bot__py.md)
- [modules/bot_logging__py](bot_logging__py.md)
- [modules/command_registry__py](command_registry__py.md)
- [modules/config__py](config__py.md)
- [modules/dirs_ui__py](dirs_ui__py.md)
- [modules/dotenv_loader__py](dotenv_loader__py.md)
- [modules/git_ops__py](git_ops__py.md)
- [modules/mcp_bridge__py](mcp_bridge__py.md)
- [modules/metrics__py](metrics__py.md)
- [modules/session__py](session__py.md)
- [modules/session_ui__py](session_ui__py.md)
- [modules/state__py](state__py.md)
- [modules/summary__py](summary__py.md)
- [modules/telegram_io__py](telegram_io__py.md)
- [modules/tg_markdown__py](tg_markdown__py.md)
- [modules/toolhelp__py](toolhelp__py.md)
- [modules/utils__py](utils__py.md)
