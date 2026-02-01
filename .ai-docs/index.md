# Документация проекта

## Обзор архитектуры

`cli-proxy` — это автономный Telegram-бот для управления CLI-сессиями с ИИ-инструментами (Codex, Claude, Gemini, Qwen и др.). Работает в режиме polling, не требует внешних серверов или туннелей. Обеспечивает глубокую интеграцию с локальной средой: файловой системой, Git, MTProto, OpenAI.

Ключевые особенности:
- Управление сессиями через inline-меню
- Поддержка headless и интерактивных режимов
- Автовосстановление сессий после перезапуска
- HTML-рендеринг вывода с поддержкой ANSI, Markdown, Mermaid
- Работа с файлами и изображениями
- Git-операции через Telegram
- MCP-совместимость (опционально)

---

## Основные компоненты

### `BotApp`
Центральный класс бота. Управляет:
- Обработкой команд и сообщений
- Сессиями через `SessionManager`
- Интерактивными меню (`SessionUI`, `DirsUI`, `GitOps`)
- Интеграциями: MTProto, MCP, OpenAI

**Инициализация**:
```python
app = BotApp(config)
app.build_app()  # настройка обработчиков
app.main()       # запуск polling
```

---

### `SessionManager`
Управляет жизненным циклом сессий. Гарантирует потокобезопасность через `run_lock`.

**Функции**:
- Создание/удаление сессий
- Переключение активной сессии
- Восстановление сессий из `state.json`
- Контроль очереди команд

**Восстановление**:
```python
sessions = restore_sessions(config, state_path)
```
Загружает сессии из `state.json`, восстанавливает `resume_token`, очередь, активную сессию.

---

### `Session`
Инкапсулирует состояние CLI-сессии. Работает через `pexpect` и `asyncio`.

**Режимы**:
- `headless`: однократный запуск команды
- `interactive`: постоянная сессия с поддержкой `resume`

**Ключевые параметры**:
- `tool`: имя инструмента (codex, claude и т.д.)
- `workdir`: рабочая директория
- `resume_token`: токен для возобновления
- `image_cmd`: обработка изображений

**Обнаружение состояния**:
- `prompt_regex` — определяет готовность сессии
- `resume_regex` — извлекает токен из вывода
- "Тик-токены" — активность в выводе

---

### `SessionUI`
Предоставляет интерфейс управления сессиями через Telegram.

**Функции**:
- `build_sessions_menu()` — список сессий с индикаторами:
  - Занятость
  - Git-состояние
  - Время работы
  - Длина очереди
- Обработка колбэков: выбор, активация, переименование, изменение `resume_token`
- Управление очередью: просмотр, очистка

**Ожидание ввода**:
- `pending_session_rename` — ожидание нового имени
- `pending_session_resume` — ожидание нового токена
- Поддержка отмены: `-`, `отмена`

---

### `GitOps`
Интеграция Git-операций в Telegram.

**Поддерживаемые операции**:
- `status`, `log`, `diff`, `summary`
- `fetch`, `pull` (с `--ff-only`), `push`
- `commit` с подтверждением
- `merge`, `rebase`, `stash`
- Работа с конфликтами: просмотр, `abort`, `continue`

**Безопасность**:
- Аутентификация через `GIT_ASKPASS` (токен не в истории)
- Подтверждение всех операций
- Автоопределение upstream-ветки

**Интеграции**:
- Генерация сообщений коммита через OpenAI
- HTML-справка из `git.md`

---

### `MTProtoUI`
Интерфейс для отправки сообщений в Telegram-чаты через MTProto (Telethon).

**Требования**:
```yaml
mtproto:
  enabled: true
  api_id: 12345
  api_hash: "abcde"
  session_string: "12345:abcdef..."
  targets:
    - title: "Сохранённые"
      peer: "me"
```

**Функции**:
- `show_menu()` — выбор цели
- `request_task()` — ввод сообщения
- `send_text()`, `send_file()` — отправка
- Поддержка отмены

