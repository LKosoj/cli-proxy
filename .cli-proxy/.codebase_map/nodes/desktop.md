# Node: desktop

Generated: 2026-06-03T02:24:29Z

## Purpose
Транспортный слой: десктоп-клиент с GUI на PySide6/Qt для управления CLI-агентами, сессиями и режимами. Бизнес-логики не содержит — работает через фасад `desktop/services/application_facade.py` (`ApplicationFacade`) и SDK-сервисы. Композиционный корень и точка входа — `desktop/main.py` (`bootstrap_facade`, `main`); главное окно — `desktop/main_window.py` (`MainWindow`).

## Scope
- Source glob: `desktop/**`
- Current files: 32 under `desktop/**` as of last review.
- Подкаталоги: `desktop/services/` (фасады и сервисы UI), `desktop/widgets/` (Qt-виджеты панелей и диалогов).

## Instructions for agent
- Это транспортный слой: бизнес-логику не размещать здесь, маршрутизировать через `ApplicationFacade` и SDK-сервисы (см. `modes/sdk/`).
- Соблюдать порядок bootstrap из `desktop/main.py:35` (`bootstrap_facade`): Registry → Services → Facade → UI state service.
- Запуск режимов из десктопа — fail-closed: актор должен резолвиться в numeric Telegram `chat_id` через `desktop/services/desktop_identity_provider.py`; иначе deny с reason `actor_unresolved`. Политика описана в `desktop/README.md` и реализована в `desktop/services/application_facade.py`.
- Синхронизировать функциональность с ботом (`tg/`) и MiniApp (`miniapp/`) — общий контракт сервисов (требование `CLAUDE.md`).
- Qt-виджеты — в `desktop/widgets/`; сервисы и фасады — в `desktop/services/`.
- Читать только файлы, относящиеся к задаче; изменения держать минимальными и проверять `pytest -q`.

## Source of truth
- `desktop/README.md` — Desktop Mode Launch Policy (fail-closed, allowlist).
- `desktop/main.py` — bootstrap и точка входа (`bootstrap_facade`, `main`).
- `desktop/main_window.py` — `MainWindow`, навигация и сборка виджетов.
- `desktop/services/__init__.py` — экспорт фасада и сервисов слоя.
- `desktop/services/application_facade.py` — `ApplicationFacade`, вычисление `launch_policy`.
- `desktop/services/desktop_admin_facade.py` — админ-операции UI.
- `desktop/services/desktop_git_service.py` — `DesktopGitService`.
- `desktop/services/desktop_identity_provider.py` — резолв owning actor.
- `desktop/services/desktop_state_service.py` — `DesktopUiStateService`, состояние UI.
- `desktop/services/theme_service.py` — `ThemeService`.
- `desktop/services/pending_input_ui.py` — UI для отложенного ввода.
- `desktop/widgets/**` — Qt-виджеты (chat_view, files_panel, git_panel, mode_panel, admin_panel, scheduler_panel, command_palette и др.).

## Module API
Детальные интерфейсы модулей этой области:

- [desktop/main.py](../api/desktop/main-py.md)
- [desktop/widgets/admin_chat_section.py](../api/desktop/widgets/admin_chat_section-py.md)
- [desktop/main_window.py](../api/desktop/main_window-py.md)
- [desktop/services/application_facade.py](../api/desktop/services/application_facade-py.md)
- [desktop/services/desktop_admin_facade.py](../api/desktop/services/desktop_admin_facade-py.md)
- [desktop/services/desktop_git_service.py](../api/desktop/services/desktop_git_service-py.md)
- [desktop/services/desktop_identity_provider.py](../api/desktop/services/desktop_identity_provider-py.md)
- [desktop/services/desktop_state_service.py](../api/desktop/services/desktop_state_service-py.md)
- [desktop/services/pending_input_ui.py](../api/desktop/services/pending_input_ui-py.md)
- [desktop/services/theme_service.py](../api/desktop/services/theme_service-py.md)
- [desktop/widgets/admin_panel.py](../api/desktop/widgets/admin_panel-py.md)
- [desktop/widgets/chat_view.py](../api/desktop/widgets/chat_view-py.md)
- [desktop/widgets/command_palette.py](../api/desktop/widgets/command_palette-py.md)
- [desktop/widgets/config_editor.py](../api/desktop/widgets/config_editor-py.md)
- [desktop/widgets/files_panel.py](../api/desktop/widgets/files_panel-py.md)

## When to update
- Any commit touching `desktop/**`.
- Any commit touching `agent/**` because this node has import/call dependency on it.
- Any commit touching `app/**` because this node has import/call dependency on it.
- Any commit touching `bot.py` because this node has import/call dependency on it.
- Any commit touching `config.py` because this node has import/call dependency on it.
- Any commit touching `miniapp/**` because this node has import/call dependency on it.
- Any architecture or behavior change affecting this area.

## Related nodes
- `nodes/agent.md`
- `nodes/app.md`
- `nodes/bot-py.md`
- `nodes/config-py.md`
- `nodes/miniapp.md`
- `nodes/modes.md`
- `nodes/session-py.md`
- `nodes/sessions.md`
- `agent` confidence=0.90 via L0/L1/L2
- `app` confidence=0.90 via L0/L1/L2
- `bot.py` confidence=0.76 via L0
- `config.py` confidence=0.90 via L2
- `miniapp` confidence=0.76 via L0
- `modes` confidence=0.90 via L0/L1/L2
- `session.py` confidence=0.90 via L0/L2
- `sessions` confidence=0.90 via L0/L1/L2

## Owner
- project-maintainers

## Last reviewed
- 2026-06-04T00:00:00Z
