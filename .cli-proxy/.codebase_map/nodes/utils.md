# Node: utils

Generated: 2026-06-17T10:46:18Z

## Purpose
Instruction node for `utils` area.

## Scope
- Source glob: `utils/**`
- Estimated files: 8

## Instructions for agent
- Read only files relevant to the active task.
- Prefer deterministic checks before edits.
- Keep changes minimal and validate with tests/linters where applicable.

## Source of truth
- `utils/**`
- `utils/__init__.py`
- `utils/cli.py`
- `utils/html_renderer.py`
- `utils/lang.py`
- `utils/paths.py`
- `utils/source_artifact.py`
- `utils/text.py`
- `utils/ui.py`

## Module API
Детальные интерфейсы модулей этой области:

- [utils/cli.py](../api/utils/cli-py.md)
- [utils/html_renderer.py](../api/utils/html_renderer-py.md)
- [utils/lang.py](../api/utils/lang-py.md)
- [utils/paths.py](../api/utils/paths-py.md)
- [utils/source_artifact.py](../api/utils/source_artifact-py.md)
- [utils/text.py](../api/utils/text-py.md)
- [utils/ui.py](../api/utils/ui-py.md)

## When to update
- Any commit touching `utils/**`.
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
- `agent` confidence=0.77 via L0
- `app` confidence=0.88 via L0
- `bot.py` confidence=0.66 via L0
- `config.py` confidence=0.90 via L0/L2
- `config_example.yaml` confidence=0.66 via L0
- `desktop` confidence=0.77 via L0
- `i18n` confidence=0.90 via L0/L1/L2
- `locales` confidence=0.66 via L0

## Owner
- project-maintainers

## Last reviewed
- 2026-06-17T10:46:18Z
