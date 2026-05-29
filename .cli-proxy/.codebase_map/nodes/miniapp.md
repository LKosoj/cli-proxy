# Node: miniapp

Generated: 2026-04-27T22:43:23Z

## Purpose
`miniapp/**` is the aiohttp Telegram MiniApp surface for CLI Proxy. It mounts on the shared HTTP ingress, authenticates Telegram WebApp init data, serves the browser UI, and exposes HTTP/WebSocket APIs for config editing, files, logs, status, runs, scheduler, admin chat/admin autonomy, SSH hosts, and session remote-control settings.

## Scope
- Source glob: `miniapp/**`
- Current files: 19 under `miniapp/**` as of last review.
- Mount/server: `miniapp/server.py`
- Route/API surface: `miniapp/routes.py`, extracted route modules such as `miniapp/routes_ssh.py`
- Telegram MiniApp auth helper: `miniapp/auth.py`
- Backend services: `miniapp/services/config_service.py`, `miniapp/services/files_service.py`, `miniapp/services/logs_service.py`
- Frontend shell and client logic: `miniapp/static/index.html`, `miniapp/static/app.js`, `miniapp/static/styles.css`
- MiniApp local launcher: `start_miniapp.py`
- Targeted coverage starts at `tests/test_miniapp*.py`, `tests/smoke/test_miniapp_server_smoke.py`, `tests/test_miniapp_app_js.py`, `tests/test_miniapp_config_tab_js.py`, `tests/test_logs_config_local_only.py`

## Instructions for agent
- Start with `.cli-proxy/.codebase_map/INDEX.md`, then this node, then the task-specific files under `miniapp/**`.
- Before claiming MiniApp runtime behavior, verify the exact method/function in source and cite concrete `path:line`.
- For route/API behavior, inspect `miniapp/routes.py`; `MiniAppRoutes.register()` is the route table.
- For config editor changes, keep `config.yaml`, `config_example.yaml`, `config.py`, `app/config_runtime/**`, `miniapp/services/config_service.py`, `miniapp/static/app.js`, `desktop/widgets/config_editor.py`, `README.md`, and `README_EN.MD` synchronized.
- For MiniApp UI changes, use the `playwright-cli` skill as required by repository instructions, and run the closest JS/Python targeted tests for the changed surface.
- Do not kill the process on port `8088`.

## Source of truth
- `miniapp/server.py` - shared-ingress mounting, base-path normalization, runtime enable guard, request body limit.
- `miniapp/routes.py` - MiniApp auth gate, route registration, WebSocket ticketing, status/log streams, files/config/runs/scheduler/admin/session APIs, static file serving.
- `miniapp/routes_ssh.py` - SSH host CRUD, connection test, key generation, and secret routes.
- `miniapp/auth.py` - Telegram WebApp initData signature validation helper used by the security facade.
- `miniapp/services/config_service.py` - MiniApp config schema/view/validate/diff/save logic, runtime reload/restart-required/secret metadata, and backend secret redaction sentinel handling.
- `miniapp/services/files_service.py` - session-scoped local file operations, path validation, binary/size checks, revision conflict handling.
- `miniapp/services/logs_service.py` - log type resolution, session filters, log parsing, and access checks.
- `miniapp/static/index.html` - tab/layout DOM for Config, Files, Logs, Status, Scheduler, Settings, Admin, and editor views.
- `miniapp/static/app.js` - Telegram WebApp client bootstrap, API calls, state, rendering, polling, WebSocket handling, UI actions, and config-editor secret sentinel/password/clear handling.
- `miniapp/static/styles.css` - MiniApp styling, including config-editor secret input row layout.
- `start_miniapp.py` - standalone local startup helper for MiniApp on shared ingress.
- `tests/test_miniapp*.py`, `tests/smoke/test_miniapp_server_smoke.py` - primary MiniApp route/service/UI regression coverage.

## When to update
- Any change under `miniapp/**` or `start_miniapp.py`.
- Any MiniApp-visible config contract change in `config.py`, `config.yaml`, `config_example.yaml`, `app/config_runtime/**`, `app/services/app_runtime_service.py`, `desktop/widgets/config_editor.py`, `miniapp/services/config_service.py`, or `miniapp/static/app.js`.
- Any change in `app/security/**`, `app/services/shared_http_ingress.py`, or `app/services/actor_identity.py` that changes MiniApp auth, authorization, rate limiting, actor IDs, or ingress mounting.
- Any change in `app/services/scheduler_service.py`, `app/services/remote_control_service.py`, `app/services/run_artifact_store.py`, `app/services/runtime_progress_service.py`, `app/services/ssh_config_loader.py`, or `app/services/remote_shell_service.py` that changes contracts consumed by `miniapp/routes.py`.
- Any change in `modes/**`, `sessions/**`, or `session.py` that changes mode launch, callbacks, session IDs, active mode/status state, run artifacts, or remote-control state exposed by MiniApp.
- Any Desktop/Bot parity change affecting config, files, logs, scheduler, admin, runs, SSH, or remote-control behavior.
- Any targeted MiniApp test or smoke-test change under `tests/test_miniapp*.py`, `tests/test_logs_config_local_only.py`, or `tests/smoke/test_miniapp_server_smoke.py`.

## Related nodes
- `nodes/app.md` - shared ingress, security facade, actor identity, runtime config reload, scheduler, SSH/remote-control, run artifacts, progress/status services used by MiniApp.
- `nodes/config-py.md` - legacy config dataclasses and MiniApp config fields.
- `nodes/config-example-yaml.md` - sample MiniApp config keys that must stay synchronized with runtime config and UI schema.
- `nodes/desktop.md` - Desktop parity for config editor, files, logs, scheduler, admin, runs, SSH, and remote-control flows.
- `nodes/modes.md` - mode launch/status/callback contracts rendered or invoked by MiniApp.
- `nodes/session-py.md` - `Session`, `SessionManager`, and `session_runtime_uid` contracts used by MiniApp session selection and access checks.
- `nodes/sessions.md` - active mode, orchestrator, SSH/remote-control, and session status accessors consumed by MiniApp routes.
- `nodes/tests.md` - targeted MiniApp route, service, JS, UI, and smoke coverage.

## Last reviewed
- 2026-05-15
