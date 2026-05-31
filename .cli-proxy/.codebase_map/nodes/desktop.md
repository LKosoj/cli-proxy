# Node: desktop

Generated: 2026-04-27T22:43:23Z

## Purpose
`desktop/**` is the PySide6/qasync Desktop UI surface for CLI Proxy. It bootstraps shared runtime services, composes Qt widgets, persists Desktop UI state, and routes Desktop session input, mode menus/callbacks, Git, config, run operations, scheduler/admin, SSH/remote-control, and plugin actions through `ApplicationFacade`.

## Scope
- Source glob: `desktop/**`
- Current files: 32 under `desktop/**` as of last review.
- Entry point: `desktop/main.py`
- Main shell: `desktop/main_window.py`
- Desktop orchestration and services: `desktop/services/application_facade.py`, `desktop/services/desktop_state_service.py`, `desktop/services/desktop_identity_provider.py`, `desktop/services/desktop_git_service.py`, `desktop/services/theme_service.py`
- Desktop UI widgets: `desktop/widgets/*.py`
- Desktop launch policy notes: `desktop/README.md`
- Targeted Desktop coverage starts at `tests/test_desktop_*.py`, `tests/test_git_panel.py`, `tests/test_report_viewer.py`, `tests/test_task_progress_widget.py`, `tests/test_task_queue_widget.py`, `tests/test_config_editor.py`, `tests/smoke/test_desktop_entrypoint_smoke.py`

## Instructions for agent
- Start with `.cli-proxy/.codebase_map/INDEX.md`, then this node, then task-specific files under `desktop/**`.
- Before claiming Desktop runtime behavior, verify the exact method/function in source and cite concrete `path:line`.
- Keep UI logic in `desktop/widgets/*`; keep Desktop orchestration in `desktop/services/application_facade.py`; reusable business logic belongs in `app/services`, `sessions`, `modes`, or `agent`.
- Preserve Desktop mode launch policy documented in `desktop/README.md`: actor/project/session ownership is resolved in `desktop/services/desktop_identity_provider.py`, Desktop launch policy is computed in `ApplicationFacade._resolve_desktop_mode_launch_policy`, and authorization flows through `app/services/mode_launch_adapter.py` and `app/security/**`.
- For mode-facing Desktop changes, read `modes/DEVELOPMENT.md` and use mode SDK services instead of coupling shared mode logic directly to Desktop widgets or a `BotApp` instance.
- For config-facing changes, keep `config.yaml`, `config_example.yaml`, `config.py`, `desktop/widgets/config_editor.py`, `miniapp/services/config_service.py`, and `miniapp/static/app.js` synchronized when their fields or semantics change.
- Validate changes with targeted `.venv/bin/pytest -q ...` files for the touched Desktop service/widget path; run the full suite only for shared runtime changes or explicit release/smoke requests.

## Source of truth
- `desktop/README.md` - Desktop mode launch policy, allowlist behavior, actor resolution, and security notes.
- `desktop/main.py` - PySide6/qasync bootstrap, service construction order, and `MainWindow` startup.
- `desktop/main_window.py` - main Qt window, navigation, session/chat/context panels, notifications, and widget composition.
- `desktop/services/application_facade.py` - Desktop runtime facade over shared app services, mode SDK, sessions, tasks, agent command approvals, scheduler/admin/run/plugin flows, and UI notifications.
- `desktop/services/desktop_state_service.py` - persisted Desktop UI state and startup readiness handling.
- `desktop/services/desktop_identity_provider.py` - Desktop owner/project/session resolution and notification target ownership.
- `desktop/services/desktop_git_service.py` - local/remote Git operations for session workdirs.
- `desktop/services/theme_service.py` - Qt theme selection, custom themes, and main stylesheet generation.
- `desktop/widgets/chat_view.py`, `desktop/widgets/session_manager.py`, `desktop/widgets/session_settings.py`, `desktop/widgets/mode_panel.py`, `desktop/widgets/mode_menu.py`, `desktop/widgets/task_progress.py`, `desktop/widgets/task_queue.py`, `desktop/widgets/git_panel.py`, `desktop/widgets/config_editor.py`, `desktop/widgets/report_viewer.py`, `desktop/widgets/run_operations_panel.py`, `desktop/widgets/plugin_menu.py`, `desktop/widgets/admin_panel.py`, `desktop/widgets/admin_chat_section.py`, `desktop/widgets/scheduler_panel.py`, `desktop/widgets/command_palette.py`, `desktop/widgets/log_viewer.py`, `desktop/widgets/remote_mode_banner.py`, `desktop/widgets/manage_tasks_progress.py` - Desktop widget behavior.

## When to update
- Any change under `desktop/**` or `desktop/README.md`.
- Any change in `app/services/**`, `app/events/**`, `app/security/**`, or `app/mode_dependencies.py` that changes contracts used by `desktop/services/application_facade.py` or Desktop widgets.
- Any change in `modes/**`, especially `modes/DEVELOPMENT.md`, `modes/registry.py`, `modes/sdk/**`, mode callbacks, mode menus, dialogs, runtime services, or plugin UI contracts used by Desktop.
- Any change in `session.py` or `sessions/**` that changes Desktop session IDs, `session_runtime_uid`, conversation scope, active mode state, queue state, or session persistence.
- Any change in `agent/**` that changes command approval, pending command, or shell execution contracts consumed by Desktop.
- Any config contract change in `config.py`, `config.yaml`, `config_example.yaml`, `app/config_runtime/**`, `desktop/widgets/config_editor.py`, `miniapp/services/config_service.py`, or `miniapp/static/app.js`.
- Any MiniApp/Bot parity change that affects Desktop-visible files/config/logs/remote-control/scheduler/run behavior.
- Any targeted Desktop test coverage change under `tests/test_desktop_*.py`, Desktop widget tests, or Desktop smoke tests.

## Related nodes
- `nodes/app.md` - shared services, events, security, config runtime, scheduler, SSH/remote-control, run artifacts, and mode dependencies used by Desktop.
- `nodes/modes.md` - mode registry, SDK, callbacks, dialogs, menus, plugin UI, and runtime contracts rendered or invoked by Desktop.
- `nodes/session-py.md` - `SessionManager`, `Session`, `session_runtime_uid`, and session persistence used by Desktop.
- `nodes/sessions.md` - conversation scope and session state access used by Desktop widgets and facade.
- `nodes/agent.md` - pending command approval and shell execution contracts used by Desktop facade.
- `nodes/config-py.md` - legacy config dataclasses and fields surfaced in Desktop config editor/runtime.
- `nodes/config-example-yaml.md` - sample config that must track Desktop-visible config fields.
- `nodes/miniapp.md` - config/files/logs/remote-control/scheduler/run parity with Desktop UI flows.
- `nodes/tests.md` - targeted Desktop, widget, integration, and smoke coverage.

## Last reviewed
- 2026-05-31
