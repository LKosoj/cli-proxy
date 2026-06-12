# Node: summary.py

Generated: 2026-06-03T02:24:29Z

## Purpose
`summary.py` — модуль уровня Core: резюмирование длинных текстов и генерация git-commit сообщений через OpenAI-совместимый API. Используется для сжатия вывода CLI-агентов перед отправкой в Telegram и для предложения commit-сообщений в git-операциях.

## Scope
- Source glob: `summary.py`
- Estimated files: 1
- Единственный файл: `summary.py` (480 строк).

## Instructions for agent
- Публичные функции (см. `summary.py`):
  - `async summarize_text(text, max_chars=3000, config=None, *, language="ru")` — резюме; для текстов < 3000 символов возвращает очищенный текст без вызова API.
  - `async summarize_text_with_reason(...)` — то же, но возвращает `(summary, reason)` с типизированной обработкой ошибок OpenAI (`summary.py:237`).
  - `async suggest_commit_message_async(...)` — однострочное commit-сообщение (`summary.py:354`).
  - `async suggest_commit_message_detailed_async(...)` — `(summary, body)` через JSON-схему `_COMMIT_MESSAGE_RESPONSE_SCHEMA`, до `_COMMIT_MESSAGE_ATTEMPTS` (5) попыток (`summary.py:375`).
  - Синхронные обёртки `suggest_commit_message`/`suggest_commit_message_detailed` возвращают `None`, если уже есть запущенный event loop (`summary.py:461`).
- Модель берётся через `resolve_openai_config(config, model_key="openai_big_model", env_priority=False)` — это «big model», не основная.
- Клиенты `AsyncOpenAI` кэшируются по `(api_key, base_url)` в `_openai_clients` — не создавать новый клиент на каждый вызов.
- Промпты резюме многоязычны (`ru/en/zh/de`); `_tail_digest` использует русскоязычные маркеры результата (см. TODO `T2-tail-digest`, `summary.py:272`) — для других языков хвостовой дайджест почти не срабатывает.
- Прямой запуск API минимизировать: для коротких входов и при отсутствии конфигурации функции возвращают раньше без сетевого вызова.

## Source of truth
- `summary.py`
- Технические интерфейсы: `.cli-proxy/.codebase_map/api/summary-py.md`

## Module API
Детальные интерфейсы модулей этой области:

- [summary.py](../api/summary-py.md)

## When to update
- Любой коммит, затрагивающий `summary.py`.
- Изменение `modes/sdk/runtime/openai_client.py` (`create_async_openai_client`, `resolve_openai_config`) — прямая import-зависимость.
- Изменение `modes/sdk/runtime/json_normalizer.py` (`parse_normalize_validate`) — валидация commit-JSON.
- Изменение `config.py` (`AppConfig`, ключ `openai_big_model`) — источник модели/ключей.
- Изменение `utils/text.py` (`normalize_text`) — предобработка текста.
- Изменение `i18n/language_names.py` (`LANGUAGE_NAMES`) — ленивая зависимость в commit-функциях.
- Изменение потребителей: `bot.py`, `sessions/session_management.py`, `app/services/git_ops_service.py`, `desktop/widgets/git_panel.py`, `modes/sdk/runtime/context_summarizer.py` (импортирует `_get_openai_config`/`_get_openai_client`).
- Любое изменение поведения резюмирования или формата commit-сообщений.

## Related nodes
- `nodes/modes.md` — `modes/sdk/runtime/*` (OpenAI-клиент, JSON-нормализатор, context_summarizer): import-зависимость.
- `nodes/config-py.md` — `AppConfig`, ключ `openai_big_model`.
- `nodes/utils.md` — `utils/text.normalize_text`.
- `nodes/i18n.md` — `i18n/language_names.LANGUAGE_NAMES` (ленивый импорт).
- `nodes/bot-py.md` — потребитель `summarize_text_with_reason`.
- `nodes/sessions.md` — `sessions/session_management.py` потребитель `summarize_text_with_reason`.
- `nodes/app.md` — `app/services/git_ops_service.py` потребитель `suggest_commit_message_detailed_async`.
- `nodes/desktop.md` — `desktop/widgets/git_panel.py` потребитель `suggest_commit_message_detailed_async`.
- `nodes/tests.md` — `tests/test_summary_*`, `tests/test_openai_client_retries.py`, `tests/test_context_summarizer.py`.

## Owner
- project-maintainers

## Last reviewed
- 2026-06-12 (OpenAI client default X-Title header)
