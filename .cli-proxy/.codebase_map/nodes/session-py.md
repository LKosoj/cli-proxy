# Node: session.py

Generated: 2026-06-03T02:24:29Z

## Purpose
`session.py` — Core Layer: определяет dataclass `Session` (`session.py:352`) и `SessionManager` (`session.py:2087`).

`Session` инкапсулирует одну беседу с CLI-агентом: рабочую директорию, активный CLI/`tool` (`ToolConfig`), пер-CLI resume-токены, два бэкенда исполнения (headless-subprocess и интерактивный `pexpect`), `busy`/`queue`/`run_lock`/`send_lock`, ticks активности и вложенное состояние `CliState` (`session.py:289`), `GitState` (`session.py:298`), `ModeState` (`session.py:308`), `OrchestratorState` (`session.py:320`), `SddState` (`session.py:328`). Легаси-поля (`active_cli`, `resume_tokens`, `agent_memory`, `git_busy`, ...) принимаются как `InitVar` и сворачиваются во вложенное состояние в `__post_init__` (`session.py:415`), а плоский доступ к ним сохраняется через прокси `_LEGACY_STATE_FIELDS` / `__getattribute__` / `__setattr__` (`session.py:503-548`).

`SessionManager` владеет chat-scoped инвентарём сессий (`sessions_by_chat`), uid-индексом (`_session_by_uid`), создаёт/ищет/закрывает сессии и персистит их в `state.json` через state-repository (`get_state_repository(self.config.defaults.state_path)`, `session.py:2090`).

## Scope
- Source glob: `session.py`
- Estimated files: 1
- Узел покрывает только модель сессии и её менеджер. Транспортный слой управления сессиями (UI, роутинг команд) живёт в `sessions/**` (`sessions/session_management.py`, `sessions/session_ui.py`) и `tg/**`, а не здесь.

## Instructions for agent
- Перед любым утверждением о runtime-поведении проверять конкретный метод в `session.py` и ссылаться на `session.py:<строка>`; публичная поверхность зеркалируется в `.cli-proxy/.codebase_map/api/session-py.md`.
- Не менять порядок сворачивания легаси-полей в `__post_init__` (`session.py:415`) и прокси `_LEGACY_STATE_FIELDS` (`session.py:503-548`): persisted `state.json` и тесты опираются на то, что `active_cli`/`resume_tokens`/`agent_memory`/`git_busy` и т.п. читаются и как вложенные, и как плоские атрибуты.
- `resume_token` — это представление пер-CLI словаря, ключуемое активным CLI (`session.py:559`). Никогда не хранить единый глобальный токен — писать через `self.resume_token` / `self.cli.resume_tokens`. `set_active_cli` (`session.py:574`) переключает только на CLI из `config.tools` и обновляет `tool` + активный токен.
- Внутри `_run_headless` resume_token фиксируется СРАЗУ на событии стрима `session_started` (для Claude — `system/init`), а не откладывается до финального `completed`. Это сохраняет id resumable-сессии при `/interrupt` и при ошибке прогона. Гейт — наличие `session_started`: если CLI не создал сессию (нет init, как в `test_headless_claude_does_not_persist_failed_fresh_session`), токен остаётся `None`. Не возвращать отложенную запись токена на `completed`.
- CLI-специфичные ветки в `_run_headless` (`session.py:699`) — codex/gemini/qwen/claude/grok — НЕ взаимозаменяемы (свои JSON-stream адаптеры, мониторы и восстановление resume-токена). Проверять каждую ветку отдельно, не выводя поведение по аналогии (см. policy в `INDEX.md`).
- Claude в headless и интерактиве обязан исполняться через обёртку `su - claude-bot -c` со снятием nested-маркеров `CLAUDECODE`/`CLAUDE_CODE_*` (`session.py:782-811`, `session.py:1271-1274`); без этого вложенный `claude` падает с конфликтом «уже внутри Claude Code».
- Инвентарь `SessionManager` — chat-scoped: мутации проходить через `_index_session`/`_unindex_session` (`session.py:2138-2151`) и завершать `_persist_sessions` (`session.py:2528`), иначе ломается uid-lookup и персист.
- После правок прогонять `pytest -q` и `flake8 .`; держать изменения минимальными.

