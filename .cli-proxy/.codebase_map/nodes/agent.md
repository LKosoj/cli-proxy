# Node: agent

Generated: 2026-06-17T10:46:18Z

## Purpose
Instruction node for `agent` area.

## Scope
- Source glob: `agent/**`
- Estimated files: 61
- Current files: 62 under `agent/**` as of last review.

## Instructions for agent
- Read only files relevant to the active task.
- Prefer deterministic checks before edits.
- Keep changes minimal and validate with tests/linters where applicable.

## Source of truth
- `agent/**`
- `agent/__init__.py`
- `agent/approvals/blocked-patterns.json`
- `agent/mcp/__init__.py`
- `agent/plugins/__init__.py`
- `agent/tooling/__init__.py`
- `agent/plugins/plantuml.jar`
- `agent/analyst_prompts.py`
- `agent/cli_routing.py`
- `agent/manager.py`
- `agent/manager_core.py`

## Module API
Детальные интерфейсы модулей этой области:

- [agent/analyst_prompts.py](../api/agent/analyst_prompts-py.md)
- [agent/cli_routing.py](../api/agent/cli_routing-py.md)
- [agent/manager_core.py](../api/agent/manager_core-py.md)
- [agent/plugins/admin_escalate.py](../api/agent/plugins/admin_escalate-py.md)
- [agent/plugins/admin_execute_action.py](../api/agent/plugins/admin_execute_action-py.md)
- [agent/plugins/admin_get_dossier.py](../api/agent/plugins/admin_get_dossier-py.md)
- [agent/plugins/admin_remember_fact.py](../api/agent/plugins/admin_remember_fact-py.md)
- [agent/plugins/admin_remember_note.py](../api/agent/plugins/admin_remember_note-py.md)
- [agent/plugins/admin_script_run.py](../api/agent/plugins/admin_script_run-py.md)
- [agent/plugins/analyst_intent_plugin.py](../api/agent/plugins/analyst_intent_plugin-py.md)
- [agent/plugins/ask_user.py](../api/agent/plugins/ask_user-py.md)
- [agent/plugins/auto_tts.py](../api/agent/plugins/auto_tts-py.md)
- [agent/plugins/base.py](../api/agent/plugins/base-py.md)
- [agent/plugins/brainstorm.py](../api/agent/plugins/brainstorm-py.md)
- [agent/plugins/chief.py](../api/agent/plugins/chief-py.md)

## When to update
- Any commit touching `agent/**`.
- Any commit touching `app/**` because this node has import/call dependency on it.
- Any commit touching `bot.py` because this node has import/call dependency on it.
- Any commit touching `config.py` because this node has import/call dependency on it.
- Any commit touching `config_example.yaml` because this node has import/call dependency on it.
- Any commit touching `desktop/**` because this node has import/call dependency on it.
- Any architecture or behavior change affecting this area.

## Related nodes
- `nodes/app.md`
- `nodes/bot-py.md`
- `nodes/config-py.md`
- `nodes/config-example-yaml.md`
- `nodes/desktop.md`
- `nodes/i18n.md`
- `nodes/locales.md`
- `nodes/miniapp.md`
- `app` confidence=0.95 via L0/L1/L2
- `bot.py` confidence=0.94 via L0/L2
- `config.py` confidence=0.90 via L0/L2
- `config_example.yaml` confidence=0.88 via L0
- `desktop` confidence=0.95 via L0
- `i18n` confidence=0.90 via L0/L1/L2
- `locales` confidence=0.88 via L0
- `miniapp` confidence=0.95 via L0

## Owner
- project-maintainers

## Last reviewed
- 2026-08-08T00:00:00Z
