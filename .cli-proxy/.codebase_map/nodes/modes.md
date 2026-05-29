# Node: modes

Generated: 2026-04-27T22:43:22Z

## Purpose
Context node for `modes/**`: pluggable modes, shared mode SDK, mode-owned runtimes, mode UI/callback handling, and mode-specific prompts/schemas/state.

## Scope
- Source glob: `modes/**`
- Current files: 167 under `modes/**` as of last review.
- Includes mode packages: `modes/agent/`, `modes/analyst/`, `modes/manager/`, `modes/webmaster/`, `modes/admin/`, `modes/codebase_mapper/`
- Includes shared mode infrastructure: `modes/sdk/**`, `modes/registry.py`, `modes/DEVELOPMENT.md`, `modes/codebase_mapper_constants.py`

## Instructions for agent
- Before changing mode behavior, read `modes/DEVELOPMENT.md` and the specific `<mode>/mode.py`, `<mode>/runner_service.py`, `<mode>/schemas.py`, `<mode>/ui.py`, and `<mode>/prompts.yaml` files involved.
- Register/load modes through `modes/registry.py`; mode packages must export `PLUGIN` from `<mode>/__init__.py`.
- Implement shared mode contracts through `modes/sdk/base.py` (`BaseMode`) and SDK services under `modes/sdk/services/**`; do not add new general mode logic that depends directly on `BotApp`.
- Use `modes/sdk/runtime/json_normalizer.py` for JSON parsing, normalization, and schema validation in mode/runtime code.
- For background work and callbacks, prefer `BaseMode._start_mode_task(...)` and `BaseMode._dispatch_callback_action(...)` over local task tracking or large inline dispatch blocks.
- Keep edits scoped to the active mode or shared SDK layer; update targeted tests under `tests/**` for changed contracts or behavior.

## Source of truth
- `modes/DEVELOPMENT.md`
- `modes/registry.py`
- `modes/sdk/base.py`
- `modes/sdk/services/**`
- `modes/sdk/runtime/**`
- `modes/agent/**`
- `modes/analyst/**`
- `modes/manager/**`
- `modes/webmaster/**`
- `modes/admin/**`
- `modes/codebase_mapper/**`
- `modes/codebase_mapper_constants.py`

## When to update
- Any commit touching `modes/**`.
- Any change to mode registration/loading semantics in `modes/registry.py`.
- Any change to `BaseMode`, mode SDK services, mode runtime contracts, JSON normalization, tooling, orchestration, or validation under `modes/sdk/**`.
- Any change to mode prompts, schemas, runner services, UI/callbacks, state stores, or mode-owned runtimes under `modes/{agent,analyst,manager,webmaster,admin,codebase_mapper}/**`.
- Any change in `app/**`, `agent/**`, `bot.py`, `desktop/**`, `miniapp/**`, `config.py`, or `config_example.yaml` that changes how modes are initialized, invoked, configured, displayed, or tested.

## Related nodes
- `nodes/app.md`
- `nodes/agent.md`
- `nodes/bot-py.md`
- `nodes/config-py.md`
- `nodes/config-example-yaml.md`
- `nodes/desktop.md`
- `nodes/miniapp.md`
- `nodes/session-py.md`
- `nodes/sessions.md`
- `nodes/tests.md`

## Last reviewed
- 2026-05-04
