# Node: tg

Generated: 2026-06-17T10:46:18Z

## Purpose
Instruction node for `tg` area.

## Scope
- Source glob: `tg/**`
- Estimated files: 19
- Current files: 19 under `tg/**` as of last review.

## Instructions for agent
- Read only files relevant to the active task.
- Prefer deterministic checks before edits.
- Keep changes minimal and validate with tests/linters where applicable.

## Source of truth
- `tg/**`
- `tg/__init__.py`
- `tg/callback_actions/__init__.py`
- `tg/callback_actions/dirs.py`
- `tg/callback_actions/files.py`
- `tg/callback_actions/preset.py`
- `tg/callback_actions/protocol.py`
- `tg/callback_actions/session.py`
- `tg/callbacks.py`
- `tg/command_policy.py`
- `tg/command_registry.py`
- `tg/handlers.py`
- `tg/rich_message.py`

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

## Behavior notes
- `tg/rich_message.py`: входящие Rich Messages (Bot API 10.1+, поле `Message.rich_message`). python-telegram-bot 22.x знает Bot API ≤10.0, кладёт поле в `message.api_kwargs`, `text` пуст, `filters.TEXT` не срабатывает — раньше такие сообщения терялись молча. Фильтр `RICH_MESSAGE` (rich_message есть, text пуст) зарегистрирован в `tg/wiring.py` сразу после TEXT-хендлера и ведёт в `on_message`; `process_message` при `text is None` берёт `rich_message_text()` — блоки развёрнуты в плоский текст (pre → ```lang, list → label + текст, blockquote → «> », медиа → только подпись, url → «текст (адрес)»). Пустой развёрнутый текст (одни медиа без подписи) отбрасывается с INFO-логом; текст, начинающийся с «/», уходит в `on_unknown_command`, потому что у rich-сообщений нет entities и `filters.COMMAND` их не видит. Последним в группе 0 стоит `MessageHandler(UpdateType.MESSAGE & ~StatusUpdate.ALL → bot_app.on_unsupported_message)`: голосовые, стикеры, видео и неизвестные типы оставляют INFO `inbound unsupported message … content=[…] api_kwargs=[…]`, ответа пользователю нет; правки сообщений и служебные события не логируются. Тесты: `tests/test_rich_message.py`.
- `tg/message_processor.py::_log_inbound` пишет INFO-строку `inbound message|photo|document chat_id=… thread_id=… user_id=… message_id=… text_len=…` (без текста: он логируется уже после авторизации в `[run_prompt] acquiring run_lock`) на входе в `process_message`/`process_photo`/`process_document`; отказ авторизации и тихий выход «session not resolved» тоже логируются. `tg/callbacks.py::handle_callback` логирует `inbound callback … data=…`. Это единственный след входящего апдейта до запуска CLI — по нему отличают «сообщение не дошло до бота» от «бот проглотил».

## When to update
- Any commit touching `tg/**`.
- Any commit touching `agent/**` because this node has import/call dependency on it.
- Any commit touching `app/**` because this node has import/call dependency on it.
- Any commit touching `bot.py` because this node has import/call dependency on it.
- Any commit touching `config.py` because this node has import/call dependency on it.
- Any commit touching `config_example.yaml` because this node has import/call dependency on it.
- Any architecture or behavior change affecting this area.

## Related nodes
- `nodes/agent.md`
- `nodes/app.md`
- `nodes/bot-py.md`
- `nodes/config-py.md`
- `nodes/config-example-yaml.md`
- `nodes/desktop.md`
- `nodes/i18n.md`
- `nodes/locales.md`
- `agent` confidence=0.95 via L0/L1/L2
- `app` confidence=0.95 via L0/L1/L2
- `bot.py` confidence=0.95 via L0/L2
- `config.py` confidence=0.66 via L0
- `config_example.yaml` confidence=0.66 via L0
- `desktop` confidence=0.95 via L0
- `i18n` confidence=0.90 via L0/L1/L2
- `locales` confidence=0.95 via L0

## Owner
- project-maintainers

## Last reviewed
- 2026-09-22T13:00:00Z
