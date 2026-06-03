# Node: modes

Generated: 2026-06-03T02:24:29Z

## Purpose
Mode Layer проекта: пакет `modes/` реализует подключаемые режимы работы агента. Каждый режим — подкаталог с `__init__.py`, экспортирующим `PLUGIN` (экземпляр или подкласс `BaseMode` из `modes/sdk/base.py`). Режимы обнаруживаются и регистрируются в рантайме через `modes/registry.py` (`ModeLoader.discover/load_into`, `ModeRegistry`). Режимы транспорт-агностичны и не обращаются к `BotApp` напрямую — инфраструктура инжектируется как SDK-сервисы в `BaseMode.initialize(config, services)`.

Зарегистрированные режимы (`mode_id` → описание, источник — `modes/<id>/mode.py`):
- `admin` (🛡 Admin) — базовое администрирование сессии — `modes/admin/mode.py`.
- `agent` (🤖 Агент) — ИИ-агент-оркестратор с инструментами и планированием — `modes/agent/mode.py`.
- `analyst` (🧠 Аналитик) — формирует ТЗ и поддерживает аудит-поток — `modes/analyst/mode.py`.
- `codebase_mapper` (🗺 Mapper) — строит карту кодовой базы и обновляет её по git diff — `modes/codebase_mapper/mode.py`.
- `manager` (🏗 Менеджер) — декомпозиция плана, управление фазами, тихий режим — `modes/manager/mode.py`.
- `sdd` (📐 SDD) — Spec-Driven Development (specify → plan → tasks через гейты) — `modes/sdd/mode.py`.
- `webmaster` (🌐 Вебмастер) — управление web-сайтом через `use_cli` — `modes/webmaster/mode.py`.

`modes/sdk/` — общий SDK (контракт `BaseMode`, сервисы, рантайм, MCP-клиенты), а НЕ режим: загрузчик исключает его (`ModeLoader.excluded_dirs = {"sdk", "__pycache__"}`).

## Scope
- Source glob: `modes/**`
- Estimated files: ~205
- Подпакеты режимов: `modes/admin/`, `modes/agent/`, `modes/analyst/`, `modes/codebase_mapper/`, `modes/manager/`, `modes/sdd/`, `modes/webmaster/`.
- Инфраструктура: `modes/sdk/` (в т.ч. `modes/sdk/services/`, `modes/sdk/runtime/`, `modes/sdk/runtime/mcp/`, `modes/sdk/planning/`).
- Корневые файлы: `modes/registry.py`, `modes/DEVELOPMENT.md`, `modes/codebase_mapper_constants.py`, `modes/__init__.py`.

## Instructions for agent
- Перед любыми изменениями режима обязательно прочитать `modes/DEVELOPMENT.md`.
- Контракт режима — `modes/sdk/base.py` (`BaseMode`): минимум `mode_id`, `display_name`, `description`, `handle_input(...)`, `handle_callback(...)`; опционально `on_enable/on_disable`, `build_menu`, feature-методы.
- Новый режим = подкаталог `modes/<id>/` с `__init__.py`, экспортирующим `PLUGIN`; ручная правка `registry.py` не требуется — он обнаруживается по `discover()`.
- Не импортировать `BotApp` напрямую — использовать SDK-сервисы из `modes/sdk/__init__.py` (`tasks`, `dialogs`, `session_control`, `messaging_factory`, `pipeline`, `tooling`, `agent_runtime`, `dirs_flow`), инжектируемые в `initialize(...)`.
- MCP-клиенты — `modes/sdk/runtime/mcp/` (`manager.py`, `http_client.py`, `stdio_client.py`, `jsonrpc.py`); не дублировать MCP-логику в режимах.
- Read only files relevant to the active task; deterministic checks before edits.
- Валидировать `pytest -q` (тесты режимов — узел `nodes/tests.md`); линт — `flake8 .`.

