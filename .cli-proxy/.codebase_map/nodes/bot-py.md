# Node: bot.py

Generated: 2026-06-03T02:24:29Z

## Purpose
`bot.py` — composition root (Core Layer) приложения CLI Proxy. Класс `BotApp` (`bot.py:112`) собирает все сервисы через DI-контейнер `build_application` (`app/bootstrap.py`, импорт `bot.py:74`) и держит на себе: Telegram inbound-роутинг (включая Thread Mode), авторизацию и rate-limit на входе, делегирование команд в транспортный слой `tg/*`, запуск mode-пайплайна и graceful shutdown. Функция `build_app(config)` (`bot.py:2729`) строит `telegram.ext.Application`, регистрирует хендлеры через `register_handlers` (`tg/wiring.py`) и lifecycle-хуки; `main()` (`bot.py:2748`) — процессный entrypoint (`python bot.py`).

## Scope
- Source glob: `bot.py`
- Estimated files: 1
- Не содержит бизнес-логику режимов и транспортные реализации — только их связывание (wiring). Сами реализации живут в `app/services/**`, `tg/**`, `modes/**`, `sessions/**`.

## Instructions for agent
- Перед утверждениями о runtime-поведении проверять конкретный метод в `bot.py` и ссылаться на `bot.py:<строка>` (см. зеркало интерфейсов `api/bot-py.md`).
- `BotApp.__init__` (`bot.py:113`) — порядок инициализации значим: сначала `build_application(...)`, затем атрибуты из `self.container.*`, затем SDK-сервисы и `_initialize_mode_plugins()` (`bot.py:1014`). Не переставлять зависимые шаги.
- Большинство `cmd_*` / `on_*` методов — тонкие делегаты в `self.handlers` (`tg/handlers.py`), `self.callbacks` (`tg/callbacks.py`), `self.message_processor` (`tg/message_processor.py`), `self.session_management` (`sessions/session_management.py`). Логику менять там, а здесь — только связывание.
- Режимы не должны получать прямой доступ к `BotApp`: SDK-сервисы передаются через `_initialize_mode_plugins` (`bot.py:1014`) и `AppServices` (`bot.py:228`). Новые зависимости режимов добавлять как именованные сервисы, а не как ссылку на `self`.
- Тесты монипатчат модульные символы `bot.ansi_to_html` / `bot.make_html_file` / `bot.summarize_text_with_reason` — сохранять их доступность на уровне модуля (`bot.py:90`, `bot.py:1087`).
- После правок прогонять `pytest -q` и `flake8 .`; держать изменения минимальными.

## Source of truth
- `bot.py` — единственный файл узла.
- Зеркало публичного API: `.cli-proxy/.codebase_map/api/bot-py.md`.
- Ключевые прямые зависимости (импорты `bot.py:25-74`):
  - `app/bootstrap.py` — `build_application` (DI-контейнер сервисов).
  - `config.py` — `AppConfig`, `load_config`; `app/config_runtime/loader.py` — `load_validated_settings`.
  - `session.py` — `Session`, `session_runtime_uid` (менеджер сессий — через `self.manager`).
  - `tg/wiring.py` — `register_handlers`; `tg/handlers.py`, `tg/callbacks.py`, `tg/message_processor.py`.
  - `sessions/session_management.py`, `sessions/conversation_scope.py`, `sessions/session_ui.py`.
  - `app/services/lifecycle_service.py` — `build_post_init`, `build_post_shutdown`, `build_error_handler`.
  - `app/services/telegram_transport.py`, `app/services/access_policy_service.py`, `app/services/dirs_service.py` и прочие `app/services/*`.
  - `modes/sdk` — `DialogService`, `MessagingService`, `AgentRuntimeService`, `ModeToolingService`, `DirsFlowService` и др.
  - `miniapp` — `MiniAppServer`; `agent` — `configure_pending_commands_store`, `set_approval_callback`.

## When to update
- Любой коммит, затрагивающий `bot.py`.
- Любой коммит в `agent/**`, `app/**`, `config.py`, `desktop/**`, `miniapp/**` — узел имеет import/call-зависимость от них (см. Related nodes).
- Изменение сигнатур делегатов в `tg/**` или `sessions/session_management.py`, на которые ссылаются `cmd_*` / `on_*`.
- Изменение состава SDK-сервисов в `_initialize_mode_plugins` (`bot.py:1014`) или `AppServices` (`bot.py:228`).
- Изменение lifecycle (`build_app` `bot.py:2729`, `shutdown_runtime` `bot.py:2649`, `main` `bot.py:2748`).
- Любое архитектурное или поведенческое изменение в этой области.

## Module API
Детальные интерфейсы модулей этой области:

- [bot.py](../api/bot-py.md)

## Related nodes
- `nodes/app.md` — DI-контейнер и сервисы (`build_application`, `app/services/**`), confidence=0.90 via L0/L1/L2.
- `nodes/agent.md` — pending-commands store и approval callback, confidence=0.90 via L0/L1/L2.
- `nodes/config-py.md` — `AppConfig` / `load_config`, confidence=0.90 via L2.
- `nodes/miniapp.md` — `MiniAppServer`, confidence=0.90 via L0/L1/L2.
- `nodes/modes.md` — SDK-сервисы и mode-пайплайн, confidence=0.90 via L0/L1/L2.
- `nodes/session-py.md` — `Session` / `SessionManager`, confidence=0.90 via L0/L2.
- `nodes/sessions.md` — `session_management`, `conversation_scope`, `session_ui`, confidence=0.90 via L0/L1/L2.
- `nodes/desktop.md` — синхронизация функциональности с desktop-клиентом, confidence=0.76 via L0.
- `nodes/tg.md` — транспортные хендлеры (`handlers`, `callbacks`, `message_processor`, `wiring`).

## Owner
- project-maintainers

## Last reviewed
- 2026-06-03
