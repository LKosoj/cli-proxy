# Структура проекта

Обновлено: 2026-05-01. Основа: `rg --files` по основным source/test area.

## Верхний уровень

```text
/srv/git_projects/cli-proxy/
├── bot.py
├── config.py
├── config_example.yaml
├── session.py
├── summary.py
├── app/
├── agent/
├── modes/
├── sessions/
├── tg/
├── miniapp/
├── desktop/
├── utils/
├── tests/
├── scripts/
└── .cli-proxy/.codebase_map/
```

## Основные каталоги

| Путь | Файлов по `rg --files` | Роль |
|---|---:|---|
| `app/` | 154 | Shared application services, config runtime, security, events, bootstrap |
| `modes/` | 220 | Mode plugins, SDK, runtime executor/dispatcher, validation/tooling |
| `agent/` | 61 | CLI routing, manager core, tool plugins, Telegram plugin wiring |
| `tests/` | 547 | Unit/integration/smoke coverage |
| `desktop/` | 32 | PySide6 Desktop UI, facade, widgets, desktop services |
| `tg/` | 17 | Telegram commands, callbacks, message processing, markdown/entities |
| `miniapp/` | 21 | aiohttp MiniApp backend and static frontend |
| `sessions/` | 10 | Session-facing helpers around core `session.py` |
| `utils/` | 8 | Shared CLI/path/text/UI/html helpers |

## Детализация

### `app/`

```text
app/
├── bootstrap.py
├── mode_dependencies.py
├── config_runtime/
│   ├── loader.py
│   ├── models.py
│   ├── adapter.py
│   └── serialization.py
├── events/
│   └── bus.py
├── security/
│   ├── facade.py
│   ├── auth.py
│   ├── validators.py
│   ├── rate_limits.py
│   ├── audit.py
│   ├── errors.py
│   └── interfaces.py
└── services/
    ├── app_runtime_service.py
    ├── input_dispatch_service.py
    ├── core_orchestration_service.py
    ├── mode_run_lifecycle_service.py
    ├── run_operations_service.py
    ├── session_service.py
    ├── scheduler_service.py
    ├── remote_control_service.py
    ├── remote_shell_service.py
    ├── ssh_service.py
    ├── shared_http_ingress.py
    └── lint_evolution/
```

### `modes/`

```text
modes/
├── DEVELOPMENT.md
├── registry.py
├── sdk/
│   ├── base.py
│   ├── context.py
│   ├── services/
│   └── runtime/
├── agent/
├── analyst/
├── manager/
│   └── services/
├── webmaster/
├── admin/
│   ├── runner_service.py
│   ├── facade.py
│   ├── transports/
│   └── templates/
└── codebase_mapper/
    ├── mode.py
    ├── runtime.py
    ├── ui.py
    └── prompts.yaml
```

### `agent/`

```text
agent/
├── cli_routing.py
├── manager.py
├── manager_core.py
├── manager_prompts.py
├── analyst_prompts.py
├── telegram_wiring.py
├── approvals/
├── mcp/
├── tooling/
└── plugins/
```

### Transports and UI

```text
tg/
├── wiring.py
├── handlers.py
├── callbacks.py
├── message_processor.py
├── markdown.py
├── command_registry.py
└── callback_actions/

miniapp/
├── server.py
├── routes.py
├── auth.py
├── services/
└── static/

desktop/
├── main.py
├── main_window.py
├── services/
└── widgets/
```

### Session and helpers

```text
sessions/
├── conversation_scope.py
├── scoped_key.py
├── session_management.py
├── session_run_service.py
├── session_output_service.py
├── session_state_access.py
├── session_status.py
└── session_ui.py

utils/
├── cli.py
├── html_renderer.py
├── paths.py
├── source_artifact.py
├── text.py
└── ui.py
```

## Codebase map

```text
.cli-proxy/.codebase_map/
├── INDEX.md
├── ARCHITECTURE.md
├── STRUCTURE.md
├── STACK.md
├── INTEGRATIONS.md
├── CONVENTIONS.md
├── TESTING.md
├── CONCERNS.md
├── nodes/
├── api/
├── graph.json
├── meta.json
└── rules.yaml
```

## Ограничения обзора

- Структура отражает файлы, видимые `rg --files`; ignored/generated файлы не перечислялись.
- Каталог `tests/` крупный, поэтому в структуре приведен как отдельная зона без детального дерева.
