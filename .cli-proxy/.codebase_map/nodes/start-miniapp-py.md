# Node: start_miniapp.py

Generated: 2026-04-27T22:43:23Z

## Purpose
`/srv/git_projects/cli-proxy/start_miniapp.py` is the standalone local MiniApp launcher. It loads `/srv/git_projects/cli-proxy/config.yaml`, forces `config.miniapp.enabled = True`, builds `BotApp`, ensures a shared HTTP ingress exists, starts that ingress, and keeps the process alive for local MiniApp access at `/cli-proxy/`.

## Scope
- Source glob: `start_miniapp.py`
- File: `/srv/git_projects/cli-proxy/start_miniapp.py`
- Includes: stdout logging setup, `load_config("config.yaml")`, `BotApp(config)` construction, fallback creation of `SharedHttpIngress(host="127.0.0.1", port=8088)`, `shared_http_ingress.start()`, and the keep-alive loop.
- Excludes: MiniApp route handlers in `/srv/git_projects/cli-proxy/miniapp/routes.py`, MiniApp mounting logic in `/srv/git_projects/cli-proxy/miniapp/server.py`, shared ingress implementation in `/srv/git_projects/cli-proxy/app/services/shared_http_ingress.py`, and bot runtime polling in `/srv/git_projects/cli-proxy/bot.py`.

## Instructions for agent
- Start with `/srv/git_projects/cli-proxy/.cli-proxy/.codebase_map/INDEX.md`, then this node, then `/srv/git_projects/cli-proxy/start_miniapp.py`.
- Before claiming startup behavior, verify the exact code path in `/srv/git_projects/cli-proxy/start_miniapp.py`; do not infer behavior from `/srv/git_projects/cli-proxy/bot.py` or `/srv/git_projects/cli-proxy/miniapp/server.py` without checking those files too.
- For config or bind-address changes, also inspect `/srv/git_projects/cli-proxy/config.py`, `/srv/git_projects/cli-proxy/config.yaml`, `/srv/git_projects/cli-proxy/config_example.yaml`, and `/srv/git_projects/cli-proxy/app/services/shared_http_ingress.py`.
- For MiniApp route, mount, or request-size behavior, inspect `/srv/git_projects/cli-proxy/miniapp/server.py`, `/srv/git_projects/cli-proxy/miniapp/routes.py`, and the closest MiniApp tests.
- Do not kill the process on port `8088`.
- Keep edits surgical; validate with targeted tests only unless shared runtime behavior changed.

## Source of truth
- `/srv/git_projects/cli-proxy/start_miniapp.py` - standalone launcher behavior.
- `/srv/git_projects/cli-proxy/.cli-proxy/.codebase_map/api/start_miniapp-py.md` - generated symbol inventory only.
- `/srv/git_projects/cli-proxy/config.py` - `MiniAppConfig` and `AppConfig` fields consumed after `load_config()`.
- `/srv/git_projects/cli-proxy/bot.py` - `BotApp` construction and default `shared_http_ingress` attachment.
- `/srv/git_projects/cli-proxy/app/services/shared_http_ingress.py` - host, port, start/stop, mount, and client body limit behavior.
- `/srv/git_projects/cli-proxy/miniapp/server.py` - MiniApp shared-ingress mounting and runtime enabled guard.
- `/srv/git_projects/cli-proxy/tests/smoke/test_miniapp_server_smoke.py` and `/srv/git_projects/cli-proxy/tests/test_miniapp_routes_integration.py` - closest regression coverage for shared-ingress MiniApp startup paths.

## When to update
- Any change to `/srv/git_projects/cli-proxy/start_miniapp.py`.
- Any change in `/srv/git_projects/cli-proxy/bot.py` that changes `BotApp` construction or `shared_http_ingress` initialization.
- Any MiniApp config contract change in `/srv/git_projects/cli-proxy/config.py`, `/srv/git_projects/cli-proxy/config.yaml`, or `/srv/git_projects/cli-proxy/config_example.yaml`.
- Any change in `/srv/git_projects/cli-proxy/app/services/shared_http_ingress.py` that affects host, port, route mounting, startup, shutdown, or request body limits.
- Any change in `/srv/git_projects/cli-proxy/miniapp/server.py` that affects MiniApp enablement, base path, mounting, or shared ingress usage.
- Any targeted startup or MiniApp ingress test change under `/srv/git_projects/cli-proxy/tests/smoke/test_miniapp_server_smoke.py` or `/srv/git_projects/cli-proxy/tests/test_miniapp_routes_integration.py`.
- Update `Last reviewed` after source verification.

## Related nodes
- `/srv/git_projects/cli-proxy/.cli-proxy/.codebase_map/nodes/miniapp.md` - MiniApp server, routes, static UI, and MiniApp tests.
- `/srv/git_projects/cli-proxy/.cli-proxy/.codebase_map/nodes/app.md` - shared HTTP ingress and app services.
- `/srv/git_projects/cli-proxy/.cli-proxy/.codebase_map/nodes/bot-py.md` - `BotApp` startup wiring and shared runtime services.
- `/srv/git_projects/cli-proxy/.cli-proxy/.codebase_map/nodes/config-py.md` - `MiniAppConfig`, `AppConfig`, and config facade.
- `/srv/git_projects/cli-proxy/.cli-proxy/.codebase_map/nodes/config-example-yaml.md` - sample MiniApp config keys.
- `/srv/git_projects/cli-proxy/.cli-proxy/.codebase_map/nodes/tests.md` - targeted smoke and MiniApp ingress coverage.

## Last reviewed
- 2026-04-28
