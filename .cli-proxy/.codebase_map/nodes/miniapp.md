# Node: miniapp

Generated: 2026-06-17T10:46:18Z

## Purpose
Instruction node for `miniapp` area.

## Scope
- Source glob: `miniapp/**`
- Estimated files: 22

## Instructions for agent
- Read only files relevant to the active task.
- Prefer deterministic checks before edits.
- Keep changes minimal and validate with tests/linters where applicable.

## Source of truth
- `miniapp/**`
- `miniapp/__init__.py`
- `miniapp/services/__init__.py`
- `miniapp/static/app.js`
- `miniapp/static/index.html`
- `miniapp/static/styles.css`
- `miniapp/auth.py`
- `miniapp/route_context.py`
- `miniapp/routes.py`
- `miniapp/routes_admin.py`
- `miniapp/routes_config.py`

## Module API
Детальные интерфейсы модулей этой области:

- [miniapp/auth.py](../api/miniapp/auth-py.md)
- [miniapp/route_context.py](../api/miniapp/route_context-py.md)
- [miniapp/routes.py](../api/miniapp/routes-py.md)
- [miniapp/routes_admin.py](../api/miniapp/routes_admin-py.md)
- [miniapp/routes_config.py](../api/miniapp/routes_config-py.md)
- [miniapp/routes_foundation.py](../api/miniapp/routes_foundation-py.md)
- [miniapp/routes_json.py](../api/miniapp/routes_json-py.md)
- [miniapp/routes_logs.py](../api/miniapp/routes_logs-py.md)
- [miniapp/routes_reports.py](../api/miniapp/routes_reports-py.md)

## When to update
- Any commit touching `miniapp/**`.
- Any commit touching `agent/**` because this node has import/call dependency on it.
- Any commit touching `app/**` because this node has import/call dependency on it.
- Any commit touching `bot.py` because this node has import/call dependency on it.
- Any commit touching `config.py` because this node has import/call dependency on it.
- Any commit touching `config_example.yaml` because this node has import/call dependency on it.
- Any architecture or behavior change affecting this area.

## Related nodes
- `nodes/agent.md`
- `nodes/app.md`
- `nodes/bot-py.md`
- `nodes/config-py.md`
- `nodes/config-example-yaml.md`
- `nodes/desktop.md`
- `nodes/i18n.md`
- `nodes/locales.md`
- `agent` confidence=0.95 via L0
- `app` confidence=0.95 via L0/L1/L2
- `bot.py` confidence=0.94 via L0
- `config.py` confidence=0.90 via L2
- `config_example.yaml` confidence=0.77 via L0
- `desktop` confidence=0.95 via L0
- `i18n` confidence=0.90 via L1/L2
- `locales` confidence=0.94 via L0

## Owner
- project-maintainers

## Last reviewed
- 2026-07-04T06:10:00Z
