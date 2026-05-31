# Node: agent

Generated: 2026-04-27T22:43:22Z

## Purpose
`agent/**` contains the local agent tool layer and manager/analyst support code: manager orchestration, CLI routing, Telegram plugin handler wiring, plugin contracts, concrete tool plugins, command approvals, and shared helper implementations used by modes and runtime entrypoints.

## Scope
- Source glob: `agent/**`
- Current files: 61 under `agent/**` as of last review.
- Manager orchestration and prompts: `agent/manager.py`, `agent/manager_core.py`, `agent/manager_prompts.py`
- Analyst prompt assembly: `agent/analyst_prompts.py`
- CLI work-type routing and failover: `agent/cli_routing.py`
- Telegram plugin handler wiring: `agent/telegram_wiring.py`
- Tool plugin contract and local plugins: `agent/plugins/base.py`, `agent/plugins/*.py`, `agent/plugins/plantuml.jar`
- Shared tool helpers and command approvals: `agent/tooling/helpers.py`, `agent/approvals/blocked-patterns.json`

## Instructions for agent
- Start from `.cli-proxy/.codebase_map/INDEX.md`, then this node, then the task-specific files under `agent/**`.
- Before claiming runtime behavior, verify the exact function/class in source and cite concrete `path:line`.
- For manager behavior, read `modes/DEVELOPMENT.md`, `agent/manager_core.py`, and the relevant files under `modes/manager/**`; keep shared mode logic in SDK services/BaseMode paths, not direct `BotApp` coupling.
- For tool/plugin changes, preserve the `ToolPlugin`/`DialogMixin` contract in `agent/plugins/base.py` and the `ToolSpec` contract exposed through `modes/sdk/runtime/tooling/spec.py`.
- For command, file, web, SSH, or approval behavior, reuse `agent/tooling/helpers.py` and update `agent/approvals/blocked-patterns.json` only when the policy itself changes.
- For CLI routing changes, check `agent/cli_routing.py`, `config.py`, `app/config_runtime/**`, and `config_example.yaml` together when config fields or defaults change.
- Validate with targeted tests under `tests/**` for the changed surface, such as `tests/test_cli_routing_failover.py`, `tests/test_agent_plugins.py`, `tests/test_agent_plugin_dialog_mixin.py`, `tests/test_manager_*.py`, or plugin-specific tests.

## Source of truth
- `agent/manager.py` - compatibility export for `agent.manager_core`.
- `agent/manager_core.py` - `ManagerOrchestrator`, manager run state helpers, plan metadata, response archive helpers, and manager CLI/OpenAI calls.
- `agent/manager_prompts.py` - manager decomposition, validation, review, and final audit prompt templates.
- `agent/analyst_prompts.py` - analyst prompt construction used by `modes/analyst/mode.py`.
- `agent/cli_routing.py` - `defaults.cli_routing` loading, work-type priority lists, session CLI switching, failover, and task-bearing CLI hook calls.
- `agent/telegram_wiring.py` - plugin-provided Telegram message/inline handler registration called by `tg/wiring.py`.
- `agent/plugins/base.py` - `ToolPlugin`, `DialogMixin`, and plugin UI/dialog protocol.
- `agent/plugins/*.py` - local tool implementations loaded by `modes/sdk/runtime/tooling/loader.py`.
- `agent/tooling/helpers.py` - pending command approval state, blocked command checks, command execution, path safety, output trimming, web search, and fetch helpers.
- `agent/approvals/blocked-patterns.json` - command blocking policy consumed by `agent/tooling/helpers.py`.
- `modes/sdk/runtime/tooling/loader.py`, `modes/sdk/runtime/tooling/registry.py`, `modes/sdk/runtime/tooling/spec.py`, `modes/sdk/runtime/tooling/mcp_plugin.py` - registry/loading/spec integration for `agent/plugins/**`.

## When to update
- Any commit touching `agent/**`.
- Any change to plugin loading, registry behavior, tool specs, MCP remote tool wrapping, or mode runtime tool execution under `modes/sdk/runtime/tooling/**`.
- Any change to manager mode orchestration, schemas, services, prompts, UI, or runner integration under `modes/manager/**`.
- Any change to analyst prompt usage in `modes/analyst/mode.py`.
- Any change to Telegram plugin handler registration in `tg/wiring.py` or plugin UI dispatch behavior.
- Any change to `defaults.cli_routing`, tool config, config runtime adapter/serialization, or sample config in `config.py`, `app/config_runtime/**`, `config.yaml`, or `config_example.yaml`.
- Any change to task deadline checker startup/shutdown that imports `agent/plugins/task_management.py`.
- Any targeted test addition/removal that changes coverage expectations for agent tools, manager orchestration, CLI routing, or plugin dialogs.

## Related nodes
- `nodes/modes.md` - consumes `agent.manager`, `agent.manager_core`, `agent.analyst_prompts`, and the tooling registry/loader integration for plugins.
- `nodes/app.md` - bootstraps `ToolRegistry` and starts the task deadline checker from `agent/plugins/task_management.py`.
- `nodes/tg.md` - `tg/wiring.py` delegates plugin handler registration to `agent/telegram_wiring.py`.
- `nodes/desktop.md` - desktop facade binds the shared `ToolRegistry` for plugin UI/runtime use.
- `nodes/bot-py.md` - bot runtime owns Telegram callbacks/messages that coexist with agent plugin handlers.
- `nodes/config-py.md` - `AppConfig` drives tool availability and `defaults.cli_routing`.
- `nodes/config-example-yaml.md` - sample config must track new or changed tool/routing config keys.
- `nodes/session-py.md`, `nodes/sessions.md` - sessions are switched by `agent/cli_routing.py` and carry tool execution state.
- `nodes/tests.md` - targeted coverage for agent plugins, CLI routing, manager orchestration, and Telegram/plugin UI flows.

## Last reviewed
- 2026-05-31
