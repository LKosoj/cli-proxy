# Node: config_example.yaml

Generated: 2026-04-27T22:43:23Z

## Purpose
`/srv/git_projects/cli-proxy/config_example.yaml` is the sample YAML contract for CLI Proxy runtime configuration. It documents safe placeholder values, top-level config sections, defaults, comments/runbooks, and example tool/MCP/preset entries that must stay loadable by the validated config runtime.

## Scope
- Source glob: `config_example.yaml`
- File: `/srv/git_projects/cli-proxy/config_example.yaml`
- Includes sample sections: `telegram`, `tools`, `defaults`, `mcp`, `miniapp`, `thread_mode`, `webhooks`, `scheduler`, `security.rate_limits`, `lint_evolution`, `mcp_clients`, and `presets`.
- Excludes the local runtime config `/srv/git_projects/cli-proxy/config.yaml`; update that file separately when changing real operator defaults or secrets.

## Instructions for agent
- Start with `/srv/git_projects/cli-proxy/.cli-proxy/.codebase_map/INDEX.md`, then this node, then `/srv/git_projects/cli-proxy/config_example.yaml`.
- Before changing sample keys, verify the matching schema/default/serialization path in `/srv/git_projects/cli-proxy/config.py` and `/srv/git_projects/cli-proxy/app/config_runtime/**`.
- Keep `/srv/git_projects/cli-proxy/config_example.yaml` synchronized with `/srv/git_projects/cli-proxy/config.yaml`, `/srv/git_projects/cli-proxy/README.md`, `/srv/git_projects/cli-proxy/README_EN.MD`, MiniApp config UI, and Desktop config editor when config fields or semantics change.
- Preserve example safety: keep secrets as placeholders/nulls and avoid committing real tokens, API keys, chat IDs, or host credentials.
- Use targeted config tests for behavioral changes, especially `/srv/git_projects/cli-proxy/tests/test_config_loader.py` and `/srv/git_projects/cli-proxy/tests/test_config_models.py`.

## Source of truth
- `/srv/git_projects/cli-proxy/config_example.yaml` - sample YAML users copy or compare against.
- `/srv/git_projects/cli-proxy/config.py` - legacy dataclass contract consumed by bot, Desktop, MiniApp, sessions, and modes.
- `/srv/git_projects/cli-proxy/app/config_runtime/models.py` - validated Pydantic schema for accepted YAML keys and values.
- `/srv/git_projects/cli-proxy/app/config_runtime/loader.py` - YAML, `.env`, process env, and `CLI_PROXY__*` overlay behavior.
- `/srv/git_projects/cli-proxy/app/config_runtime/adapter.py` and `/srv/git_projects/cli-proxy/app/config_runtime/serialization.py` - mapping and dump order for runtime config.
- `/srv/git_projects/cli-proxy/tests/test_config_loader.py` and `/srv/git_projects/cli-proxy/tests/test_config_models.py` - repository checks that parse and assert the sample config contract.

## When to update
- Any change to `/srv/git_projects/cli-proxy/config_example.yaml`.
- Any config field, default, validation rule, env override, adapter, or serialization change in `/srv/git_projects/cli-proxy/config.py` or `/srv/git_projects/cli-proxy/app/config_runtime/**`.
- Any corresponding runtime config change in `/srv/git_projects/cli-proxy/config.yaml`.
- Any config editor/schema change in `/srv/git_projects/cli-proxy/miniapp/services/config_service.py`, `/srv/git_projects/cli-proxy/miniapp/static/app.js`, or `/srv/git_projects/cli-proxy/desktop/widgets/config_editor.py`.
- Any user-facing config documentation change in `/srv/git_projects/cli-proxy/README.md` or `/srv/git_projects/cli-proxy/README_EN.MD`.
- Any targeted config test change under `/srv/git_projects/cli-proxy/tests/test_config*.py` or MiniApp/Desktop config editor tests.

## Related nodes
- `/srv/git_projects/cli-proxy/.cli-proxy/.codebase_map/nodes/config-py.md` - dataclass contract and public config facade.
- `/srv/git_projects/cli-proxy/.cli-proxy/.codebase_map/nodes/app.md` - validated config runtime, runtime reload, and shared services.
- `/srv/git_projects/cli-proxy/.cli-proxy/.codebase_map/nodes/miniapp.md` - MiniApp config schema/editor and config API.
- `/srv/git_projects/cli-proxy/.cli-proxy/.codebase_map/nodes/desktop.md` - Desktop config editor/runtime consumers.
- `/srv/git_projects/cli-proxy/.cli-proxy/.codebase_map/nodes/bot-py.md` - bot startup and runtime consumption of config.
- `/srv/git_projects/cli-proxy/.cli-proxy/.codebase_map/nodes/modes.md` - mode runtime consumers of defaults/tool settings.
- `/srv/git_projects/cli-proxy/.cli-proxy/.codebase_map/nodes/tests.md` - targeted config, MiniApp, Desktop, and bot coverage.

## Last reviewed
- 2026-05-15
