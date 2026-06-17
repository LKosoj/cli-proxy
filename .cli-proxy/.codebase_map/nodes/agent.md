# Node: agent

Generated: 2026-06-03T02:24:29Z

## Purpose
Agent Layer (слой 4 архитектуры): инструменты агента (`ToolPlugin`) и поддерживающая их обвязка. Не транспорт (`tg/`, `desktop/`, `miniapp/`) и не реализация режимов (`modes/`); инструменты публикуются через `ToolRegistry` (`modes/sdk/runtime/tooling/`) и потребляются из `bot.py`/SDK-сервисов.

Состав:
- `agent/plugins/` — ~50 плагинов-инструментов (по файлу на плагин): файловые операции (`read_file.py`, `write_file.py`, `edit_file.py`, `delete_file.py`, `list_directory.py`), команды (`run_command.py`, `ssh_exec.py`), поиск (`search_files.py`, `search_text.py`, `search_web.py`, `memory_search.py`), web/research (`fetch_page.py`, `web_research.py`, `website_content.py`, `github_analysis.py`), генерация медиа и TTS (`stable_diffusion.py`, `ddg_image_search.py`, `haiper_image_to_video.py`, `gtts_text_to_speech.py`, `auto_tts.py`), задачи/память/напоминания (`manage_tasks.py`, `task_management.py`, `schedule_task.py`, `reminders.py`, `memory.py`), admin-инструменты (`admin_*.py`), служебные (`intent_plugin.py`, `analyst_intent_plugin.py`, `brainstorm.py`, `chief.py`, `ask_user.py`, `prompt_perfect.py`, `manage_message.py`, `send_file.py`, `get_tool_details.py`). Базовый класс — `agent/plugins/base.py` (`ToolPlugin`), руководство — `agent/plugins/plugin-development.md`.
- `agent/tooling/helpers.py` — store pending-команд, approval-flow и `execute_shell_command` (реэкспортируются через `agent/__init__.py`).
- `agent/approvals/blocked-patterns.json` — регэксп-паттерны команд, запрещённых всегда (env-leak, DoS и т.п.).
- `agent/cli_routing.py` — маршрутизация типа работы (`analytics`/`planning`/`development`/`administration`/…) на CLI-агентов (`claude`/`codex`/`gemini`/`qwen`/`grok`); дефолты в `DEFAULT_CLI_ROUTING`.
- `agent/manager_core.py` (+ реэкспорт `agent/manager.py`, промпты `agent/manager_prompts.py`) — ядро manager-режима: пайплайн decompose/dev/review/final audit.
- `agent/analyst_prompts.py` — сборка промптов analyst-режима из выбранного шаблона.
- `agent/telegram_wiring.py` — регистрация plugin-handlers в Telegram-приложении (`install_plugin_handlers`).
- `agent/mcp/` — точка интеграции MCP; фактические MCP-клиенты в `modes/sdk/runtime/mcp/`.

## Scope
- Source glob: `agent/**`
- Current files: 61 under `agent/**` as of last review.
- Корневые модули: `agent/__init__.py`, `agent/cli_routing.py`, `agent/manager_core.py`, `agent/manager.py`, `agent/manager_prompts.py`, `agent/analyst_prompts.py`, `agent/telegram_wiring.py`.
- Подпакеты: `agent/plugins/` (плагины + `base.py`, `plugin-development.md`), `agent/tooling/`, `agent/approvals/`, `agent/mcp/`.

## Instructions for agent
- Read only files relevant to the active task; плагинов ~50 — не загружать весь `agent/plugins/`.
- Новый плагин — наследник `ToolPlugin` из `agent/plugins/base.py`; следовать `agent/plugins/plugin-development.md` (ToolSpec, меню, диалоги, регистрация через `ToolRegistry`). Префикс имён функций (`function_prefix`) — opt-in.
- Тяжёлую логику не размещать здесь: операции — в `app/services/`, плагин лишь вызывает сервис.
- Изменяя `agent/approvals/blocked-patterns.json`, не ослаблять запреты на утечку секретов/DoS; править через анализ корневой причины, не маскируя.
- `agent/manager.py` — тонкий реэкспорт `manager_core`; логику править в `agent/manager_core.py`.
- Prefer deterministic checks before edits. Keep changes minimal and validate with `pytest -q` and `flake8 .`.

## Source of truth
Код — единственный источник истины; точки входа области:
- `agent/__init__.py` — публичный реэкспорт approval/pending-command API из `agent/tooling/helpers.py`.
- `agent/plugins/base.py` — базовый класс `ToolPlugin`; `agent/plugins/plugin-development.md` — руководство.
- `agent/plugins/*.py` — конкретные инструменты (по одному файлу на плагин).
- `agent/tooling/helpers.py` — execute/approval pending-команд.
- `agent/approvals/blocked-patterns.json` — запрещённые паттерны команд.
- `agent/cli_routing.py` — `DEFAULT_CLI_ROUTING` и классификатор типа работы.
- `agent/manager_core.py`, `agent/manager_prompts.py` — manager-пайплайн и его промпты.
- `agent/analyst_prompts.py` — промпты analyst-режима.
- `agent/telegram_wiring.py` — `install_plugin_handlers`.
- `agent/mcp/__init__.py` — пакет интеграции MCP (клиенты в `modes/sdk/runtime/mcp/`).

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
- `nodes/miniapp.md`
- `nodes/modes.md`
- `nodes/session-py.md`
- `app` confidence=0.95 via L0/L1/L2
- `bot.py` confidence=0.90 via L0/L2
- `config.py` confidence=0.90 via L2
- `config_example.yaml` confidence=0.89 via L0
- `desktop` confidence=0.76 via L0
- `miniapp` confidence=0.95 via L0
- `modes` confidence=0.95 via L0/L1/L2
- `session.py` confidence=0.95 via L0/L2

## Owner
- project-maintainers

## Last reviewed
- 2026-06-17 (Manager final report saved to shared report history)
- 2026-06-12 (OpenAI client default X-Title header)
