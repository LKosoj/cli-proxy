# Node: desktop

Generated: 2026-06-17T10:46:18Z

## Purpose
Instruction node for `desktop` area.

## Scope
- Source glob: `desktop/**`
- Estimated files: 32
- Current files: 32 under `desktop/**` as of last review.

## Instructions for agent
- Read only files relevant to the active task.
- Prefer deterministic checks before edits.
- Keep changes minimal and validate with tests/linters where applicable.

## Source of truth
- `desktop/**`
- `desktop/main.py`
- `desktop/services/__init__.py`
- `desktop/widgets/admin_chat_section.py`
- `desktop/main_window.py`
- `desktop/services/application_facade.py`
- `desktop/services/desktop_admin_facade.py`
- `desktop/services/desktop_git_service.py`
- `desktop/services/desktop_identity_provider.py`
- `desktop/services/desktop_state_service.py`
- `desktop/services/pending_input_ui.py`

## Module API
Детальные интерфейсы модулей этой области:

- [desktop/main.py](../api/desktop/main-py.md)
- [desktop/widgets/admin_chat_section.py](../api/desktop/widgets/admin_chat_section-py.md)
- [desktop/main_window.py](../api/desktop/main_window-py.md)
- [desktop/services/application_facade.py](../api/desktop/services/application_facade-py.md)
- [desktop/services/desktop_admin_facade.py](../api/desktop/services/desktop_admin_facade-py.md)
- [desktop/services/desktop_git_service.py](../api/desktop/services/desktop_git_service-py.md)
- [desktop/services/desktop_identity_provider.py](../api/desktop/services/desktop_identity_provider-py.md)
- [desktop/services/desktop_state_service.py](../api/desktop/services/desktop_state_service-py.md)
- [desktop/services/pending_input_ui.py](../api/desktop/services/pending_input_ui-py.md)
- [desktop/services/theme_service.py](../api/desktop/services/theme_service-py.md)
- [desktop/widgets/admin_panel.py](../api/desktop/widgets/admin_panel-py.md)
- [desktop/widgets/chat_view.py](../api/desktop/widgets/chat_view-py.md)
- [desktop/widgets/command_palette.py](../api/desktop/widgets/command_palette-py.md)
- [desktop/widgets/config_editor.py](../api/desktop/widgets/config_editor-py.md)
- [desktop/widgets/files_panel.py](../api/desktop/widgets/files_panel-py.md)

## When to update
- Any commit touching `desktop/**`.
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
- `nodes/i18n.md`
- `nodes/locales.md`
- `nodes/miniapp.md`
- `agent` confidence=0.95 via L0/L1/L2
- `app` confidence=0.95 via L0/L1/L2
- `bot.py` confidence=0.95 via L0
- `config.py` confidence=0.90 via L0/L2
- `config_example.yaml` confidence=0.66 via L0
- `i18n` confidence=0.90 via L0/L1/L2
- `locales` confidence=0.95 via L0
- `miniapp` confidence=0.95 via L0

## Owner
- project-maintainers

## Last reviewed
- 2026-07-07T00:00:00Z
