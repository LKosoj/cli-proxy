# Node: config_example.yaml

Generated: 2026-06-03T02:24:29Z

## Purpose
`config_example.yaml` — версионируемый эталонный пример конфигурации бота (без секретов). Документирует все поддерживаемые секции и их дефолты: `telegram` (token/whitelist/admlist, `user_workdirs`, `user_modes`, `user_languages`, таймауты polling/httpx), `tools` (профили CLI codex/claude/gemini/qwen/grok: `cmd`/`resume_cmd`/`image_cmd`/`env`/`separate_stderr`), `defaults` (workdir, `default_cli`, `default_language`, лимиты памяти, `manager_*`, `run_artifacts_*`, `memory_*`, `tool_disclosure`, контекст-токены, `skill_*`, `cli_routing` по задачам), `mcp`, `miniapp`, `thread_mode`, `webhooks`, `scheduler`, `security.rate_limits`, `lint_evolution`, `mcp_clients`, `presets`. Это шаблон для копирования в локальный `config.yaml`; значения здесь — плейсхолдеры (`YOUR_TELEGRAM_BOT_TOKEN`, `/Users/you/projects`, `null` для ключей).

## Scope
- Source glob: `config_example.yaml`
- Estimated files: 1
- Узел — это документация-пример, а не схема. Канонический источник полей и дефолтов — dataclasses в `config.py` (узел `nodes/config-py.md`); парсинг/валидация — `app/config_runtime/**`. Этот файл обязан оставаться синхронным со схемой и с локальным `config.yaml`.

## Instructions for agent
- Согласно `CLAUDE.md`, любую новую опцию добавлять в ОБА файла одновременно: `config.yaml` (рабочий, не коммитится с секретами) и `config_example.yaml` (эталон). Не оставлять рассинхрон.
- Перед утверждениями о наборе/дефолтах полей сверяться с dataclass в `config.py` и валидационными моделями `app/config_runtime/models.py` — не описывать здесь ключи, которых нет в схеме.
- Не помещать в этот файл реальные секреты: ключи и токены остаются `null`/плейсхолдерами; реальные значения приходят из `.env`/окружения. Это проверяется тестом `tests/test_config_templates_no_keyring.py`.
- Новые поля сопровождать кратким inline-комментарием назначения/допустимых значений (как сделано для `memory_*`, `run_artifacts_*`, `skill_*`, `thread_mode`, `webhooks`, `scheduler`) — `tests/test_config_loader.py:325` проверяет наличие пояснений для run-операций и skill-политики.
- После правок прогонять валидационные тесты этого файла (см. Source of truth) и `flake8 .`; держать изменения минимальными.

## Source of truth
- `config_example.yaml` — единственный файл узла.
- Каноническая схема и дефолты: `config.py` (узел `nodes/config-py.md`); валидация/адаптация: `app/config_runtime/loader.py`, `app/config_runtime/models.py`, `app/config_runtime/adapter.py`.
- Должен оставаться синхронным с локальным `config.yaml` (узла нет — файл не индексируется).
- Тесты, фиксирующие контракт этого файла:
  - `tests/test_config_loader.py:204` — `load_validated_settings("config_example.yaml")` должен валидно парситься (`thread_mode`/`webhooks`/`scheduler`).
  - `tests/test_config_loader.py:325` — пример документирует run-операции и влияние skill-политики (наличие комментариев).
  - `tests/test_config_serialization.py:197` — соответствие runtime-policy контракту (`toolhelp_path`, `default_language`, набор ключей `lint_evolution`).
  - `tests/test_config_models.py:284` — пример валиден относительно pydantic-моделей.
  - `tests/test_config_templates_no_keyring.py:13` — в трекаемом шаблоне нет секретов.
- Включён в список tracked-шаблонов: `utils/source_artifact.py:29`, `utils/source_artifact.py:43`.

## When to update
- Любой коммит, изменяющий `config_example.yaml`.
- Любое добавление/переименование/удаление поля конфигурации или изменение дефолта в `config.py` — синхронно с этим файлом и с `config.yaml`.
- Любой коммит в `app/config_runtime/**`, меняющий набор валидируемых полей или их допустимые значения.
- Любой коммит, добавляющий/меняющий поведение, управляемое опциями (например `agent/**`, `modes/**`, `miniapp/**`), если он вводит новый конфиг-флаг.
- Изменения в тестах-валидаторах `tests/test_config_loader.py`, `tests/test_config_serialization.py`, `tests/test_config_models.py`, `tests/test_config_templates_no_keyring.py`.
- Любое архитектурное или поведенческое изменение в этой области.

## Related nodes
- `nodes/config-py.md` — каноническая схема/дефолты; держать пример синхронным со схемой (confidence=0.76 via L0).
- `nodes/app.md` — рантайм валидации/адаптации конфигурации (`app/config_runtime/**`), confidence=0.95 via L0.
- `nodes/tests.md` — валидаторы примера (`tests/test_config_*`), confidence=0.95 via L0.
- `nodes/modes.md` — режимы, чьё поведение управляется опциями `defaults.*` (manager/analyst/webmaster/sdd), confidence=0.76 via L0.
- `nodes/agent.md` — agent-инструменты и роутинг CLI, конфигурируемые `tools`/`cli_routing` (confidence=0.89 via L0).
- `nodes/miniapp.md` — секция `miniapp`, confidence=0.89 via L0.
- `nodes/session-py.md` — потребитель полей конфигурации в сессиях, confidence=0.89 via L0.
- `nodes/setup-bot-sh.md` — установочный скрипт, работающий с шаблоном конфигурации, confidence=0.76 via L0.

## Owner
- project-maintainers

## Last reviewed
- 2026-06-03
