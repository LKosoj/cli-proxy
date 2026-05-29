# Code Conventions

## Python

- Базовый стиль: 4 пробела, `snake_case` для функций/переменных/файлов, `PascalCase` для классов.
- Публичные API и границы модулей должны иметь type hints; примеры строгих моделей находятся в `app/config_runtime/models.py` и `modes/sdk/runtime/contracts.py`.
- Для схем конфигурации используется Pydantic v2: общий базовый класс `ConfigModel` в `app/config_runtime/models.py` задает `extra="forbid"`, `str_strip_whitespace=True`, `validate_default=True`.
- Для JSON/LLM payload нельзя добавлять разрозненный `json.loads` в mode/runtime-код: использовать `modes/sdk/runtime/json_normalizer.py` (`loads_safe`, `normalize_payload`, `parse_normalize_validate`).
- Для jsonschema-контрактов использовать централизованную нормализацию с дефолтами из `modes/sdk/runtime/json_normalizer.py`; ошибки валидации пробрасываются как `JSONSchemaValidationError`.

## Ошибки и логи

- Логгер: `logging.getLogger(__name__)` или dependency-injected logger для сервисов/виджетов (`app/security/audit.py`, `desktop/main_window.py`).
- Новые `except`-ветки должны логировать контекст через `logger.exception(...)`; это отражено в runtime-слоях `summary.py`, `modes/sdk/runtime/json_normalizer.py`, `app/security/audit.py`.
- Новый mode/runtime fallback не добавлять молча: legacy fallback допустим только для обратной совместимости и должен быть явно логирован.

## Telegram, Desktop, MiniApp

- Telegram-ответы по умолчанию проходят через общий Markdown/entities pipeline `tg/markdown.py`: `to_telegram_entities`, `split_telegram_entities`, `to_markdown_v2`.
- Локальные markdown-ссылки в Telegram нормализуются в `tg/markdown.py`, чтобы не ломать MarkdownV2 entities.
- При изменении runtime config синхронизировать `config.yaml`, `config_example.yaml`, `app/config_runtime/models.py`, `app/config_runtime/serialization.py`, `app/config_runtime/loader.py`, MiniApp config editor (`miniapp/services/config_service.py`, `miniapp/static/app.js`) и Desktop config UI (`desktop/widgets/config_editor.py`).
- README держать парно: `README.md` и `README_EN.MD`.
- Desktop UI-логика живет в `desktop/widgets/*`, orchestration - в `desktop/services/application_facade.py`.
- MiniApp web/API логика разделена между `miniapp/routes.py`, `miniapp/services/*`, `miniapp/static/app.js`, `miniapp/static/styles.css`.

## Modes

- Перед изменением mode читать `modes/DEVELOPMENT.md`.
- Mode-код должен идти через `BaseMode` и SDK-сервисы: `modes/sdk/base.py`, `modes/sdk/services/*`, `modes/sdk/orchestration.py`.
- Общую mode-логику не завязывать на `BotApp`; прямой доступ к `bot_app` допустим только для mode-specific gaps и требует явного обоснования.
- Callback dispatch делать через карту handler-ов и `BaseMode._dispatch_callback_action(...)`, а не через длинный `if/elif`.
- Фоновые задачи mode создавать через `BaseMode._start_mode_task(...)` или `TaskService`, чтобы они были видимы и отменяемы.
- Intent/LLM output: schema-first (`<mode>/schemas.py`) -> `parse_normalize_validate(...)` -> нормализация helper-ом -> тесты валидного и невалидного payload.

## Quality Gates

- Основные команды: `.venv/bin/pytest -q tests/test_file.py::test_name`, `.venv/bin/pytest -q tests/test_file.py`, `.venv/bin/flake8`.
- Полный `.venv/bin/pytest -q` запускать для release/smoke или изменений shared/runtime слоев (`modes/sdk/runtime/*`, `app/services/*`, `config.py`, `bot.py`).
- Python validation adapter ожидает toolchain `flake8` + `pytest -q`: `modes/sdk/runtime/validation/adapters/python.py`.

## Ограничения скана

- Обновлено по полному `rg --files` и выборочному чтению quality-related файлов; команды тестов/линтера в рамках map-only обновления не запускались.
