# Node: sessions

Generated: 2026-06-03T02:24:29Z

## Purpose
Транспорт-агностичный Service Layer для жизненного цикла сессий: идентичность/скоупинг сессии, очередь пользовательского ввода, запуск prompt-задач, доставка вывода (с HTML/summary), доступ к runtime-состоянию, построение статус-текста и Telegram-UI меню сессий. Используется ботом и режимами через сервисы; сами модули не содержат логики конкретных режимов.

## Scope
- Source glob: `sessions/**`
- Files: 10 (`sessions/*.py`, ~2890 строк), пакет помечен `sessions/__init__.py`.
- В скоуп НЕ входит dataclass `Session`/`SessionManager` — он живёт в корневом `session.py` (см. `nodes/session-py.md`); модули этой области работают над его экземплярами.

## Instructions for agent
- Read only files relevant to the active task; не грузить всю область целиком.
- Идентичность/ключи сессии: `sessions/conversation_scope.py` (`ConversationScope`, `DesktopScope`, `session_uid`/`session_surface`) и `sessions/scoped_key.py` (`build_session_scoped_key`, `sanitize_scoped_key_token`). Менять формат ключа `{chat_id}_{session_id}` только согласованно с персистентностью `state.json`.
- Чтение/запись runtime-флагов сессии (active_mode, orchestrator, ssh_remote, remote_control, analyst_mode) — только через геттеры/сеттеры `sessions/session_state_access.py`, не обращаться к атрибутам напрямую.
- Очередь ввода нормализуется через `sessions/queue_item.py` (`normalize_queue_item`, `append_session_queue_item`); метаданные ограничены полями `image_path`, `image_paths`, `attachments`.
- Доставка вывода и рендер крупных логов в HTML/summary — `sessions/session_output_service.py`; запуск prompt-задач и интеграция с оркестрацией — `sessions/session_run_service.py`.
- Prefer deterministic checks before edits; держать изменения минимальными и валидировать через `pytest -q` и `flake8 .`.

## Source of truth
- `sessions/**`
- `sessions/__init__.py` — маркер пакета.
- `sessions/conversation_scope.py` — `ConversationScope`/`DesktopScope`: идентичность сессии по `chat_id`/`message_thread_id`, `session_uid()`, `to_payload()`/`from_payload()`.
- `sessions/scoped_key.py` — построение и санитизация составных scoped-ключей сессии.
- `sessions/queue_item.py` — `SessionQueueItem` и нормализация/добавление элементов очереди ввода.
- `sessions/session_management.py` — `SessionManagement`, `PendingInput`: персист сессий, отмена mode-задач, очистка буферов/медиа/pending-вопросов, трекинг задач (привязан к `bot_app`).
- `sessions/session_run_service.py` — `SessionRunService`: `start_session_task`, `start_prompt_task`, assistant-preview, runtime-progress, интеграция с оркестрацией.
- `sessions/session_output_service.py` — `SessionOutputService`: `send_output`, рендер HTML в файл для крупного вывода, summary, разрешение notification-scope.
- `sessions/session_state_access.py` — чистые геттеры/сеттеры runtime-состояния сессии (mode/orchestrator/ssh/remote/analyst).
- `sessions/session_status.py` — построение статус-текста сессии/режимов, видимые/зарегистрированные режимы, краткий runtime-progress.
- `sessions/session_ui.py` — `SessionUI`: меню сессий, обработка callback'ов и pending-сообщений, контроль доступа, сброс полей сессии.

## Module API
Детальные интерфейсы модулей этой области:

- [sessions/conversation_scope.py](../api/sessions/conversation_scope-py.md)
- [sessions/queue_item.py](../api/sessions/queue_item-py.md)
- [sessions/scoped_key.py](../api/sessions/scoped_key-py.md)
- [sessions/session_management.py](../api/sessions/session_management-py.md)
- [sessions/session_output_service.py](../api/sessions/session_output_service-py.md)
- [sessions/session_run_service.py](../api/sessions/session_run_service-py.md)
- [sessions/session_state_access.py](../api/sessions/session_state_access-py.md)
- [sessions/session_status.py](../api/sessions/session_status-py.md)
- [sessions/session_ui.py](../api/sessions/session_ui-py.md)

## When to update
- Any commit touching `sessions/**`.
- Изменение dataclass `Session`/`SessionManager` в `session.py` (поля состояния, scoped-ключи, persistence) — импортируется этой областью.
- Изменение контрактов `app/services/**`, на которые опираются модули (напр. `session_run_service`, `session_interrupt_service`, `state_repository`, `advanced_orchestrator_service`, `runtime_progress_service`).
- Изменение helpers в `utils/` (`utils.html_renderer`, `utils.text`, `utils.ui`) или `summary.py`, используемых при доставке вывода.
- Изменение SDK режимов (`modes/sdk/**`) в части callback-данных/UI-скоупов, используемых `session_ui.py`/`session_status.py`.
- Изменение точек входа транспорта (`bot.py`, `desktop/**`, `miniapp/**`), которые конструируют/вызывают эти сервисы.
- Любое архитектурное/поведенческое изменение в этой области.

## Related nodes
- `nodes/session-py.md` — dataclass `Session`/`SessionManager`, над которым работают эти сервисы.
- `nodes/app.md` — `app/services/*` (оркестрация, state-repository, interrupt, прогресс): основной потребитель и зависимость.
- `nodes/summary-py.md` — суммаризация вывода (`summarize_text_with_reason`).
- `nodes/modes.md` — `modes/sdk/*` (callback_data, UI-скоупы) для статуса/меню.
- `nodes/bot-py.md` — composition root, конструирует `SessionManagement`/сервисы.
- `nodes/desktop.md`, `nodes/miniapp.md` — транспорты, использующие сервисы сессий.
- `nodes/agent.md` — потребитель состояния сессии через `session_state_access`.
- Edges (из графа): `app` confidence=0.90 via L0/L1/L2 · `session.py` confidence=0.90 via L0/L2 · `summary.py` confidence=0.90 via L0/L2 · `modes` confidence=0.90 via L0/L1/L2 · `agent`/`bot.py`/`desktop`/`miniapp` confidence=0.76 via L0.

## Owner
- project-maintainers

## Last reviewed
- 2026-06-03 (enriched in place; based on `sessions/*.py` at commit 5193643)
