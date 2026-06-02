# Node: config.py

Generated: 2026-04-27T22:43:23Z

## Purpose
`/srv/git_projects/cli-proxy/config.py` defines the legacy `AppConfig` dataclass contract used by the bot, Desktop, MiniApp, sessions, modes, and app services. It also exposes the public config facade functions that load validated YAML into dataclasses, serialize an `AppConfig`, and save it back to disk.

## Scope
- Source glob: `config.py`
- File: `/srv/git_projects/cli-proxy/config.py`
- Includes: config dataclasses for Telegram, tools, defaults, MCP, MiniApp, thread mode, webhooks, scheduler, security rate limits, lint evolution, SSH hosts, and `AppConfig`.
- Includes: `load_config(path)`, `app_config_to_dict(config)`, and `save_config(config)`.
- Excludes: validated schema internals in `/srv/git_projects/cli-proxy/app/config_runtime/models.py`, YAML/env overlay logic in `/srv/git_projects/cli-proxy/app/config_runtime/loader.py`, adapter mapping in `/srv/git_projects/cli-proxy/app/config_runtime/adapter.py`, and serialization internals in `/srv/git_projects/cli-proxy/app/config_runtime/serialization.py`; inspect those files when config fields or semantics change.

## Instructions for agent
- Start with `/srv/git_projects/cli-proxy/.cli-proxy/.codebase_map/INDEX.md`, then this node, then `/srv/git_projects/cli-proxy/config.py`.
- Before claiming runtime config behavior, verify the exact function in source; `load_config()` delegates to `/srv/git_projects/cli-proxy/app/config_runtime/loader.py` and `/srv/git_projects/cli-proxy/app/config_runtime/adapter.py`, while `app_config_to_dict()` and `save_config()` delegate to `/srv/git_projects/cli-proxy/app/config_runtime/serialization.py`.
- Keep top-level imports in `/srv/git_projects/cli-proxy/config.py` narrow; existing runtime-heavy imports are inside facade functions.
- For any config field, default, validation, env override, or serialization change, keep `/srv/git_projects/cli-proxy/config.py`, `/srv/git_projects/cli-proxy/config.yaml`, `/srv/git_projects/cli-proxy/config_example.yaml`, `/srv/git_projects/cli-proxy/app/config_runtime/**`, `/srv/git_projects/cli-proxy/miniapp/services/config_service.py`, `/srv/git_projects/cli-proxy/miniapp/static/app.js`, `/srv/git_projects/cli-proxy/desktop/widgets/config_editor.py`, `/srv/git_projects/cli-proxy/README.md`, and `/srv/git_projects/cli-proxy/README_EN.MD` synchronized.
- Use targeted tests for changed config surfaces, starting with `/srv/git_projects/cli-proxy/tests/test_config_adapter.py`, `/srv/git_projects/cli-proxy/tests/test_config_serialization.py`, `/srv/git_projects/cli-proxy/tests/test_config_loader.py`, and UI-specific MiniApp/Desktop tests when those surfaces change.

## Source of truth
- `/srv/git_projects/cli-proxy/config.py` - dataclass contract and public config facade functions.
- `/srv/git_projects/cli-proxy/.cli-proxy/.codebase_map/api/config-py.md` - generated symbol inventory only; verify behavior in source.
- `/srv/git_projects/cli-proxy/app/config_runtime/models.py` - validated schema model consumed before `AppConfig` adaptation.
- `/srv/git_projects/cli-proxy/app/config_runtime/loader.py` - YAML reading, `.env`/process environment overlays, validation fail-fast behavior.
- `/srv/git_projects/cli-proxy/app/config_runtime/adapter.py` - mapping from validated settings into `config.py` dataclasses.
- `/srv/git_projects/cli-proxy/app/config_runtime/serialization.py` - stable serialization and YAML dump behavior used by `save_config()`.
- `/srv/git_projects/cli-proxy/config.yaml` and `/srv/git_projects/cli-proxy/config_example.yaml` - concrete runtime and sample YAML contracts.

## Module API
Детальные интерфейсы модулей этой области:

- [config.py](../api/config-py.md)

## When to update
- Any change to `/srv/git_projects/cli-proxy/config.py`, including dataclass fields, defaults, imports, `load_config()`, `app_config_to_dict()`, or `save_config()`.
- Any config schema, validation, env override, adapter, or serialization change in `/srv/git_projects/cli-proxy/app/config_runtime/**`.
- Any runtime/sample config contract change in `/srv/git_projects/cli-proxy/config.yaml` or `/srv/git_projects/cli-proxy/config_example.yaml`.
- Any bot startup or runtime reload change in `/srv/git_projects/cli-proxy/bot.py`, `/srv/git_projects/cli-proxy/app/bootstrap.py`, or `/srv/git_projects/cli-proxy/app/services/config_service.py` that changes how `AppConfig` is loaded or consumed.
- Any Desktop or MiniApp config editor contract change in `/srv/git_projects/cli-proxy/desktop/widgets/config_editor.py`, `/srv/git_projects/cli-proxy/miniapp/services/config_service.py`, or `/srv/git_projects/cli-proxy/miniapp/static/app.js`.
- Any config-facing test change under `/srv/git_projects/cli-proxy/tests/test_config*.py`, `/srv/git_projects/cli-proxy/tests/test_miniapp_config*.py`, or `/srv/git_projects/cli-proxy/tests/test_config_editor.py`.

## Related nodes
- `/srv/git_projects/cli-proxy/.cli-proxy/.codebase_map/nodes/app.md` - owns `app/config_runtime/**` and shared config services.
- `/srv/git_projects/cli-proxy/.cli-proxy/.codebase_map/nodes/bot-py.md` - loads `config.yaml` at startup and consumes `AppConfig`.
- `/srv/git_projects/cli-proxy/.cli-proxy/.codebase_map/nodes/config-example-yaml.md` - sample YAML that must track `config.py` and validated schema fields.
- `/srv/git_projects/cli-proxy/.cli-proxy/.codebase_map/nodes/desktop.md` - Desktop config editor/runtime consumers.
- `/srv/git_projects/cli-proxy/.cli-proxy/.codebase_map/nodes/miniapp.md` - MiniApp config schema, editor, and config API consumers.
- `/srv/git_projects/cli-proxy/.cli-proxy/.codebase_map/nodes/modes.md` - mode runtime code consumes `AppConfig` defaults and tool settings.
- `/srv/git_projects/cli-proxy/.cli-proxy/.codebase_map/nodes/session-py.md` - session construction consumes tool/default/SSH config.
- `/srv/git_projects/cli-proxy/.cli-proxy/.codebase_map/nodes/sessions.md` - session services consume `AppConfig` through runtime dependencies.
- `/srv/git_projects/cli-proxy/.cli-proxy/.codebase_map/nodes/tests.md` - targeted config, Desktop, MiniApp, bot, and mode coverage.

## Owner
- project-maintainers

## Last reviewed
- 2026-06-02
