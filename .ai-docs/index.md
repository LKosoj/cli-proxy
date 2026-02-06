# Документация проекта

## Обзор архитектуры

Проект представляет собой Telegram-бота для управления CLI-агентами (Codex, Gemini, Qwen, Claude) с поддержкой многопользовательских сессий, очередей команд и HTML-рендеринга. Основные компоненты:

- **`BotApp`** — центральный класс, координирующий сессии, команды и интеграции
- **`SessionManager`** — управление жизненным циклом сессий
- **`Session`** — активная сессия с атрибутами: `tool`, `workdir`, `agent_enabled`, `queue`, `busy`
- **`SessionUI`** — интерфейс для управления сессиями через Telegram
- **`GitOps`** — выполнение Git-операций
- **`MCPBridge`** — TCP-сервер для внешних клиентов
- **`Metrics`** — сбор статистики использования

## Управление сессиями

### Создание и выбор сессий

Сессии создаются через команду `/new` или `/newpath`. Каждая сессия изолирована в отдельном рабочем каталоге. Для выбора активной сессии используется `/sessions`, который отображает интерактивное меню.

```python
# Пример создания сессии
session = session_manager.create(tool="codex", workdir="/path/to/project")
session_manager.set_active(session.id)
```

### Интерфейс управления

Класс `SessionUI` предоставляет интерактивное меню через `InlineKeyboardMarkup` с поддержкой:

- Просмотра списка сессий (`build_sessions_menu`)
- Выбора активной сессии
- Переименования сессии
- Обновления resume-токена
- Проверки состояния
- Закрытия сессии

Обработка действий осуществляется через `handle_callback()` и `handle_pending_message()`.

### Жизненный цикл

1. Создание сессии → `create()`
2. Активация → `set_active()`
3. Выполнение команд → `run_prompt()`
4. Закрытие → `close()` с вызовом `_on_before_close` и `_on_close`

Состояние сессий сохраняется в `state.json` через `manager._persist_sessions()`.

## Конфигурация

### Файл config.yaml

```yaml
telegram:
  token: "YOUR_BOT_TOKEN"
  whitelist_chat_ids: [123456789]

defaults:
  workdir: "/path/to/workdir"
  log_path: "/path/to/logs"
  state_path: "/path/to/state.json"

tools:
  codex:
    cmd: "codex --headless {prompt}"
    resume_cmd: "codex --resume {resume}"
    image_cmd: "codex --image {image}"
    env:
      OPENAI_API_KEY: "${OPENAI_API_KEY}"

presets:
  tests: "Запустить тесты"
  lint: "Проверить код линтером"
```

### Переменные окружения

Приоритет: `env` в `tools.*` > `config.yaml` > системные переменные.

Поддерживаемые переменные:
- `TAVILY_API_KEY` — для поиска
- `JINA_API_KEY` — для веб-поиска
- `GITHUB_TOKEN` — для Git-операций
- `OPENAI_API_KEY` — для LLM

## Основные команды

### Основные (в меню)
- `/new`, `/sessions`, `/interrupt`, `/git`, `/files`, `/tools`, `/toolhelp`

### Скрытые
- `/dirs`, `/newpath`, `/use`, `/cwd`, `/setprompt`, `/send`, `/resume`, `/close`, `/status`, `/rename`, `/state`, `/clearqueue`, `/queue`, `/preset`, `/metrics`

### Прямой ввод
- Через `/send` или префикс `>`

## Git-интеграция

Класс `GitOps` предоставляет интерфейс для Git-операций:

- Просмотр состояния: `Status`, `Log`, `Diff`
- Синхронизация: `Fetch`, `Pull`
- Коммиты: `Commit`, `Push`
- Слияние: `Merge`, `Rebase`
- Работа с конфликтами

Требует `github_token` в конфиге для приватных репозиториев. Операции выполняются в очереди при занятой сессии.

## MCP-сервер

Класс `MCPBridge` реализует TCP-сервер для взаимодействия с ботом:

```json
// Запрос
{"token": "токен", "prompt": "текст запроса", "session_id": "идентификатор"}

// Ответ
{"ok": true, "output": "результат"}
{"ok": false, "error": "описание ошибки"}
```

Настройки в `config.yaml`:
```yaml
mcp:
  enabled: true
  host: "127.0.0.1"
  port: 8765
  token: "optional_token"
```

## Файловый менеджер

Поддержка работы с файлами:
- Загрузка текстовых файлов до 500 КБ
- Загрузка изображений (с обработкой при наличии `image_cmd`)
- Просмотр и отправка файлов через `/files`
- Навигация по каталогам с пагинацией

## Плагины и инструменты

### Реестр инструментов

`ToolRegistry` управляет плагинами:
- Автоматическая загрузка из директории `plugins`
- Регистрация через `register()`
- Выполнение через `execute()`
- Фильтрация по `allowed_tools`

### Поддерживаемые инструменты

- `read_file`, `write_file`, `delete_file` — работа с файлами
- `list_directory`, `search_files` — навигация
- `run_command` — выполнение shell-команд
- `ask_user` — взаимодействие с пользователем
- `search_web`, `fetch_page` — веб-поиск
- `youtube_transcript` — субтитры с YouTube
- `code_interpreter` — выполнение Python-кода

## Безопасность

### Фильтрация команд

Блокировка опасных паттернов:
- Утечка переменных окружения
- Доступ к чувствительным файлам
- Сетевое сканирование
- Выполнение вредоносных операций

### Ограничения
- Максимальный размер файла: 50 МБ
- Ограничение вывода: 3000 символов
- Таймауты выполнения: 30-120 секунд
- Проверка путей на выход за пределы `workdir`

## Запуск и тестирование

### Запуск
```bash
python bot.py
```

### Тестирование
```bash
pytest -q
```

## Логирование

Централизованное логирование с `TimedRotatingFileHandler`:
- Общий лог: `log_path`
- Ошибки: `error_log_path`
- Лог агента: `agent_log_path`

Ротация в 03:00 UTC, `backupCount=1`.