---

### `MCPBridge`
TCP-сервер для внешних клиентов (MCP-совместимость).

**Запрос**:
```json
{"token": "secret", "prompt": "ls -la", "session_id": "codex::/work"}
```

**Ответ**:
```json
{"ok": true, "output": "file1.txt\nfile2.txt"}
```

**Настройки**:
```yaml
mcp:
  enabled: true
  host: "127.0.0.1"
  port: 8080
  token: "secret"
```

---

## Конфигурация (`config.yaml`)

### Telegram
```yaml
telegram:
  token: "BOT_TOKEN"
  whitelist_chat_ids: [123456789]
```

### Инструменты
```yaml
tools:
  codex:
    mode: headless
    cmd: "codex '{prompt}'"
    resume_cmd: "codex --thread {resume}"
    resume_regex: "thread_id=([a-f0-9]+)"
    env:
      OPENAI_API_KEY: "${OPENAI_API_KEY}"
```

### Пути и лимиты
```yaml
defaults:
  workdir: "/home/user/work"
  state_path: "state.json"
  log_path: "bot.log"
  image_temp_dir: "/tmp/images"
  image_max_mb: 10
  idle_timeout_sec: 30
```

### MTProto
```yaml
mtproto:
  enabled: false
  api_id: 12345
  api_hash: "abcde"
  session_string: "..."
  targets:
    - title: "Me"
      peer: "me"
```

### MCP
```yaml
mcp:
  enabled: false
  host: "127.0.0.1"
  port: 8080
  token: "secret"
```

### Presets
```yaml
presets:
  tests: "Run all tests"
  lint: "Check code style"
```

---

## Работа с файлами

### Поддерживаемые типы
- **Текстовые** (до 300 КБ): вставляются в промпт
- **Изображения**: обрабатываются через `image_cmd`, запрос из подписи

### Безопасность
- Проверка `is_within_root()` — защита от выхода за пределы `workdir`
- Ограничение размера: `image_max_mb`
- Проверка MIME-типов

---

## Обработка вывода

### ANSI → HTML
- Цвета, жирный текст через `<span>`
- Использует `ansi2html`

### Markdown → HTML
- Поддержка списков, таблиц, tasklists
- Через `markdown-it-py`

### Mermaid
- Блоки ```mermaid``` → SVG через `https://mermaid.ink/svg/`
- Таймаут 10 сек

### Буферизация
- Длинные сообщения буферизуются
- Отправка с задержкой через `_flush_after_delay`
- Полный вывод — как временный HTML-файл

---

## Состояние и данные

### `state.json`
Хранит:
- Сессии: `{tool}::{workdir}` → `SessionState`
- Активную сессию: `_active`
- Дополнительные данные: `_sessions`

**Поля `SessionState`**:
- `tool`, `workdir`, `resume_token`, `name`
- `summary`, `updated_at`

### `toolhelp.json`
Кэш справки по инструментам:
```json
{
  "codex": {
    "tool": "codex",
    "content": "/help, /status, /reset...",
    "updated_at": 1712345678
  }
}
```

---

## Запуск и тестирование

### Установка зависимостей
```bash
pip install -r requirements.txt
```

### Запуск
```bash
python bot.py
```

### Тестирование
```bash
pytest -q
```

---

## Безопасность

- Доступ только по `whitelist_chat_ids`
- Проверка путей: `is_within_root()`
- Ограничение размера вложений
- Токены в переменных окружения: `${GITHUB_TOKEN}`
- MTProto: сессия хранится зашифрованно

---

## Дорожная карта

### Фаза 1: Надёжность
- Блокировка сессий
- Приоритизация команд
- Единый журнал событий
- Разделение статусов CLI/Git

### Фаза 2: Интеграции
- MCP-бридж
- Git-шаблоны (PR, коммиты)
- Preset-кнопки: тесты, линтинг

### Фаза 3: Масштабирование
- Профили (dev/prod)
- Метрики и мониторинг
- Аудит и контроль команд
