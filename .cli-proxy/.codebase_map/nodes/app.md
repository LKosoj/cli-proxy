# Node: app

Generated: 2026-04-27T22:43:22Z

## Purpose
`app/**` is the shared application layer for CLI Proxy runtime. It builds the application container, validates and adapts runtime config, publishes system events, centralizes security, and exposes services used by the bot, desktop UI, MiniApp, sessions, and modes.

## Scope
- Source glob: `app/**`
- Current files: 149 under `app/**` as of last review.
- Top-level modules: `app/bootstrap.py`, `app/mode_dependencies.py`, `app/config_runtime/**`, `app/events/**`, `app/security/**`, `app/services/**`
- Service subareas include runtime config reload, session/task orchestration, Telegram/UI state transport, run artifacts/observability/doctor/recovery, scheduler/webhook ingress, SSH/remote control, skill runtime/registry, `app/services/lint_evolution/**`, and `app/services/session_transfer/**`.

## Instructions for agent
- Start with `.cli-proxy/.codebase_map/INDEX.md`, then this node, then task-specific files under `app/**`.
- Before claiming runtime behavior, verify the exact method/function in source and cite concrete `path:line`.
- Keep `app/services/__init__.py` lazy; do not add eager service imports there because `config.py` imports `app.services.dotenv_loader`.
- Treat `app/config_runtime/models.py`, `app/config_runtime/adapter.py`, `app/config_runtime/serialization.py`, `config.py`, `config.yaml`, and `config_example.yaml` as one config contract when fields or semantics change.
- For mode integration changes, read `modes/DEVELOPMENT.md` and use `app/mode_dependencies.py` plus `modes/sdk/**` services instead of coupling shared mode logic to `BotApp`.
- For Telegram output changes, keep the shared transport/markdown path through `tg/markdown.py` and `app/services/telegram_transport.py`.

## Source of truth
- `app/bootstrap.py` - `ApplicationContainer` and deterministic construction of core runtime dependencies.
- `app/mode_dependencies.py` - mode-facing dependency dataclasses and foundation services.
- `app/config_runtime/models.py`, `app/config_runtime/loader.py`, `app/config_runtime/adapter.py`, `app/config_runtime/serialization.py` - validated config schema, env overlay, legacy config adapter, and serialization.
- `app/events/bus.py` - system event dataclasses and `SystemEventBus`.
- `app/security/__init__.py`, `app/security/facade.py`, `app/security/auth.py`, `app/security/validators.py`, `app/security/rate_limits.py`, `app/security/audit.py`, `app/security/errors.py` - security public API and implementation.
- `app/services/__init__.py` - lazy exports for desktop-facing `ConfigService`, `SessionService`, `TaskService`, and `ThemeService`.
- `app/services/*.py` - shared application services imported by `bot.py`, `desktop/**`, `miniapp/**`, `sessions/**`, `session.py`, and `modes/**`, including run artifact lifecycle facades.
- `app/services/mode_run_lifecycle_service.py` - mode run artifact lifecycle facade; boundary validation failures are logged and returned as error reports.
- `app/services/lint_evolution/**` - lint evolution runtime, rules, schema, and reports.
- `app/services/session_transfer/**` - session transfer canonical model, readers, writers, and service.

## When to update
- Any change under `app/**`.
- Any config schema, default, serialization, or runtime reload change in `config.py`, `config.yaml`, `config_example.yaml`, or `miniapp/services/config_service.py`.
- Any change in `bot.py`, `desktop/**`, `miniapp/**`, `sessions/**`, `session.py`, or `modes/**` that changes how app services, events, security, mode dependencies, run artifacts, or runtime config are called.
- Any change to `tg/markdown.py` or Telegram transport behavior that affects `app/services/telegram_transport.py`.
- Any architecture or behavior change that moves responsibility into or out of `app/**`.

## Related nodes
- `nodes/bot-py.md` - imports `app.bootstrap`, app services, `app.events.bus`, and `app.security`.
- `nodes/config-py.md` - legacy config dataclasses adapted by `app/config_runtime/adapter.py`.
- `nodes/config-example-yaml.md` - sample config that must track validated config fields.
- `nodes/desktop.md` - imports app-level config, session, task, theme, path, SSH, and run services.
- `nodes/miniapp.md` - uses app security, events, runtime config, scheduler, SSH, remote control, and run services.
- `nodes/modes.md` - uses `app.mode_dependencies` and app run artifact/progress/project-prompt services.
- `nodes/sessions.md` - uses app orchestration, logging, runtime progress, task hook, and session state services.
- `nodes/session-py.md` - imports CLI monitors, tool availability, SSH skill generation, state repository, and session tick services from `app/services`.
- `nodes/tg.md` - shares Telegram formatting/transport expectations with app-level Telegram transport.
- `nodes/tests.md` - contains targeted coverage for app services, config runtime, events, security, MiniApp/Desktop integration, and mode launch paths.

## Last reviewed
- 2026-05-30