## Source of truth
- `session.py` — единственный файл узла.
- Зеркало публичного API: `.cli-proxy/.codebase_map/api/session-py.md`.
- Персист сессий: `state.json` через `app/services/state_repository.py` (`get_state_repository`, `session.py:2090`); путь — из `config.defaults.state_path` (`config.py`).
- Прямые import-зависимости (`session.py:19-45`):
  - `app/services/*` — `claude_jsonl_monitor`, `cli_json_stream`, `gemini_session_monitor`, `tool_availability`, `project_prompts_service`, `ssh_skill_generator`, `state_repository`, `session_tick_history_store`, `qwen_jsonl_monitor`, `ssh_config_loader` (ленивый импорт).
  - `config.py` — `AppConfig`, `ToolConfig`, `save_config`.
  - `modes/sdk/runtime/cli_contracts.py` — `parse_bundle_for_response_format`.
  - `sessions/` — `conversation_scope`, `queue_item`, `scoped_key`.
  - `utils/` — `cli` (`build_command`, `detect_prompt_regex`, `detect_resume_regex`, `resolve_env_value`), `paths` (`sandbox_session_dir`, `legacy_sandbox_session_dir`), `text` (`strip_ansi`, `extract_tick_tokens`, `is_time_only_text`).
- Ключевые точки входа: `run_prompt` (`session.py:599`) → `_run_headless` (`session.py:699`) / `_run_interactive` (`session.py:1246`); `interrupt` (`session.py:1351`), `close_headless_process` (`session.py:1472`), `close` (`session.py:1491`); `SessionManager.create` (`session.py:2210`), `get`/`get_by_uid`/`get_by_scope` (`session.py:2272`/`2292`/`2339`), `close` (`session.py:2358`), `persist_session` (`session.py:2519`). Модульные хелперы: `ensure_cli_proxy_gitignored` (`session.py:64`), `switch_session_active_cli_if_needed` (`session.py:229`), `session_runtime_uid` (`session.py:272`), `run_tool_help` (`session.py:2734`).

## Module API
Детальные интерфейсы модулей этой области:

- [session.py](../api/session-py.md)

## When to update
- Любой коммит, затрагивающий `session.py`.
- Изменение публичной поверхности `Session`/`SessionManager` (см. `api/session-py.md`): `run_prompt`, `set_active_cli`, `resume_token`, `create`/`get`/`get_by_uid`/`close`/`persist_session`.
- Изменение схемы вложенного состояния (`CliState`/`GitState`/`ModeState`/`OrchestratorState`/`SddState`) или сериализации `state.json` в `app/services/state_repository.py` — должно оставаться round-trip-совместимым с легаси-полями.
- Изменение CLI JSON-stream адаптеров/мониторов в `app/services/*` (`cli_json_stream`, `*_monitor`), которые потребляет `_run_headless`.
- Изменение полей `ToolConfig` в `config.py` (`cmd`/`headless_cmd`/`resume_cmd`/`image_cmd`/`env`/...) или схемы инструментов в `config_example.yaml`, используемых `build_command`.
- Любой коммит в `app/**`, `config.py`, `config_example.yaml`, `sessions/**`, `utils/**`, от которых узел зависит по импортам.
- Любое архитектурное или поведенческое изменение в этой области.

## Related nodes
- `nodes/app.md` — прямая import-зависимость: `app/services/*` (state-repo, JSON-stream, мониторы, tool-availability), confidence=0.95 via L0/L1/L2.
- `nodes/config-py.md` — прямая import-зависимость: `AppConfig`/`ToolConfig`/`save_config`, confidence=0.90 via L2.
- `nodes/modes.md` — `modes/sdk/runtime/cli_contracts`; `Session`/`SessionManager` используются режимами, confidence=0.90 via L0/L1/L2.
- `nodes/config-example-yaml.md` — схема `tools.*`, по которой строятся команды CLI, confidence=0.89 via L0.
- `nodes/bot-py.md` — потребитель: держит `SessionManager` и `session_runtime_uid`, confidence=0.76 via L0.
- `nodes/agent.md` — потребитель: плагины/agent-режим работают с `Session`, confidence=0.95 via L0.
- `nodes/miniapp.md` — потребитель сессий, confidence=0.95 via L0.
- `nodes/desktop.md` — потребитель; синхронизировать функциональность с desktop-клиентом, confidence=0.76 via L0.
- `nodes/sessions.md` — прямые import-зависимости `conversation_scope`/`queue_item`/`scoped_key` (рёбра не в graph.json, но импорт фактический).
- `nodes/utils.md` — прямые import-зависимости `utils/cli`, `utils/paths`, `utils/text` (рёбра не в graph.json, но импорт фактический).

## Owner
- project-maintainers

## Last reviewed
- 2026-06-14
