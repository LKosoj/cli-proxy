# Node: tg

Generated: 2026-06-03T02:24:29Z

## Purpose
Транспортный слой Telegram: канал взаимодействия пользователя с ботом через библиотеку `python-telegram-bot`. Бизнес-логики не содержит — делегирует в `app/services/*`, SDK-сервисы и mode-плагины (`modes/`, `agent/`). Точка входа — `tg/wiring.py:register_handlers(app, bot_app, config)`, вызывается из `bot.py:2730`: регистрирует pre-command-хендлер (`bot_app.on_pre_command`, group=-2), оборачивает core-команды из `build_command_registry` с проверкой доступа (`bot_app.ensure_telegram_inbound_authorized`), затем ставит плагин-хендлеры через `agent.telegram_wiring.install_plugin_handlers`. Команд-классы и обработчики инстанцируются в `BotApp.__init__` (`bot.py:288-294`): `CallbackHandler`, `MessageProcessor`, `FileUploadHandler`; командные методы приходят из `BotHandlers` (`tg/handlers.py`).

## Scope
- Source glob: `tg/**`
- Current files: 17 under `tg/**` as of last review.
- Подкаталог: `tg/callback_actions/` — доменные mixin'ы inline-callback'ов, собираемые в единый `CallbackActionsMixin`.

## Instructions for agent
- Это транспортный слой: бизнес-логику здесь не размещать. Маршрутизировать через `app/services/*`, SDK-сервисы и mode-плагины; к `BotApp` обращаться только как к контейнеру сервисов.
- Регистрация хендлеров — единственная точка `tg/wiring.py:register_handlers`. Новые core-команды добавлять в `build_command_registry` (`tg/command_registry.py`), а сам метод-обработчик — в `BotHandlers` (`tg/handlers.py`). Если команда должна работать вне топика — добавить её имя в `OUTSIDE_TOPIC_ALLOWED_COMMANDS` (`tg/command_policy.py`).
- Новые inline-callback'и добавлять как метод соответствующего mixin'а в `tg/callback_actions/*` и регистрировать prefix в `CallbackHandler._callback_prefix_handlers` (`tg/callbacks.py`). Сигнатура: `async def _cb_*(self, *, data, chat_id, query, context) -> bool`.
- Доступ — fail-closed: каждый входящий апдейт авторизуется через `bot_app.ensure_telegram_inbound_authorized` (см. обёртку в `tg/wiring.py`); scope (reply_chat_id/thread/owner/session) разрешать через `bot_app.resolve_telegram_callback_scope`, не вычислять вручную.
- Исходящие сообщения форматировать через `tg/markdown.py` (`to_markdown_v2`, `to_telegram_entities`) — по умолчанию MarkdownV2 (требование `CLAUDE.md`).
- Состояние upload/rename/media-group хранится в `bot_app.ui_state` (ChatUiState); `FileUploadHandler` (`tg/file_upload_handler.py`) только читает/пишет через этот общий объект — не дублировать состояние.
- Синхронизировать функциональность с Desktop (`desktop/`) и MiniApp (`miniapp/`) — общий контракт сервисов (требование `CLAUDE.md`).
- `tg/handlers.py` (~2135 строк), `tg/callbacks.py` (~840), `tg/callback_actions/session.py` (~646) большие — читать точечно через Grep. Изменения держать минимальными; валидировать `pytest -q tests/test_tg_handlers.py tests/test_markdown_v2_send_message.py tests/test_telegram_ingress_security.py tests/test_mode_callback_routing.py`.

