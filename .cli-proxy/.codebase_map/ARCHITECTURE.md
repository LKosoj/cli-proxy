# Архитектура cli-proxy

Обновлено: 2026-04-28. Основа: полный `rg --files` и выборочное чтение архитектурных entrypoint-файлов.

## Назначение

`cli-proxy` — Python-приложение для управления CLI-агентами через Telegram Bot, MiniApp и Desktop UI. Общий runtime строится вокруг сессий, mode-плагинов, CLI-runner'ов, shared services и единой конфигурации.

## Слои

- Entrypoints:
  - `bot.py` — основной Telegram runtime: `BotApp`, сборка сервисов, запуск Telegram handlers, MiniApp, scheduler, session/runtime services.
  - `desktop/main.py` — PySide6/qasync bootstrap Desktop UI через `ApplicationFacade`.
  - `start_miniapp.py` — отдельный запуск MiniApp.
  - `gen_init_data.py`, `parse_status.py`, `code_stats.py`, `summary.py` — служебные сценарии.

- Application container и shared services:
  - `app/bootstrap.py` — deterministic сборка `ApplicationContainer`: state repository, `SessionManager`, mode registry/loader, mode dependencies, task/session control, webhook/scheduler/SSH services.
  - `app/mode_dependencies.py` — dependency bundle для mode-плагинов и foundation services.
  - `app/services/*` — общий runtime: input dispatch, session/run operations, orchestration, project registry, scheduler, SSH/remote control, logging, metrics, config service, skill/runtime support.
  - `app/events/bus.py` — typed system events для Telegram, MiniApp, Desktop, scheduler, security и runtime reload.

- Конфигурация и безопасность:
  - `config.py` — dataclass-модель runtime config и сохранение `config.yaml`.
  - `app/config_runtime/*` — pydantic-валидация YAML, `.env` и `CLI_PROXY__*` overrides.
  - `app/security/*` — `SecurityFacade`, auth strategies, validators, rate limits, audit/errors.
  - `config_example.yaml` должен оставаться синхронизированным с новыми config keys.

- Сессии и CLI execution:
  - `session.py` — core `Session`, `SessionManager`, CLI selection/execution helpers, process/runtime state.
  - `sessions/conversation_scope.py` — chat/thread/desktop scope и `session_uid`.
  - `sessions/session_management.py` — Telegram-facing orchestration для запуска, остановки, interrupt и output.
  - `sessions/session_run_service.py`, `sessions/session_output_service.py`, `sessions/session_state_access.py`, `sessions/session_status.py`, `sessions/session_ui.py` — run/output/state/status/UI helpers.

- Mode system:
  - `modes/registry.py` — discovery и загрузка mode-плагинов из `modes/*/__init__.py` через `PLUGIN`.
  - `modes/sdk/base.py` — `BaseMode`, lifecycle, menu/callback helpers, доступ к SDK-сервисам без прямой привязки к `BotApp`.
  - `modes/sdk/services/*` — messaging, dialogs, tasks, session control, runtime, tooling, registry/status.
  - `modes/sdk/runtime/*` — executor/dispatcher/planner, contracts, reactions, validation adapters, MCP tooling, memory/evidence/QC utilities.
  - Реализации mode: `modes/agent`, `modes/analyst`, `modes/manager`, `modes/webmaster`, `modes/admin`, `modes/codebase_mapper`.
  - Правила разработки mode зафиксированы в `modes/DEVELOPMENT.md`.

- Agent tooling:
  - `agent/cli_routing.py` — выбор CLI-кандидатов и routed prompt execution.
  - `agent/manager_core.py` — orchestration ядро manager-потока.
  - `agent/plugins/*` — tool plugins, включая файловые, web/search, CLI, memory, admin и media-интеграции.
  - `agent/telegram_wiring.py` — установка plugin handlers в Telegram application.

- Telegram transport:
  - `tg/wiring.py` — регистрация command/message/callback handlers.
  - `tg/handlers.py` — команды, меню, runtime payloads и Telegram UI actions.
  - `tg/callbacks.py`, `tg/callback_actions/*` — callback dispatch и action mixins.
  - `tg/message_processor.py` — обработка входящих сообщений.
  - `tg/markdown.py` — единый Telegram Markdown/entities pipeline.

- MiniApp:
  - `miniapp/server.py` — монтирование aiohttp subapp на `SharedHttpIngress`.
  - `miniapp/routes.py` — HTTP/WebSocket API MiniApp, access checks, sessions, runs, files, logs, admin, SSH/remote control endpoints.
  - `miniapp/auth.py` — Telegram initData verification.
  - `miniapp/services/config_service.py`, `files_service.py`, `logs_service.py` — backend-сервисы MiniApp.
  - `miniapp/static/*` — frontend MiniApp.

- Desktop:
  - `desktop/services/application_facade.py` — основной Desktop orchestration facade поверх shared `app/services`, `modes`, `sessions`, `agent`.
  - `desktop/main_window.py` — композиция PySide6 widgets.
  - `desktop/widgets/*` — UI-панели для сессий, чата, режимов, git, config, runs, scheduler, admin.
  - `desktop/services/desktop_git_service.py`, `desktop_state_service.py`, `desktop_identity_provider.py`, `theme_service.py` — Desktop-specific services.

- Utilities and tests:
  - `utils/*` — text/path/UI/html/CLI helpers.
  - `tests/*` — unit, integration и smoke coverage для runtime, modes, MiniApp, Desktop, security, sessions, services.

## Runtime flow

1. `bot.py` загружает `AppConfig`, создает `BotApp` и вызывает `build_application(...)` из `app/bootstrap.py`.
2. `ApplicationContainer` создает state/session/mode/tooling/services foundation.
3. `BotApp` подключает Telegram transport (`tg/*`), session services (`sessions/*`), mode SDK/runtime (`modes/*`), MiniApp (`miniapp/*`) и shared HTTP ingress.
4. Входящий Telegram/MiniApp/Desktop action нормализуется в transport-specific слой, затем идет в shared services, session state и активный mode/runtime.
5. Mode-плагины работают через `ModeDependencies` и SDK-сервисы; прямой доступ mode-кода к `BotApp` считается исключением.

## Ограничения обзора

- Скан был файловым и выборочным по ключевым архитектурным файлам; runtime запуск и тесты не выполнялись.
- `.cli-proxy/.codebase_map/api/*` содержит не все текущие source-файлы: фактический `rg --files` шире текущего API-зеркала.
