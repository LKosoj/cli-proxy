# Node: tg

Generated: 2026-04-27T22:43:23Z

## Purpose
`tg/**` is the Telegram interface layer. It wires `python-telegram-bot` handlers, defines Telegram command menus and policy, processes inbound text/doc/photo updates, routes inline callbacks, and converts Markdown-ish output into Telegram-safe text/entities.

## Scope
- Source glob: `tg/**`
- Current files: 17 under `tg/**` as of last review.
- Entry and registration: `tg/wiring.py`, `tg/command_registry.py`, `tg/command_policy.py`.
- Command/UI handlers: `tg/handlers.py`.
- Inbound message and attachment processing: `tg/message_processor.py`.
- Inline callback routing: `tg/callbacks.py`, `tg/callback_actions/*.py`.
- Telegram formatting helpers: `tg/markdown.py`.

## Instructions for agent
- Start with `.cli-proxy/.codebase_map/INDEX.md`, then this node, then task-specific files under `tg/**`.
- For command registration or command menu changes, read `tg/wiring.py`, `tg/command_registry.py`, and the matching `BotHandlers` method in `tg/handlers.py`.
- For inline button behavior, read `tg/callbacks.py` plus the matching file under `tg/callback_actions/`.
- For text/document/photo ingress, read `tg/message_processor.py` and verify Telegram route/session resolution through the exact helper used by the changed flow.
- Keep outbound Telegram text on the shared Markdown/entities path in `tg/markdown.py` and `app/services/telegram_transport.py`; do not add new `md2=False` send paths.
- For mode-facing Telegram changes, read `modes/DEVELOPMENT.md` and use `modes/sdk/**`/`BaseMode` helpers instead of adding shared mode logic to `BotApp`.
- Validate Python edits with targeted `.venv/bin/pytest -q` tests near the changed behavior and `.venv/bin/flake8`.

## Source of truth
- `tg/__init__.py`
- `tg/command_policy.py`
- `tg/command_registry.py`
- `tg/wiring.py`
- `tg/handlers.py`
- `tg/message_processor.py`
- `tg/callbacks.py`
- `tg/markdown.py`
- `tg/callback_actions/__init__.py`
- `tg/callback_actions/dirs.py`
- `tg/callback_actions/files.py`
- `tg/callback_actions/preset.py`
- `tg/callback_actions/protocol.py`
- `tg/callback_actions/session.py`

## When to update
- Any change under `tg/**`.
- Any change in `bot.py` that changes Telegram handler composition, `BotApp` methods called by `tg/**`, or Telegram startup wiring.
- Any change in `app/services/telegram_transport.py`, `app/services/input_dispatch_service.py`, menu visibility, path normalization, state repository, SSH config, or session creation services used by `tg/**`.
- Any mode SDK or mode plugin change that changes Telegram command exposure, callback handling, directory picker flows, dialogs, or transport context.
- Any config change in `config.py`, `config.yaml`, or `config_example.yaml` that affects Telegram token/admin IDs, workdir/state paths, MiniApp command behavior, SSH visibility, or command/menu policy.
- Any test change that adds, removes, or materially changes targeted Telegram coverage.

## Related nodes
- `.cli-proxy/.codebase_map/nodes/bot-py.md` - `bot.py` imports `tg.command_policy`, `tg.command_registry`, `tg.handlers`, `tg.callbacks`, `tg.message_processor`, and `tg.wiring`.
- `.cli-proxy/.codebase_map/nodes/app.md` - `tg/**` uses app services for input dispatch, menu visibility, paths, state repository, SSH config, Telegram transport, and session creation.
- `.cli-proxy/.codebase_map/nodes/agent.md` - `tg/wiring.py` installs plugin handlers from `agent.telegram_wiring`; callback protocol actions import agent command approval/denial helpers.
- `.cli-proxy/.codebase_map/nodes/modes.md` - command registry and callbacks expose mode menus, mode callbacks, dialogs, and directory picker integration.
- `.cli-proxy/.codebase_map/nodes/sessions.md` - handlers and callbacks read/update active mode, SSH flag, orchestrator flag, and visible session state.
- `.cli-proxy/.codebase_map/nodes/session-py.md` - handlers operate on `Session` and session runtime UID data.
- `.cli-proxy/.codebase_map/nodes/config-py.md` - Telegram handlers read runtime config defaults and Telegram/admin settings.
- `.cli-proxy/.codebase_map/nodes/config-example-yaml.md` - sample config must track Telegram-facing config fields.
- `.cli-proxy/.codebase_map/nodes/tests.md` - targeted coverage includes Telegram handler, callback, markdown, routing, ingress security, SSH UI, file, git, and self-update tests.

## Last reviewed
- 2026-05-30
