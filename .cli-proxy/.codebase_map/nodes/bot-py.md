# Node: bot.py

Generated: 2026-04-27T22:43:23Z

## Purpose
`/srv/git_projects/cli-proxy/bot.py` is the Telegram bot runtime entrypoint. It builds the `telegram.ext.Application`, owns `TelegramInboundRoute` and `BotApp`, wires the service container, resolves Telegram chat/thread/session routing, delegates prompt and mode execution to session services, and starts polling from `main()`.

## Scope
- Source glob: `bot.py`
- File: `/srv/git_projects/cli-proxy/bot.py`
- Includes: Telegram inbound authorization/routing, `BotApp` service wiring, command/callback wrappers, MiniApp launch command, runtime config reload entrypoint, prompt/mode delegation, shutdown cleanup, `build_app()`, `main()`.
- Excludes: handler internals in `/srv/git_projects/cli-proxy/tg/**`, session execution internals in `/srv/git_projects/cli-proxy/sessions/**`, mode implementations in `/srv/git_projects/cli-proxy/modes/**`, MiniApp routes in `/srv/git_projects/cli-proxy/miniapp/**`, config model definitions in `/srv/git_projects/cli-proxy/config.py`.

## Instructions for agent
- Read `/srv/git_projects/cli-proxy/bot.py` before changing this area.
- For handler or command registration changes, also read `/srv/git_projects/cli-proxy/tg/wiring.py`, `/srv/git_projects/cli-proxy/tg/command_registry.py`, `/srv/git_projects/cli-proxy/tg/handlers.py`, `/srv/git_projects/cli-proxy/tg/callbacks.py`, and `/srv/git_projects/cli-proxy/tg/message_processor.py`.
- For startup, config, or lifecycle changes, also read `/srv/git_projects/cli-proxy/config.py`, `/srv/git_projects/cli-proxy/app/config_runtime/loader.py`, `/srv/git_projects/cli-proxy/app/bootstrap.py`, and `/srv/git_projects/cli-proxy/app/services/lifecycle_service.py`.
- For prompt, session, or mode execution changes, also read `/srv/git_projects/cli-proxy/sessions/session_management.py`, `/srv/git_projects/cli-proxy/sessions/session_run_service.py`, `/srv/git_projects/cli-proxy/modes/DEVELOPMENT.md`, and the relevant `/srv/git_projects/cli-proxy/modes/**` implementation.
- Keep `BotApp` as orchestration glue: prefer existing services, SDK helpers, and `tg/**` handlers over adding business logic directly to `/srv/git_projects/cli-proxy/bot.py`.
- Use targeted tests for the touched path; do not run the full suite unless shared runtime behavior changed or the user asked for a full smoke check.

## Source of truth
- `/srv/git_projects/cli-proxy/bot.py` — runtime behavior for this node.
- `/srv/git_projects/cli-proxy/.cli-proxy/.codebase_map/api/bot-py.md` — generated symbol inventory only; verify behavior in source.
- `/srv/git_projects/cli-proxy/tg/wiring.py` — Telegram handler registration against `BotApp`.
- `/srv/git_projects/cli-proxy/tg/command_registry.py` — bot command menu definitions.
- `/srv/git_projects/cli-proxy/app/bootstrap.py` — application service container built for `BotApp`.
- `/srv/git_projects/cli-proxy/config.py`, `/srv/git_projects/cli-proxy/config.yaml`, `/srv/git_projects/cli-proxy/config_example.yaml` — runtime config consumed by startup and `BotApp`.

## When to update
- Any change to `/srv/git_projects/cli-proxy/bot.py`, including imports, constants, `TelegramInboundRoute`, `BotApp`, `build_app()`, or `main()`.
- Any change in `/srv/git_projects/cli-proxy/tg/wiring.py`, `/srv/git_projects/cli-proxy/tg/command_registry.py`, `/srv/git_projects/cli-proxy/tg/handlers.py`, `/srv/git_projects/cli-proxy/tg/callbacks.py`, or `/srv/git_projects/cli-proxy/tg/message_processor.py` that changes how Telegram updates call `BotApp`.
- Any change in `/srv/git_projects/cli-proxy/sessions/session_management.py` or `/srv/git_projects/cli-proxy/sessions/session_run_service.py` that changes `BotApp` prompt/mode delegation.
- Any runtime config change consumed by `/srv/git_projects/cli-proxy/bot.py`, especially in `/srv/git_projects/cli-proxy/config.py`, `/srv/git_projects/cli-proxy/config.yaml`, or `/srv/git_projects/cli-proxy/config_example.yaml`.
- Any mode SDK/container contract change used by `BotApp`, including `/srv/git_projects/cli-proxy/app/bootstrap.py`, `/srv/git_projects/cli-proxy/modes/sdk/**`, or `/srv/git_projects/cli-proxy/modes/DEVELOPMENT.md`.
- Any MiniApp launch/auth integration change involving `BotApp` in `/srv/git_projects/cli-proxy/miniapp/**` or `/srv/git_projects/cli-proxy/app/security/**`.
- Update `Last reviewed` after source verification.

## Related nodes
- `/srv/git_projects/cli-proxy/.cli-proxy/.codebase_map/nodes/tg.md`
- `/srv/git_projects/cli-proxy/.cli-proxy/.codebase_map/nodes/app.md`
- `/srv/git_projects/cli-proxy/.cli-proxy/.codebase_map/nodes/sessions.md`
- `/srv/git_projects/cli-proxy/.cli-proxy/.codebase_map/nodes/modes.md`
- `/srv/git_projects/cli-proxy/.cli-proxy/.codebase_map/nodes/agent.md`
- `/srv/git_projects/cli-proxy/.cli-proxy/.codebase_map/nodes/miniapp.md`
- `/srv/git_projects/cli-proxy/.cli-proxy/.codebase_map/nodes/config-py.md`
- `/srv/git_projects/cli-proxy/.cli-proxy/.codebase_map/nodes/config-example-yaml.md`
- `/srv/git_projects/cli-proxy/.cli-proxy/.codebase_map/nodes/session-py.md`
- `/srv/git_projects/cli-proxy/.cli-proxy/.codebase_map/nodes/summary-py.md`
- `/srv/git_projects/cli-proxy/.cli-proxy/.codebase_map/nodes/utils.md`
- `/srv/git_projects/cli-proxy/.cli-proxy/.codebase_map/nodes/tests.md`

## Last reviewed
- 2026-05-17