## Source of truth
- `modes/registry.py` — обнаружение/регистрация режимов (`ModeLoader`, `ModeRegistry`).
- `modes/sdk/base.py` — контракт `BaseMode`.
- `modes/sdk/__init__.py` — публичный экспорт SDK-сервисов и моделей.
- `modes/DEVELOPMENT.md` — правила разработки режимов.
- `modes/<id>/mode.py` и `modes/<id>/prompts.yaml` — реализация и промпты каждого режима.
- `modes/__init__.py`, `modes/codebase_mapper_constants.py`.

## Module API
Детальные интерфейсы модулей этой области:

- [modes/analyst/__init__.py](../api/modes/analyst/__init__-py.md)
- [modes/manager/__init__.py](../api/modes/manager/__init__-py.md)
- [modes/sdd/__init__.py](../api/modes/sdd/__init__-py.md)
- [modes/admin/action_specs.py](../api/modes/admin/action_specs-py.md)
- [modes/admin/allowlist.py](../api/modes/admin/allowlist-py.md)
- [modes/admin/analyzer.py](../api/modes/admin/analyzer-py.md)
- [modes/admin/autonomy_loop.py](../api/modes/admin/autonomy_loop-py.md)
- [modes/admin/autonomy_policy.py](../api/modes/admin/autonomy_policy-py.md)
- [modes/admin/baseline.py](../api/modes/admin/baseline-py.md)
- [modes/admin/chat_gateway.py](../api/modes/admin/chat_gateway-py.md)
- [modes/admin/chat_memory.py](../api/modes/admin/chat_memory-py.md)
- [modes/admin/chat_schemas.py](../api/modes/admin/chat_schemas-py.md)
- [modes/admin/chat_service.py](../api/modes/admin/chat_service-py.md)
- [modes/admin/config_store.py](../api/modes/admin/config_store-py.md)
- [modes/admin/drift.py](../api/modes/admin/drift-py.md)
- [modes/admin/executor.py](../api/modes/admin/executor-py.md)
- [modes/admin/facade.py](../api/modes/admin/facade-py.md)
- [modes/admin/memory.py](../api/modes/admin/memory-py.md)
- [modes/admin/mode.py](../api/modes/admin/mode-py.md)
- [modes/admin/monitor.py](../api/modes/admin/monitor-py.md)
- [modes/admin/notifier.py](../api/modes/admin/notifier-py.md)
- [modes/admin/plugin_tools.py](../api/modes/admin/plugin_tools-py.md)
- [modes/admin/prereqs.py](../api/modes/admin/prereqs-py.md)
- [modes/admin/reconciliation.py](../api/modes/admin/reconciliation-py.md)
- [modes/admin/runbook_builder.py](../api/modes/admin/runbook_builder-py.md)
- [modes/admin/runbook_facade.py](../api/modes/admin/runbook_facade-py.md)
- [modes/admin/runbook_promoter.py](../api/modes/admin/runbook_promoter-py.md)
- [modes/admin/runbook_validator.py](../api/modes/admin/runbook_validator-py.md)
- [modes/admin/runbooks.py](../api/modes/admin/runbooks-py.md)
- [modes/admin/runner_service.py](../api/modes/admin/runner_service-py.md)
- [modes/admin/scanner.py](../api/modes/admin/scanner-py.md)

## When to update
- Любой коммит, затрагивающий `modes/**`.
- Добавление/удаление режима (новый `modes/<id>/` с `PLUGIN`) или изменение контракта `modes/sdk/base.py`.
- Изменение состава SDK-сервисов в `modes/sdk/__init__.py` или правил загрузки в `modes/registry.py`.
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
- `nodes/miniapp.md`
- `nodes/scripts.md`
- `agent` confidence=0.95 via L0/L1/L2
- `app` confidence=0.95 via L0/L1/L2
- `bot.py` confidence=0.90 via L0/L2
- `config.py` confidence=0.90 via L2
- `config_example.yaml` confidence=0.76 via L0
- `desktop` confidence=0.76 via L0
- `miniapp` confidence=0.89 via L0
- `scripts` confidence=0.90 via L1/L2

## Owner
- project-maintainers

## Last reviewed
- 2026-06-03 (enrichment pass)