## Source of truth
- `tg/wiring.py` — `register_handlers`: монтаж pre-command/command/callback/message-хендлеров на `telegram.ext.Application`; авторизация и plugin-wiring.
- `tg/command_registry.py` — `build_command_registry(bot_app)`: список core-команд (`name`/`desc`/`handler`/`menu`).
- `tg/command_policy.py` — `OUTSIDE_TOPIC_ALLOWED_COMMANDS` (frozenset команд, допустимых вне топика).
- `tg/handlers.py` — `BotHandlers` (реализация команд), `format_session_state`, `build_lang_menu`, `TelegramRuntimePayload`.
- `tg/callbacks.py` — `CallbackHandler(CallbackActionsMixin)`: prefix-диспетчер inline-callback'ов (`orch_transition:`, `ma:`/`mode_action:`, `approve_cmd:`, `deny_cmd:`, `ask:`).
- `tg/callback_actions/__init__.py` — сборка `CallbackActionsMixin` из доменных mixin'ов.
- `tg/callback_actions/protocol.py` — `ProtocolActionsMixin`: mode-actions, аппрув/деплай команд, ask-user.
- `tg/callback_actions/session.py` — `SessionActionsMixin`: управление сессией/UI, SSH-toggle.
- `tg/callback_actions/dirs.py` — `DirsActionsMixin`: directory-picker flow.
- `tg/callback_actions/files.py` — `FileActionsMixin`: меню файлов сессии.
- `tg/callback_actions/preset.py` — `PresetActionsMixin`: запуск preset-команд.
- `tg/message_processor.py` — `MessageProcessor`: обработка входящих текстов/документов, pending-session-сообщений.
- `tg/file_upload_handler.py` — `FileUploadHandler`: pending-upload, pending-rename, flush media-group (через `bot_app.ui_state`).
- `tg/files_service_adapter.py` — Telegram-хелперы поверх `app/services/session_files_service.py` (`session_files_service`, `session_uid_for_files`, `files_rel_path`, `resolve_files_payload`).
- `tg/markdown.py` — конвертация/экранирование MarkdownV2 и Telegram-entities (`to_markdown_v2`, `escape_markdown_v2_all`, `to_telegram_entities`, `split_telegram_entities`, `utf16_length`).
- `tg/pending_input_ui.py` — `TelegramPendingInputUiAdapter` (`bot.py:223`): клавиатуры pending-input (confirm / queue-choice / queue-confirm / orchestrator-transition).

## Module API
Детальные интерфейсы модулей этой области:

- [tg/callback_actions/__init__.py](../api/tg/callback_actions/__init__-py.md)
- [tg/callback_actions/dirs.py](../api/tg/callback_actions/dirs-py.md)
- [tg/callback_actions/files.py](../api/tg/callback_actions/files-py.md)
- [tg/callback_actions/preset.py](../api/tg/callback_actions/preset-py.md)
- [tg/callback_actions/protocol.py](../api/tg/callback_actions/protocol-py.md)
- [tg/callback_actions/session.py](../api/tg/callback_actions/session-py.md)
- [tg/callbacks.py](../api/tg/callbacks-py.md)
- [tg/command_registry.py](../api/tg/command_registry-py.md)
- [tg/file_upload_handler.py](../api/tg/file_upload_handler-py.md)
- [tg/files_service_adapter.py](../api/tg/files_service_adapter-py.md)
- [tg/handlers.py](../api/tg/handlers-py.md)
- [tg/markdown.py](../api/tg/markdown-py.md)
- [tg/message_processor.py](../api/tg/message_processor-py.md)
- [tg/pending_input_ui.py](../api/tg/pending_input_ui-py.md)

## When to update
- Any commit touching `tg/**`.
- Any commit touching `agent/**` because this node has import/call dependency on it (`agent.telegram_wiring`, approvals в `tg/callback_actions/protocol.py`).
- Any commit touching `app/**` because this node has import/call dependency on it (`app/services/*`).
- Any commit touching `bot.py` because this node has import/call dependency on it (wiring/инстанцирование хендлеров).
- Any commit touching `desktop/**` because this node has import/call dependency on it.
- Any commit touching `miniapp/**` because this node has import/call dependency on it.
- Any architecture or behavior change affecting this area.

## Related nodes
- `nodes/agent.md`
- `nodes/app.md`
- `nodes/bot-py.md`
- `nodes/desktop.md`
- `nodes/miniapp.md`
- `nodes/modes.md`
- `nodes/session-py.md`
- `nodes/sessions.md`
- `agent` confidence=0.90 via L0/L1/L2
- `app` confidence=0.90 via L0/L1/L2
- `bot.py` confidence=0.90 via L0/L2
- `desktop` confidence=0.76 via L0
- `miniapp` confidence=0.76 via L0
- `modes` confidence=0.90 via L0/L1/L2
- `session.py` confidence=0.90 via L0/L2
- `sessions` confidence=0.90 via L0/L1/L2

## Owner
- project-maintainers

## Last reviewed
- 2026-06-17 (Telegram /reports command over shared report history)
- 2026-06-03T02:39:47Z
