# Соглашения

## Именование и форматирование

- **Переменные и функции**: `snake_case` (например, `session_manager`, `build_keyboard`).
- **Классы**: `PascalCase` (например, `SessionUI`, `GitOps`).
- **Константы**: `UPPER_SNAKE_CASE` (например, `TOOL_TIMEOUT_MS`, `DIALOG_TIMEOUT`).
- **Файлы и модули**: `snake_case.py` (например, `session_manager.py`, `tool_registry.py`).
- **Папки плагинов**: `plugins/`, файлы — `*.py`, исключая `__init__.py`, `base.py`.

## Структура данных

- **Сессии**: ключ в формате `{tool}::{workdir}`.
- **Состояние**: хранится в `state.json`, активная сессия — в `_active`.
- **Конфигурация**: `config.yaml`, загружается через `load_config()`.
- **Логи**: `TimedRotatingFileHandler`, ротация в 03:00 UTC, `backupCount=1`.
- **Песочница**: `workdir/sandbox`, `AGENT_SANDBOX_ROOT` — корень для изолированных данных.

## Обработка ошибок

- Все операции чтения/записи файлов обрабатывают исключения с логированием.
- При отсутствии файла или некорректных данных возвращаются значения по умолчанию.
- Сетевые и API-ошибки логируются через `logging.exception`, возвращаются структурированные ответы с `{"success": false, "error": "..."}`.

## Безопасность

- Проверка путей: `is_within_root()`, `_resolve_within_workspace()`.
- Блокировка чувствительных файлов: `.env`, `id_rsa`, `*.pem`, `credentials`.
- Фильтрация команд: `BLOCKED_PATTERNS_PATH`, `check_command()`.
- Токены: передаются через `GIT_ASKPASS`, `GITHUB_TOKEN`, `ZAI_API_KEY` и др.

## Плагины

- Наследуют `ToolPlugin`, регистрируются в `ToolRegistry`.
- Инициализируются через `initialize(config, services)`.
- Используют `DialogMixin` для диалогов (наследуется первым).
- Имена инструментов: `{function_prefix}.{spec.name}`.
- Автоматическая загрузка из `plugins/`.

## Callback-данные

- Формат: `action:key:values`.
- Поддерживаемые префиксы:
  - `dlg:{plugin_id}:{payload}` — шаг диалога.
  - `cb:{plugin_id}:{action}` — автономное действие.
  - `dlg_cancel:{plugin_id}` — отмена диалога.
- Макс. длина `callback_data`: 64 байта.

## Диалоги

- Управляются через `DialogMixin`.
- Таймаут неактивности: `DIALOG_TIMEOUT` (по умолчанию 300 сек).
- Отмена: слова из `CANCEL_WORDS` (`отмена`, `cancel`, `-`).
- Состояние хранится в `DialogState` на уровне `chat_id`.

## Работа с файлами

- Макс. размер загрузки: 500 КБ (текст), 10 МБ (изображения).
- Временные файлы: `tempfile`, удаляются после использования.
- Кодировка: UTF-8, игнорирование ошибок декодирования.

## Переменные окружения

- Имеют приоритет над `config.yaml`.
- Примеры: `OPENAI_API_KEY`, `TAVILY_API_KEY`, `GITHUB_TOKEN`.
- Подстановка в команды: `${VAR}`.

## Логирование

- Централизованное через `logging`.
- Формат: `%(asctime)s - %(name)s - %(levelname)s - %(message)s`.
- Уровень: `INFO` по умолчанию.
- Отдельные логи: общий, ошибки, агент.

## Версионирование и совместимость

- Версии пакетов фиксируются в `requirements.txt` (например, `python-telegram-bot==20.7`).
- MCP-протокол: версия по умолчанию `2024-11-05`.
- JSON-файлы: отступы 2 пробела, `ensure_ascii=False`.
