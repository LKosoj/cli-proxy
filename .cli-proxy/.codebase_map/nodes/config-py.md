# Node: config.py

Generated: 2026-06-03T02:24:29Z

## Purpose
`config.py` — схема конфигурации приложения (Core Layer) и фасад её загрузки/сохранения. Определяет dataclass-модель `AppConfig` (`config.py:293`) и вложенные конфиги: `TelegramConfig` (`config.py:12`), `ToolConfig` (`config.py:45`), `DefaultsConfig` (`config.py:66`), `MCPConfig`/`MCPClientServerConfig` (`config.py:165`, `config.py:173`), `MiniAppConfig` (`config.py:197`), `ThreadModeConfig` (`config.py:208`), `WebhooksConfig` (`config.py:217`), `SchedulerConfig` (`config.py:227`), `SecurityConfig` (`config.py:254`), `LintEvolutionConfig` (`config.py:259`), `SSHHostConfig` (`config.py:273`), `PresetConfig` (`config.py:191`). Публичные функции — тонкие фасады: `load_config(path)` (`config.py:309`) подгружает `.env` через `load_dotenv_near` и делегирует валидацию/адаптацию в `app/config_runtime/*`; `app_config_to_dict` (`config.py:323`) и `save_config` (`config.py:329`) делегируют сериализацию в `app/config_runtime/serialization.py`.

## Scope
- Source glob: `config.py`
- Estimated files: 1
- Узел — это схема (dataclasses) + фасад загрузки. Сама валидация, парсинг YAML, адаптация и сериализация живут НЕ здесь, а в `app/config_runtime/**`. Дефолты полей `config.py` — источник истины для значений по умолчанию.

## Instructions for agent
- Перед утверждениями о составе/дефолтах конфигурации проверять конкретное поле dataclass в `config.py` и ссылаться на `config.py:<строка>` (см. зеркало интерфейсов `api/config-py.md`).
- Добавление/переименование поля конфигурации затрагивает несколько мест: dataclass в `config.py`, маппинг валидации/адаптации в `app/config_runtime/models.py` и `app/config_runtime/adapter.py`, сериализацию в `app/config_runtime/serialization.py`. Менять синхронно.
- Согласно `CLAUDE.md`, любую новую опцию добавлять в ОБА файла: `config.yaml` и `config_example.yaml` (узел `nodes/config-example-yaml.md`).
- Секреты не хранить в схеме по умолчанию: чувствительные значения (`openai_api_key`, `github_token`, `*_env`-ссылки в `SSHHostConfig`) приходят из `.env`/окружения через `load_dotenv_near` (`config.py:313`, реализация — `app/services/dotenv_loader.py`); `override=False` — не перетирать переменные из systemd/docker.
- `config.py` не должен импортировать тяжёлые рантайм-модули на уровне модуля: загрузчик/адаптер/сериализатор импортируются лениво внутри функций (`config.py:317`, `config.py:324`, `config.py:330`) — сохранять этот паттерн, чтобы избежать циклов импорта.
- После правок прогонять `pytest -q tests/test_config_serialization.py` (round-trip схемы) и `flake8 .`; держать изменения минимальными.

## Source of truth
- `config.py` — единственный файл узла (схема dataclasses + фасады `load_config`/`save_config`/`app_config_to_dict`).
- Зеркало публичного API: `.cli-proxy/.codebase_map/api/config-py.md`.
- Прямые зависимости (импорты `config.py`):
  - `app/services/dotenv_loader.py` — `load_dotenv_near` (загрузка `.env` рядом с config-файлом, `config.py:5`).
  - `app/config_runtime/loader.py` — `load_validated_settings` (парсинг + валидация YAML, `config.py:318`).
  - `app/config_runtime/adapter.py` — `adapt_validated_settings` (validated settings → `AppConfig`, `config.py:317`).
  - `app/config_runtime/serialization.py` — `serialize_app_config`, `dump_app_config_yaml` (`config.py:324`, `config.py:330`).
  - `app/config_runtime/models.py` — pydantic/валидационные модели, на которые опирается loader/adapter.
- Основные потребители `AppConfig`/`load_config`: `bot.py`, `session.py`, `app/services/config_service.py`, `miniapp/services/config_service.py`, `agent/manager_core.py`, `agent/cli_routing.py`, `modes/sdk/runtime/memory_policy.py`, `i18n/resolver.py`, `utils/lang.py`.

## Module API
Детальные интерфейсы модулей этой области:

- [config.py](../api/config-py.md)

## When to update
- Любой коммит, затрагивающий `config.py`.
- Любой коммит в `app/**` (особенно `app/config_runtime/**`, `app/services/dotenv_loader.py`) — узел имеет import/call-зависимость от них (confidence=0.90).
- Любой коммит в `config_example.yaml` — пример конфигурации должен соответствовать схеме (confidence=0.76).
- Любой коммит в `tests/**`, влияющий на проверки конфигурации (`tests/test_config_serialization.py` и др.; confidence=0.76).
- Любой коммит в `utils/**`, использующий поля конфигурации (`utils/lang.py`; confidence=0.76).
- Добавление/переименование/удаление поля любого dataclass или изменение дефолтов — синхронно с `config.yaml`/`config_example.yaml` и `app/config_runtime/**`.
- Любое архитектурное или поведенческое изменение в этой области.

## Related nodes
- `nodes/app.md` — рантайм загрузки/валидации/сериализации конфигурации (`app/config_runtime/**`, `app/services/config_service.py`, `dotenv_loader`), confidence=0.90 via L0/L1/L2.
- `nodes/config-example-yaml.md` — эталонный пример конфигурации; держать синхронным со схемой, confidence=0.76 via L0.
- `nodes/tests.md` — проверки round-trip и совместимости схемы (`tests/test_config_serialization.py`), confidence=0.76 via L0.
- `nodes/utils.md` — потребители полей конфигурации (`utils/lang.py`), confidence=0.76 via L0.
- `nodes/bot-py.md` — composition root, грузит `AppConfig` через `load_config`.
- `nodes/session-py.md` — использует поля конфигурации для сессий.

## Owner
- project-maintainers

## Last reviewed
- 2026-06-03
