# Project Profile

Project kind: `existing_codebase`

## Code Map Context

Codebase map generated at: 2026-06-17T10:39:27Z

Codebase map commit: b25f9ae22e65d869c9132eff0b578aebcd5e3fb3

[ARCHITECTURE.md]
# ARCHITECTURE

Generated: 2026-06-17T10:39:27Z

## Top-level areas
- `tests`: 549 files
- `modes`: 219 files
- `app`: 154 files
- `agent`: 60 files
- `desktop`: 31 files
- `miniapp`: 22 files
- `tg`: 17 files
- `sessions`: 10 files
- `utils`: 8 files
- `i18n`: 5 files
- `locales`: 4 files
- `scripts`: 2 files

[STACK.md]
# Tech Stack

Обновлено по полному `rg --files` и выборочному чтению ключевых файлов фокуса `tech`.

## Runtime
- **Python 3.12** — CI matrix: `.github/workflows/ci.yml`; зависимости: `requirements.txt`; запуск: `bot.py`, `desktop/main.py`, `start_miniapp.py`.
- **asyncio** — основной async runtime для бота, MiniApp, scheduler, mode pipeline и desktop facade: `bot.py`, `desktop/services/application_facade.py`.
- **aiohttp** — общий HTTP listener и MiniApp/webhook surfaces: `app/services/shared_http_ingress.py`, `miniapp/server.py`, `app/services/webhook_ingress_service.py`.
- **python-telegram-bot==22.6** — Telegram polling, команды, callbacks, WebApp-кнопка: `bot.py`, `tg/wiring.py`, `tg/command_registry.py`.

## UI
- **Desktop: PySide6==6.8.2 + qasync==0.27.1** — Qt UI и asyncio loop: `desktop/main.py`, `desktop/main_window.py`, `desktop/widgets/*`.
- **MiniApp: static HTML/CSS/JS + aiohttp** — отдельного JS build stack нет: `miniapp/package.json` пустой; UI: `miniapp/static/index.html`, `miniapp/static/app.js`, `miniapp/static/styles.css`.
- **Browser-side assets** — Telegram WebApp SDK и Ace Editor подключаются через CDN
...[truncated]

[INTEGRATIONS.md]
# Integrations

Обновлено по полному `rg --files` и выборочному чтению ключевых файлов фокуса `tech`.

## Telegram
- **Bot API / polling** — `python-telegram-bot==22.6`; bootstrap and handlers: `bot.py`, `tg/wiring.py`.
- **Core commands** — registry in `tg/command_registry.py`; includes session control, git/files, MiniApp, limits, modes and lint evolution admin commands.
- **Callbacks** — centralized callback router and action modules: `tg/callbacks.py`, `tg/callback_actions/*`.
- **Thread mode** — `chat:*` / `thread:*` scopes and forum-topic routing: `sessions/conversation_scope.py`, `app/services/session_thread_manager.py`, `tg/command_policy.py`.
- **Outbound formatting** — Markdown/entities transport and chunking: `tg/markdown.py`, `app/services/telegram_transport.py`.

## MiniApp HTTP Surface
- **Shared ingress** — one aiohttp listener for MiniApp and webhooks: `app/services/shared_http_ingress.py`.
- **MiniApp mount** — base path from `miniapp.base_path`, default `/cli-proxy`: `miniapp/server.py`, `config.py`.
- **Auth** — Telegram WebApp `initData` HMAC verification through `SecurityFacade`: `miniapp/auth.py`, `minia
...[truncated]

[CONVENTIONS.md]
# CONVENTIONS

Generated: 2026-06-17T10:39:27Z

- `flake8`: unknown
- `pytest` layout (`tests/`): yes

[TESTING.md]
# TESTING

Generated: 2026-06-17T10:39:27Z

- test files under `tests/`: 549

## Test areas
- `tests/smoke`: 8
- `tests/conftest.py`: 1
- `tests/test_admin_allowlist.py`: 1
- `tests/test_admin_analyzer.py`: 1
- `tests/test_admin_autonomy_e2e.py`: 1
- `tests/test_admin_autonomy_loop.py`: 1
- `tests/test_admin_autonomy_policy.py`: 1
- `tests/test_admin_autopilot_telegram_format.py`: 1

## Representative tests
- `tests/test_admin_monitor.py`
- `tests/test_input_dispatch_pending_queue.py`
- `tests/test_memory_store.py`
- `tests/test_ci_workflow.py`
- `tests/test_mode_registry_loader.py`
- `tests/test_utils_ui.py`
- `tests/test_summary_commit_message_language.py`
- `tests/test_admin_plugin_tools.py`

[CONCERNS.md]
# CONCERNS

Generated: 2026-06-17T10:39:27Z

## Potential concerns
- unfinished/HACK/XXX markers: 49
- Auto-generated map; verify critical assumptions manually before refactors.

## Largest top-level areas
- `tests`: 549 files
- `modes`: 219 files
- `app`: 154 files
- `agent`: 60 files
- `desktop`: 31 files
- `miniapp`: 22 files
- `tg`: 17 files
- `sessions`: 10 files

[STRUCTURE.md]
# STRUCTURE

Generated: 2026-06-17T10:39:27Z

- total indexed files: 1095

## Top-level areas
- `tests`: 549
- `modes`: 219
- `app`: 154
- `agent`: 60
- `desktop`: 31
- `miniapp`: 22
- `tg`: 17
- `sessions`: 10
- `utils`: 8
- `i18n`: 5
- `locales`: 4
- `scripts`: 2

## Representative paths
- `locales/zh.json`
- `locales/en.json`
- `locales/de.json`
- `locales/ru.json`
- `i18n/__init__.py`
- `i18n/translator.py`
- `i18n/plural.py`
- `i18n/resolver.py`
- `i18n/language_names.py`
- `bot.py`
- `summary.py`
- `modes/sdk/context.py`
- `modes/sdk/orchestrator_deps.py`
- `tg/callback_actions/preset.py`
- `tg/callback_actions/dirs.py`
- `tg/callback_actions/protocol.py`
- `tg/callback_actions/__init__.py`
- `tg/callback_actions/session.py`
- `tg/callback_actions/files.py`
- `tg/markdown.py`

## Selected Packs

- `core-baseline` score `1.0`
- `architecture` score `1.0`
- `asyncapi` score `1.0`
- `openapi` score `1.0`
- `ops` score `1.0`
- `python` score `1.0`
- `ui` score `1.0`

## Open Questions

- Confirm whether inferred packs match the project intent.
